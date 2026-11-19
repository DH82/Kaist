"""RTX 4090에서는 SAM3 단계를 한 번에 1개만 실행하고, 그동안 다른 GPU 단계는 남은 슬롯에서 함께 실행됩니다.

실제 runner.py·gpu_slots.py를 임시 폴더에 복사하고 어댑터(kaist_runtime.py)만 가짜로 바꿔 실행합니다.
  python3 -m unittest deploy/test_sam3_single.py
"""
import json, os, shutil, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = """import sys,time
from pathlib import Path
log=Path(__file__).resolve().parent/'order.log'
def note(word):
    with log.open('a') as f:f.write(f'{word} {sys.argv[1]} {sys.argv[2]}\\n')
note('begin');time.sleep(2);note('end')
print('true')
"""


class Sam3Single(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for d in ('worker', 'dags', 'config'):
            (self.tmp / d).mkdir()
        for name in ('runner.py', 'gpu_slots.py'):
            shutil.copy(ROOT / 'worker/app/worker' / name, self.tmp / 'worker' / name)
        shutil.copy(ROOT / 'worker/app/dags/pipeline_progress.py', self.tmp / 'dags')
        (self.tmp / 'dags/kaist_runtime.py').write_text(ADAPTER)
        (self.tmp / 'config/gpu_4090_a.json').write_text('{}')
        self.env = {**os.environ, 'KAIST_SAM3_SLOTS': '1'}

    def act(self, action, req):
        r = subprocess.run([sys.executable, 'runner.py', action], input=json.dumps(req), cwd=self.tmp / 'worker',
                           capture_output=True, text=True, check=True, env=self.env)
        return json.loads(r.stdout)

    def req(self, stage, ep):
        return {'stage': stage, 'episode': ep, 'run_id': 'r1', 'sam_prompt': '', 'sam_scene': '2d', 'sam_confirmed': False}

    def finish(self, jobs, seconds=30):
        end = time.time() + seconds
        while time.time() < end:
            states = [self.act('status', self.req(*j))['state'] for j in jobs]
            if all(s == 'success' for s in states):
                return
            self.assertNotIn('failed', states)
            time.sleep(0.2)
        self.fail('시간 안에 끝나지 않았습니다')

    def spans(self):
        out, open_ = {}, {}
        for i, line in enumerate((self.tmp / 'dags/order.log').read_text().splitlines()):
            word, stage, ep = line.split()
            if word == 'begin':
                open_[stage, ep] = i
            else:
                out[stage, ep] = (open_[stage, ep], i)
        return out

    def test_one_sam3_at_a_time_while_other_stage_runs(self):
        jobs = [('sam3', 'ep_a'), ('sam3', 'ep_b'), ('track', 'ep_c')]
        for j in jobs:
            self.act('start', self.req(*j))
        time.sleep(1)
        # 두 번째 SAM3는 순서를 기다리고(GPU 슬롯도 잡지 않음), 추적은 남은 슬롯에서 바로 실행됩니다.
        states = sorted(self.act('status', self.req('sam3', e))['state'] for e in ('ep_a', 'ep_b'))
        self.assertEqual(states, ['queued', 'running'])
        self.assertEqual(self.act('status', self.req('track', 'ep_c'))['state'], 'running')
        self.finish(jobs)
        s = self.spans()
        a, b, c = s['sam3', 'ep_a'], s['sam3', 'ep_b'], s['track', 'ep_c']
        first, second = sorted((a, b))
        self.assertLess(first[1], second[0])                 # SAM3끼리는 겹치지 않음
        self.assertLess(c[0], first[1])                      # 추적은 첫 SAM3가 끝나기 전에 시작
        logs = [p.read_text() for p in (self.tmp / 'worker/jobs').glob('*/job.log')]
        self.assertEqual(sum('다른 SAM3 작업이 끝나기를 기다립니다' in t for t in logs), 1)

    def test_no_limit_runs_two_sam3_together(self):
        self.env['KAIST_SAM3_SLOTS'] = '0'
        jobs = [('sam3', 'ep_a'), ('sam3', 'ep_b')]
        for j in jobs:
            self.act('start', self.req(*j))
        self.finish(jobs)
        a, b = sorted(self.spans().values())
        self.assertLess(b[0], a[1])                          # 제한이 없으면 예전처럼 함께 실행

    def test_default_limit_follows_gpu_name(self):
        sys.path.insert(0, str(self.tmp / 'worker'))
        self.addCleanup(sys.path.remove, str(self.tmp / 'worker'))
        sys.modules.pop('gpu_slots', None)
        import gpu_slots
        os.environ.pop('KAIST_SAM3_SLOTS', None)
        for name, want in (('NVIDIA GeForce RTX 4090', 1), ('NVIDIA GeForce RTX 5090', None), ('', None)):
            gpu_slots.gpu_name = lambda uuid=None, name=name: name
            self.assertEqual(gpu_slots.sam_limit(), want)

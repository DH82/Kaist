"""최종 저장(publish)은 GPU 슬롯 없이 바로 끝나고, 작업 폴더 NAS 미러링은 백그라운드 업로드가 이어서 합니다.

실제 runner.py를 임시 폴더에 복사하고 어댑터(kaist_runtime.py)·sync_work.py만 가짜로 바꿔 실행합니다.
  python3 -m unittest deploy/test_publish_background.py
"""
import json, shutil, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = """import sys,time
print('ADAPTER',' '.join(sys.argv[1:]),flush=True)
if sys.argv[1]=='hands':time.sleep(1)
print('true')
"""
SYNC = """import time
from pathlib import Path
HERE=Path(__file__).resolve().parent
def sync_episode(cfg,ep,log=print):
    if ep.endswith('bad') and not (HERE/'fixed').exists():raise RuntimeError('boom')
    with (HERE/'order.log').open('a') as f:f.write(f'begin {ep}\\n')
    time.sleep(1)
    with (HERE/'order.log').open('a') as f:f.write(f'end {ep}\\n')
    return {'bytes':1,'paths':['x'],'destination':'nas'}
def purge_local(cfg,ep,rec,log=print):return ['x']
"""


class PublishBackground(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for d in ('worker', 'dags', 'config'):
            (self.tmp / d).mkdir()
        for name in ('runner.py', 'gpu_slots.py'):
            shutil.copy(ROOT / 'worker/app/worker' / name, self.tmp / 'worker' / name)
        shutil.copy(ROOT / 'worker/app/dags/pipeline_progress.py', self.tmp / 'dags')
        (self.tmp / 'dags/kaist_runtime.py').write_text(ADAPTER)
        (self.tmp / 'dags/sync_work.py').write_text(SYNC)
        (self.tmp / 'config/gpu_4090_a.json').write_text('{}')
        sys.path.insert(0, str(self.tmp / 'worker'))
        self.addCleanup(sys.path.remove, str(self.tmp / 'worker'))
        sys.modules.pop('gpu_slots', None)
        import gpu_slots
        self.slots = gpu_slots

    def act(self, action, req):
        r = subprocess.run([sys.executable, 'runner.py', action], input=json.dumps(req), cwd=self.tmp / 'worker',
                           capture_output=True, text=True, check=True)
        return json.loads(r.stdout)

    def req(self, stage, ep):
        return {'stage': stage, 'episode': ep, 'run_id': 'r1', 'sam_prompt': '', 'sam_scene': '2d', 'sam_confirmed': False}

    def wait(self, check, seconds=20):
        end = time.time() + seconds
        while time.time() < end:
            value = check()
            if value:
                return value
            time.sleep(0.2)
        self.fail('시간 안에 끝나지 않았습니다')

    def test_publish_skips_gpu_slot_and_defers_work_sync(self):
        lock = self.tmp / 'worker/gpu_4090_a.lock'
        held = [self.slots._try(lock, 0), self.slots._try(lock, 1)]     # GPU 슬롯 2개가 모두 사용 중
        self.assertTrue(all(held))
        try:
            for ep in ('ep_a', 'ep_b'):
                self.act('start', self.req('publish', ep))
            self.act('start', self.req('mask_approved', 'ep_a'))
            self.act('start', self.req('hands', 'ep_c'))
            for stage, ep in (('publish', 'ep_a'), ('publish', 'ep_b'), ('mask_approved', 'ep_a')):
                st = self.wait(lambda: (s := self.act('status', self.req(stage, ep)))['state'] == 'success' and s)
                self.assertIsNone(st['gpu_slot'])
            self.assertEqual(self.act('status', self.req('hands', 'ep_c'))['state'], 'queued')   # GPU 단계는 계속 슬롯을 기다림
        finally:
            for f in held:
                f.close()
        st = self.wait(lambda: (s := self.act('status', self.req('hands', 'ep_c')))['state'] == 'success' and s)
        self.assertIn(st['gpu_slot'], (0, 1))
        log = next(p.read_text() for p in (self.tmp / 'worker/jobs').glob('*/job.log') if 'stage=publish episode=ep_a' in p.read_text())
        self.assertIn('--defer-work-sync', log)
        for ep in ('ep_a', 'ep_b'):
            st = self.wait(lambda: (s := self.act('sync-status', {'episode': ep}))['state'] == 'success' and s)
            self.assertTrue(st['auto'])
            self.assertEqual(st['local_deleted'], ['x'])
        # 업로드는 한 번에 하나씩: begin/end가 서로 끼어들지 않습니다.
        order = [l.split()[0] for l in (self.tmp / 'dags/order.log').read_text().splitlines()]
        self.assertEqual(order, ['begin', 'end', 'begin', 'end'])

    def test_failed_auto_upload_is_retried_by_the_next_one(self):
        self.act('start', self.req('publish', 'ep_bad'))
        st = self.wait(lambda: (s := self.act('sync-status', {'episode': 'ep_bad'}))['state'] == 'failed' and s)
        self.assertEqual(self.act('status', self.req('publish', 'ep_bad'))['state'], 'success')   # 최종 저장 자체는 성공
        self.assertEqual((st['auto'], st['tries']), (True, 1))
        (self.tmp / 'dags/fixed').touch()
        self.act('start', self.req('publish', 'ep_d'))
        st = self.wait(lambda: (s := self.act('sync-status', {'episode': 'ep_bad'}))['state'] == 'success' and s)
        self.assertEqual(st['tries'], 2)
        self.wait(lambda: self.act('sync-status', {'episode': 'ep_d'})['state'] == 'success')

    def test_manual_upload_is_not_marked_auto(self):
        self.act('sync', {'episode': 'ep_m'})
        st = self.wait(lambda: (s := self.act('sync-status', {'episode': 'ep_m'}))['state'] == 'success' and s)
        self.assertNotIn('auto', st)


if __name__ == '__main__':
    unittest.main()

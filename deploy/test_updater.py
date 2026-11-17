"""GitHub 업데이트(repro/update_stack.py · fleet 업데이트 요청). 임시 git 저장소·임시 NAS 폴더만 사용하고,
실제 빌드·서비스 재시작(start_stack)은 가짜로 바꿉니다."""
import importlib.util, json, os, sqlite3, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code/platform'))
sys.path.insert(0, str(ROOT / 'repro'))
import fleet
import update_stack as us


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(subprocess.run, ['rm', '-rf', str(self.tmp)])
        self.origin, self.dev, self.pc = self.tmp / 'origin.git', self.tmp / 'dev', self.tmp / 'pc'
        self.nas = self.tmp / 'nas'; (self.nas / 'control').mkdir(parents=True)
        sh(self.tmp, 'git', 'init', '-q', '--bare', '-b', 'main', str(self.origin))
        sh(self.tmp, 'git', 'clone', '-q', str(self.origin), str(self.dev))
        for k, v in (('user.email', 't@t'), ('user.name', 't')):
            sh(self.dev, 'git', 'config', k, v)
        self.commit('README.md', 'v1')
        sh(self.tmp, 'git', 'clone', '-q', str(self.origin), str(self.pc))
        (self.pc / '.env').write_text(f"PC_ID='pc2'\nNAS_MOUNT='{self.nas}'\nGPU_STATE_DIR='{self.pc / 'gpu_state'}'\n")
        for p in (patch.object(us, 'ROOT', self.pc), patch.object(us, 'LOCAL', self.pc / 'state/updater'),
                  patch.dict(os.environ, {'KAIST_UPDATE_MODE': 'docker'})):
            p.start(); self.addCleanup(p.stop)
        (self.pc / 'state/updater').mkdir(parents=True)
        self.calls, self.fail_on = [], set()
        self.deployed = sh(self.pc, 'git', 'rev-parse', 'HEAD')
        us.write_json(us.LOCAL / 'state.json', {'built': self.deployed, 'deployed': self.deployed})
        self.ui(busy={'running_tis': 0, 'batch': False})

    def commit(self, name, text):
        f = self.dev / name; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(text)
        sh(self.dev, 'git', 'add', '-A'); sh(self.dev, 'git', 'commit', '-qm', f'{name}={text}'); sh(self.dev, 'git', 'push', '-q', 'origin', 'main')

    def ui(self, busy, inflight=0, age=0, stages=()):
        """이 PC UI가 NAS에 쓰는 상태 파일(필요한 부분만)."""
        path = self.nas / 'control/status/pc2.sqlite'; path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        c = sqlite3.connect(path)
        c.executescript(fleet.SCHEMA)
        c.executemany('INSERT INTO meta VALUES(?,?)', [('updated_at', json.dumps(time.time() - age)), ('update_busy', json.dumps(busy))])
        c.executemany('INSERT INTO runs (run_id,state) VALUES(?,?)', [(f'r{i}', 'running') for i in range(inflight)])
        c.executemany('INSERT INTO runs (run_id,state,stage) VALUES(?,?,?)', [(f's{i}', 'running', st) for i, st in enumerate(stages)])
        c.commit(); c.close()

    def job(self, state):
        d = self.pc / 'gpu_state/worker/jobs/abc'; d.mkdir(parents=True, exist_ok=True)
        (d / 'status.json').write_text(json.dumps({'state': state}))
        (d / 'request.json').write_text(json.dumps({'episode': 'ep_1', 'stage': 'track'}))

    def tick(self, **kw):
        u = us.Updater(self.pc / '.env')

        def fake(*flags):
            self.calls.append(flags)
            if flags in self.fail_on or (not flags and () in self.fail_on):
                raise RuntimeError('boom')
        u.start_stack = fake
        u.tick(**kw)
        return u.status

    def request(self):
        with patch.object(fleet, 'CONTROL', self.nas / 'control'):
            return fleet.update_request('pc2', 'admin@pc1')

    def head(self):
        return sh(self.pc, 'git', 'rev-parse', 'HEAD')

    def test_no_request_reports_behind_only(self):
        self.commit('code/a.py', '1')
        st = self.tick()
        self.assertEqual((st['state'], st['behind'], self.calls), ('idle', 1, []))
        self.assertEqual(self.head(), self.deployed)
        self.assertEqual(json.loads((self.nas / 'control/update/pc2/status.json').read_text())['behind'], 1)

    def test_request_builds_waits_for_jobs_then_restarts(self):
        self.commit('code/a.py', '1'); self.request(); self.job('running')
        st = self.tick()
        self.assertEqual(st['state'], 'waiting'); self.assertIn('GPU 작업 1개', st['message'])
        self.assertEqual(self.calls, [('--build-only', '--no-gpu-check')])
        self.assertNotEqual(self.head(), self.deployed)
        self.assertTrue((self.nas / 'control/update/pc2/request.json').exists())
        self.job('success')
        st = self.tick()
        self.assertEqual(st['state'], 'done'); self.assertEqual(self.calls, [('--build-only', '--no-gpu-check'), ('--gpu-check-only',), ('--skip-build',)])  # 다시 빌드하지 않음
        self.assertFalse((self.nas / 'control/update/pc2/request.json').exists())
        self.assertEqual(json.loads((us.LOCAL / 'state.json').read_text())['deployed'], self.head())

    def test_pipeline_change_waits_for_inflight_runs(self):
        self.commit('pipeline/current/project/00_scripts/x.py', '1'); self.request()
        self.ui(busy={'running_tis': 0, 'batch': False}, inflight=2)
        st = self.tick()
        self.assertEqual(st['state'], 'waiting'); self.assertIn('진행 중인 실행 2개', st['message'])
        # 코드만 바뀐 경우에는 실행 중(검수 대기 등)인 실행이 있어도 진행
        self.ui(busy={'running_tis': 0, 'batch': False}, inflight=0)
        self.assertEqual(self.tick()['state'], 'done')

    def test_reconstruction_code_change_waits_only_for_late_runs(self):
        # PhysTwin/data_process는 3D 복원 때 고정 → 마스크 검수 대기 실행은 기다리지 않음
        self.commit('pipeline/current/workspace/kaist-full/PhysTwin/data_process/data_process_pcd.py', '1'); self.request()
        self.ui(busy={'running_tis': 0, 'batch': False}, stages=['mask_approved', 'reconstruct'])
        st = self.tick()
        self.assertEqual(st['state'], 'waiting'); self.assertIn('3D 복원 이후 단계의 실행 1개', st['message'])
        self.ui(busy={'running_tis': 0, 'batch': False}, stages=['mask_approved', 'tracking_approved'])
        self.assertEqual(self.tick()['state'], 'done')

    def test_run_pinned_code_change_resumes_review_runs(self):
        self.commit('pipeline/current/workspace/kaist-full/PhysTwin/Mono/data_process/x.py', '1'); self.request()
        self.ui(busy={'running_tis': 0, 'batch': False}, stages=['mask_approved'])
        st = self.tick()
        self.assertEqual(st['state'], 'done')

    def test_pipeline_code_change_allows_review_waits(self):
        self.commit('pipeline/current/project/00_scripts/x.py', '1'); self.request()
        self.ui(busy={'running_tis': 0, 'batch': False}, stages=us.WAIT_STAGES)
        self.assertEqual(self.tick()['state'], 'done')

    def test_review_wait_does_not_hide_worker_or_airflow_activity(self):
        self.commit('pipeline/current/project/00_scripts/x.py', '1'); self.request()
        self.ui(busy={'running_tis': 1, 'batch': False}, stages=('mask_approved',))
        self.assertEqual(self.tick()['state'], 'waiting')
        self.ui(busy={'running_tis': 0, 'batch': False}, stages=('mask_approved',))
        self.job('running')
        self.assertEqual(self.tick()['state'], 'waiting')
        self.job('success')
        self.assertEqual(self.tick()['state'], 'done')

    def test_config_change_still_waits_for_review(self):
        self.commit('pipeline/current/workspace/kaist-full/config.env', 'A=2'); self.request()
        self.ui(busy={'running_tis': 0, 'batch': False}, stages=('mask_approved',))
        self.assertEqual(self.tick()['state'], 'waiting')

    def test_stale_ui_status_blocks_restart(self):
        self.commit('code/a.py', '1'); self.request(); self.ui(busy={}, age=600)
        st = self.tick()
        self.assertEqual(st['state'], 'waiting'); self.assertIn('UI 상태를 확인할 수 없습니다', st['message'])

    def test_build_failure_rolls_back(self):
        self.commit('code/a.py', '1'); self.request(); self.fail_on = {('--build-only', '--no-gpu-check')}
        st = self.tick()
        self.assertEqual(st['state'], 'failed'); self.assertIn('되돌', st['message'])
        self.assertEqual(self.head(), self.deployed)
        self.assertFalse((self.nas / 'control/update/pc2/request.json').exists())

    def test_restart_failure_rolls_back_and_restarts_old(self):
        self.commit('code/a.py', '1'); self.request(); self.fail_on = {('--skip-build',)}
        st = self.tick()
        self.assertEqual(st['state'], 'failed'); self.assertEqual(self.head(), self.deployed)
        self.assertEqual(self.calls[-1], ())   # 이전 커밋으로 전체 빌드·기동

    def test_local_edits_are_backed_up_and_overwritten(self):
        self.commit('README.md', 'v2'); self.request()
        (self.pc / 'README.md').write_text('local edit')
        st = self.tick()
        self.assertEqual(st['state'], 'done'); self.assertIn('직접 고친 파일 1개', st['message'])
        self.assertEqual(self.head(), sh(self.dev, 'git', 'rev-parse', 'HEAD'))
        self.assertEqual((self.pc / 'README.md').read_text(), 'v2'); self.assertFalse(st['dirty'])
        self.assertEqual(self.calls, [('--build-only', '--no-gpu-check'), ('--gpu-check-only',), ('--skip-build',)])
        backup = next((us.LOCAL / 'discarded').iterdir())
        self.assertIn('local edit', (backup / 'local.patch').read_text())
        self.assertEqual(json.loads((backup / 'info.json').read_text())['files'], ['README.md'])

    def test_local_edits_without_new_commit_rebuild_clean(self):
        self.request()
        (self.pc / 'README.md').write_text('local edit')
        st = self.tick()
        self.assertEqual(st['state'], 'done'); self.assertEqual((self.pc / 'README.md').read_text(), 'v1')
        self.assertEqual(self.calls, [('--build-only', '--no-gpu-check'), ('--gpu-check-only',), ('--skip-build',)])

    def test_diverged_commit_is_backed_up_and_reset(self):
        self.commit('README.md', 'v2'); self.request()
        for k, v in (('user.email', 't@t'), ('user.name', 't')):
            sh(self.pc, 'git', 'config', k, v)
        (self.pc / 'local.txt').write_text('x'); sh(self.pc, 'git', 'add', '-A'); sh(self.pc, 'git', 'commit', '-qm', 'local')
        local = self.head()
        st = self.tick()
        self.assertEqual(st['state'], 'done'); self.assertEqual(self.head(), sh(self.dev, 'git', 'rev-parse', 'HEAD'))
        self.assertFalse((self.pc / 'local.txt').exists())
        branches = sh(self.pc, 'git', 'branch', '--list', 'updater-backup/*', '--format=%(objectname)')
        self.assertEqual(branches, local)

    def test_cancel_while_waiting_pauses(self):
        self.commit('code/a.py', '1'); self.request(); self.job('running')
        self.assertEqual(self.tick()['state'], 'waiting')
        with patch.object(fleet, 'CONTROL', self.nas / 'control'):
            self.assertTrue(fleet.update_cancel('pc2'))
        self.job('success')
        self.assertEqual(self.tick()['state'], 'paused')
        self.assertEqual(self.calls, [('--build-only', '--no-gpu-check')])

    def test_up_to_date_request_finishes_without_restart(self):
        self.request()
        st = self.tick()
        self.assertEqual((st['state'], self.calls), ('done', []))

    def test_auto_mode(self):
        self.commit('code/a.py', '1')
        self.assertEqual(self.tick(auto=True)['state'], 'done')

    def test_gpu_check_waits_for_idle(self):
        # 2026-11-17 PC5: 작업 중에 GPU 점검을 돌려 10/12로 실패 → 점검은 쉬는 동안에만
        self.commit('code/a.py', '1'); self.request(); self.job('running')
        self.assertEqual(self.tick()['state'], 'waiting')
        self.assertNotIn(('--gpu-check-only',), self.calls)
        self.job('success')
        self.assertEqual(self.tick()['state'], 'done')
        self.assertEqual(self.calls, [('--build-only', '--no-gpu-check'), ('--gpu-check-only',), ('--skip-build',)])

    def test_gpu_check_failure_rolls_back_without_check(self):
        self.commit('code/a.py', '1'); self.request(); self.fail_on = {('--gpu-check-only',)}
        st = self.tick()
        self.assertEqual(st['state'], 'failed'); self.assertEqual(self.head(), self.deployed)
        self.assertEqual(self.calls[-1], ('--build-only', '--no-gpu-check'))

    def test_failed_auto_commit_is_not_retried(self):
        self.commit('code/a.py', '1'); self.fail_on = {('--gpu-check-only',)}
        self.assertEqual(self.tick(auto=True)['state'], 'failed')
        n = len(self.calls)
        self.assertEqual(self.tick(auto=True)['state'], 'idle'); self.assertEqual(len(self.calls), n)
        self.commit('code/a.py', '2'); self.fail_on = set()   # 새 커밋이 오면 다시 시도
        self.assertEqual(self.tick(auto=True)['state'], 'done')

    def test_native_mode_pulls_without_docker(self):
        self.commit('docs.md', '1'); self.request()
        with patch.dict(os.environ, {'KAIST_UPDATE_MODE': 'native'}):
            st = self.tick()
        self.assertEqual(st['state'], 'done'); self.assertEqual(self.calls, []); self.assertEqual(st['behind'], 0)


PLAIN = """==> [phase] build
#1 [internal] load build definition from Dockerfile
#1 DONE 0.0s
#5 [gpu-worker envs 1/3] FROM docker.io/nvidia/cuda
#5 CACHED
#6 [gpu-worker envs 2/3] RUN build_envs.sh sam
#6 12.31 Building wheel for sam3
#7 [ui airflow 1/2] COPY code/ /opt/kaist/
#7 DONE 0.4s
"""


class ProgressTests(unittest.TestCase):
    def test_plain_build_output(self):
        b = us.BuildProgress()
        phases = [b.feed(l) for l in PLAIN.splitlines()]
        self.assertEqual([x for x in phases if x], ['build'])
        snap = b.snapshot()
        g = {x['label']: x for x in snap['groups']}
        self.assertEqual((g['gpu-worker envs']['done'], g['gpu-worker envs']['total'], g['gpu-worker envs']['cached']), (1, 3, 1))
        self.assertEqual((g['ui airflow']['done'], g['ui airflow']['total']), (1, 2))
        self.assertEqual(snap['current'][0]['text'], 'RUN build_envs.sh sam')
        self.assertEqual(snap['current'][0]['last'], 'Building wheel for sam3')
        self.assertNotIn('==> [phase] build', snap['tail'])


class StreamingTests(UpdaterTests):
    """실제 start_stack 호출 경로: 가짜 repro/start_stack.py의 출력을 읽어 단계·빌드 진행을 NAS 상태에 기록합니다."""
    def fake_stack(self, fail=False):
        script = self.pc / 'repro/start_stack.py'; script.parent.mkdir(exist_ok=True)
        script.write_text('import sys\na = sys.argv\nif "--gpu-check-only" not in a: print(%r)\n'
                          'if "--no-gpu-check" not in a: print("==> [phase] check", flush=True); print("selfcheck 10/10")\n'
                          'sys.exit(%d)\n' % (PLAIN, 1 if fail else 0))

    def real_tick(self):
        u = us.Updater(self.pc / '.env'); u.tick(); return u

    def test_progress_recorded_through_phases(self):
        self.commit('code/a.py', '1'); self.request(); self.job('running'); self.fake_stack()
        u = self.real_tick()
        st = json.loads((self.nas / 'control/update/pc2/status.json').read_text())
        self.assertEqual(st['state'], 'waiting')
        run = st['run']
        self.assertEqual(list(run['phases']), ['fetch', 'build', 'wait'])
        self.assertTrue(all('end' in run['phases'][k] for k in ('fetch', 'build')))
        self.assertEqual(run['phase'], 'wait')
        self.assertEqual({g['label'] for g in run['build']['groups']}, {'gpu-worker envs', 'ui airflow'})
        self.assertEqual(st['phases'][1], ['build', '이미지 빌드'])
        self.job('success')
        u = self.real_tick()
        st = json.loads((self.nas / 'control/update/pc2/status.json').read_text())
        self.assertEqual(st['state'], 'done'); self.assertIsNone(st['run'])
        last = st['last']['run']
        self.assertTrue(last['ok']); self.assertIn('check', last['phases']); self.assertIn('restart', last['phases']); self.assertEqual(last['to'], self.head())

    def test_failed_build_keeps_failed_phase(self):
        self.commit('code/a.py', '1'); self.request(); self.fake_stack(fail=True)
        self.real_tick()
        st = json.loads((self.nas / 'control/update/pc2/status.json').read_text())
        self.assertEqual(st['state'], 'failed')
        last = st['last']['run']
        self.assertFalse(last['ok']); self.assertIn('rollback', last['phases'])


class FleetUpdateRequestTests(unittest.TestCase):
    def test_request_cancel_pending(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(fleet, 'CONTROL', Path(tmp)), patch.object(fleet, 'MACHINE_PC', 'pc3'):
            self.assertIsNone(fleet.update_pending())
            fleet.update_request('pc3', 'admin@pc1')
            self.assertEqual(fleet.update_pending()['requested_by'], 'admin@pc1')
            self.assertEqual(fleet.update_info('pc3')['status'], None)
            self.assertTrue(fleet.update_cancel('pc3')); self.assertFalse(fleet.update_cancel('pc3'))
            self.assertIsNone(fleet.update_pending())
            with self.assertRaises(ValueError):
                fleet.update_request('pc9', 'x')


class ReconstructFollowupTests(UpdaterTests):
    """코드가 바뀌어도 과거 복원 결과를 자동으로 재실행하지 않습니다."""

    def setUp(self):
        super().setUp()
        import hashlib
        self.work = self.tmp / 'work'
        code = self.pc / 'pipeline/current/workspace/kaist-full/PhysTwin/data_process'
        code.mkdir(parents=True)
        (code / 'align.py').write_text('new')
        good = hashlib.sha256(b'new').hexdigest()
        for ep, h in (('ep_old', hashlib.sha256(b'old').hexdigest()), ('ep_same', good)):
            c = self.work / '06_reconstruction' / ep / '_resume/contract.json'
            c.parent.mkdir(parents=True)
            c.write_text(json.dumps({'code': {'align': h}}))
        with (self.pc / '.env').open('a') as f:
            f.write(f"GPU_PROJECT_DIR='{self.work}'\n")

    def inbox(self):
        return sorted((self.nas / 'control/inbox/pc2').glob('*.json'))

    def test_update_does_not_enqueue_old_reconstructions(self):
        for _ in range(2):
            self.tick()
        self.assertEqual(self.inbox(), [])
        self.commit('pipeline/current/workspace/kaist-full/PhysTwin/data_process/other.py', 'newer')
        self.request()
        self.assertEqual(self.tick()['state'], 'done')
        self.tick()
        self.assertEqual(self.inbox(), [])



if __name__ == '__main__':
    unittest.main()

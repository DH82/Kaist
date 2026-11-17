"""단계 초기화 · 다시 실행: 끝난 실행의 한 단계부터 결과를 보관하고(삭제 없음) 다시 실행합니다.
임시 작업 폴더·가짜 NAS·가짜 Airflow/워커만 사용합니다."""
import hashlib, importlib.util, json, os, shutil, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'worker/app/dags'))
sys.path.insert(0, str(ROOT / 'code/platform'))
import stage_reset, sync_work
import kaist_runtime as rt

EP, RUN = 'ep_20261116_003115', 'ui_20261116T004016_264e1c'
KEY = hashlib.sha256(RUN.encode()).hexdigest()[:24]
ORDER = ['prepare_raw', 'sam_settings_ready', 'sam3', 'mask_approved', 'preflight', 'track', 'tracking_approved',
         'prepare_reconstruction', 'reconstruct', 'hands', 'validate', 'publish']


class Workspace:
    """가짜 파이프라인(00_scripts·kaist-full)·작업 폴더·NAS. identity.json은 실제 실행 고정과 같은 방식으로 만듭니다."""
    def __init__(self, base):
        b = Path(base)
        self.pipeline = b / 'revising/02_current'
        self.scripts = self.pipeline / 'project/00_scripts'
        self.project = b / 'kaist_work/pc1'
        self.nas = b / 'nas'
        self.scripts.mkdir(parents=True)
        shutil.copy2(ROOT / 'pipeline/current/project/00_scripts/stage_refs.py', self.scripts)
        (self.scripts / '18_fit_hands.py').write_text('# v1\n')
        (self.scripts / 'runtime.local.json').write_text(json.dumps({'pipeline_root': str(self.pipeline)}))
        mono = self.pipeline / 'kaist-full/PhysTwin/Mono/data_process'; mono.mkdir(parents=True)
        (mono / 'x.py').write_text('# mono\n')
        (self.pipeline / 'kaist-full/config.env').write_text('A=1\n')
        (self.nas / 'work/pc1').mkdir(parents=True)
        self.cfg = {'project_dir': str(self.project), 'scripts_dir': str(self.scripts), 'pipeline_root': str(self.pipeline),
                    'nas_mount': str(self.nas), 'nas_results_subdir': 'results/pc1'}
        for rel in ['03_prepared', '04_masks/approved', '05_tracking/approved', '06_reconstruction']:
            d = self.project / rel / EP; d.mkdir(parents=True); (d / 'out.txt').write_text(rel)
        (self.project / '06_reconstruction' / EP / 'hands').mkdir(); (self.project / '06_reconstruction' / EP / 'hands/mano.pkl').write_text('m')
        self.receipt = self.project / '_airflow' / EP / KEY; self.receipt.mkdir(parents=True)
        code = {str(p): rt.digest(p) for base in [self.scripts, mono] for p in sorted(base.rglob('*.py'))}
        self.identity = {'episode': EP, 'run_id': RUN, 'config': self.cfg, 'code': code,
                         'runtime': json.loads((self.scripts / 'runtime.local.json').read_text()),
                         'sam_request': {'prompt': 'towel', 'scene': '2d', 'confirmed': True},
                         'config_env_sha256': rt.digest(self.pipeline / 'kaist-full/config.env')}
        (self.receipt / 'identity.json').write_text(json.dumps(self.identity, indent=2))

    def publish(self):
        dest = self.nas / 'results/pc1/towel' / EP / KEY; dest.mkdir(parents=True); (dest / 'manifest.json').write_text('{}')
        (self.receipt / 'published.json').write_text(json.dumps({'destination': str(dest), 'storage': 'nas'}))
        return dest


class StageResetTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.w = Workspace(tmp.name)
        # 테스트용 NAS는 마운트 지점이 아니므로 마운트 확인만 통과시킵니다.
        p = patch.object(Path, 'is_mount', lambda self: True); p.start(); self.addCleanup(p.stop)
        self.log = []

    def reset(self, start):
        return stage_reset.reset_outputs(self.w.cfg, EP, RUN, start, self.log.append)

    def test_track_reset_archives_tracking_and_later_only(self):
        rec = self.reset('preflight')
        pj = self.w.project
        self.assertTrue((pj / '03_prepared' / EP).is_dir())
        self.assertTrue((pj / '04_masks/approved' / EP).is_dir())       # 승인된 마스크는 유지
        self.assertFalse((pj / '05_tracking/approved' / EP).exists())
        self.assertFalse((pj / '06_reconstruction' / EP).exists())
        archive = Path(rec['archive'])
        self.assertEqual((archive / '05_tracking/approved' / EP / 'out.txt').read_text(), '05_tracking/approved')
        self.assertTrue((archive / '06_reconstruction' / EP / 'hands/mano.pkl').is_file())
        self.assertTrue((archive / 'reset.json').is_file())

    def test_hands_reset_moves_only_hands(self):
        rec = self.reset('hands')
        self.assertTrue((self.w.project / '06_reconstruction' / EP / 'out.txt').is_file())
        self.assertFalse((self.w.project / '06_reconstruction' / EP / 'hands').exists())
        self.assertEqual(rec['moved'], [f'06_reconstruction/{EP}/hands'])

    def test_published_result_is_moved_to_nas_history(self):
        dest = self.w.publish()
        rec = self.reset('publish')
        self.assertFalse(dest.exists()); self.assertFalse(dest.parent.exists())   # 빈 <ep> 폴더도 정리
        moved = Path(rec['published_result']['to'])
        self.assertTrue((moved / 'manifest.json').is_file())
        self.assertTrue(moved.is_relative_to(self.w.nas / 'work/pc1/99_history/reset_results' / EP))
        self.assertFalse((self.w.receipt / 'published.json').exists())
        self.assertFalse(sync_work.published_to_nas(self.w.project, EP))      # 다시 저장 전에는 로컬 삭제 안 함
        self.assertEqual(rec['moved'], [])

    def test_purged_episode_is_restored_from_nas_first(self):
        nas = sync_work.work_root(self.w.cfg)
        for rel in [f'06_reconstruction/{EP}', f'_airflow/{EP}']:
            shutil.copytree(self.w.project / rel, nas / rel); shutil.rmtree(self.w.project / rel)
        (nas / '99_history/nas_sync').mkdir(parents=True)
        (nas / '99_history/nas_sync' / f'{EP}.json').write_text(json.dumps({'paths': [], 'local_deleted': [f'06_reconstruction/{EP}', f'_airflow/{EP}']}))
        rec = self.reset('hands')
        self.assertEqual(rec['restored_from_nas'], [f'06_reconstruction/{EP}', f'_airflow/{EP}'])
        self.assertTrue((self.w.project / '06_reconstruction' / EP / 'out.txt').is_file())
        self.assertFalse((self.w.project / '06_reconstruction' / EP / 'hands').exists())
        record = json.loads((nas / '99_history/nas_sync' / f'{EP}.json').read_text())
        self.assertEqual(record['local_deleted'], [])

    def test_code_change_blocks_partial_reset_without_changes(self):
        (self.w.scripts / '18_fit_hands.py').write_text('# v2\n')
        with self.assertRaisesRegex(RuntimeError, '18_fit_hands.py'):
            self.reset('hands')
        self.assertTrue((self.w.project / '06_reconstruction' / EP / 'hands').is_dir())   # 아무것도 옮기지 않음

    def test_code_change_full_reset_repins(self):
        (self.w.scripts / '18_fit_hands.py').write_text('# v2\n')
        rec = self.reset('prepare_raw')
        self.assertEqual(rec['repinned'], ['18_fit_hands.py'])
        pin = json.loads((self.w.receipt / 'identity.json').read_text())
        self.assertEqual(pin['code'][str(self.w.scripts / '18_fit_hands.py')], rt.digest(self.w.scripts / '18_fit_hands.py'))
        self.assertEqual(stage_reset.code_drift(self.w.cfg, pin)[1], [])
        self.assertFalse((self.w.project / '03_prepared' / EP).exists())

    def test_unknown_start_rejected(self):
        with self.assertRaises(ValueError):
            self.reset('mask_approved')


class RunnerResetTests(unittest.TestCase):
    """runner.reset_run: 결과 보관 후 시작 단계부터의 작업을 시작 전으로 되돌리고 이전 기록을 attempts/에 둡니다."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.w = Workspace(tmp.name)
        p = patch.object(Path, 'is_mount', lambda self: True); p.start(); self.addCleanup(p.stop)
        spec = importlib.util.spec_from_file_location('runner_under_test', ROOT / 'worker/app/worker/runner.py')
        self.r = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.r)
        self.r.JOBS = Path(tmp.name) / 'jobs'; self.r.RESET = self.r.JOBS / '_reset'
        self.r.CONFIG = Path(tmp.name) / 'cfg.json'; self.r.CONFIG.write_text(json.dumps(self.w.cfg))
        for stage in ORDER:
            d = self.r._job_dir(RUN, EP, stage); d.mkdir(parents=True)
            self.r.write(d / 'request.json', {'stage': stage, 'episode': EP, 'run_id': RUN})
            self.r.write(d / 'status.json', {'state': 'success', 'stage': stage})

    def run_reset(self, start):
        req = {'episode': EP, 'run_id': RUN, 'start': start}
        d = self.r._reset_dir(req); d.mkdir(parents=True)
        self.r.write(d / 'request.json', req); self.r.write(d / 'status.json', {'state': 'running', **req, 'started': 0})
        self.r.reset_run(d)
        return json.loads((d / 'status.json').read_text())

    def state(self, stage):
        return json.loads((self.r._job_dir(RUN, EP, stage) / 'status.json').read_text())['state']

    def test_rewinds_from_start(self):
        st = self.run_reset('preflight')
        self.assertEqual(st['state'], 'success', st)
        self.assertEqual(st['rewound'], ORDER[ORDER.index('preflight'):])
        self.assertEqual(self.state('mask_approved'), 'success')
        self.assertEqual(self.state('track'), 'not_started')
        self.assertEqual(len(list((self.r._job_dir(RUN, EP, 'track') / 'attempts').iterdir())), 1)

    def test_running_job_aborts_without_changes(self):
        d = self.r._job_dir(RUN, EP, 'hands')
        self.r.write(d / 'status.json', {'state': 'running', 'stage': 'hands', 'created': 9e12, 'pid': os.getpid()})
        with patch.object(self.r, 'status', lambda p: json.loads((p / 'status.json').read_text())):
            st = self.run_reset('preflight')
        self.assertEqual(st['state'], 'failed')
        self.assertEqual(self.state('track'), 'success')
        self.assertTrue((self.w.project / '05_tracking/approved' / EP).is_dir())

    def test_reset_start_rejects_gate_stage(self):
        with self.assertRaises(ValueError):
            self.r.reset_start({'episode': EP, 'run_id': RUN, 'start': 'tracking_approved'})


class ResetEndpointTests(unittest.TestCase):
    """UI /api/jobs/reset: 검사 → Worker 초기화 → 끝나면 Airflow 단계를 시작 단계부터 다시 실행."""
    DAG = 'kaist_pc1_reconstruction'

    def setUp(self):
        import access, fleet
        import platform_app as ui
        from fastapi.testclient import TestClient
        self.ui = ui
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, KAIST_INTAKE_DB=str(Path(tmp.name) / 'c.sqlite'), KAIST_AIRFLOW_API_URL='')
        env.start(); self.addCleanup(env.stop)
        self.run_state = 'success'
        self.tasks = [{'task_id': t, 'state': 'success'} for t in ORDER]
        self.cleared = []; self.worker = []; self.worker_states = {}
        def local_api(method, path, **kw):
            if path.endswith('/taskInstances'): return {'task_instances': self.tasks}
            if 'clearTaskInstances' in path: self.cleared.append((kw['json']['dry_run'], kw['json']['task_ids'])); return {}
            if path.endswith('/dagRuns'): return {'dag_runs': []}
            return {'state': self.run_state, 'conf': {'episode_id': EP, 'operator_id': 'PC1'}}
        def call(method, path, json=None, **kw):
            action = path.rsplit('/', 1)[1]
            if action == 'status': return {'state': self.worker_states.get(json['stage'], 'not_started')}
            self.worker.append((action, json))
            return {'state': 'success' if action == 'reset-status' else 'running'}
        for obj, name, value in [(ui, 'UI_PASSWORD_FILE', ''), (ui, 'ALLOWED_HOSTS', {'testserver'}), (fleet, 'CONTROL', Path(tmp.name) / 'control'),
                                 (ui, 'RESETS', {})]:
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)
        for name, fn in [('local_api', local_api), ('call', call), ('active_dags', lambda: [('gpu_4090_a', 'full', self.DAG)]),
                         ('dag_worker', lambda d: 'gpu_4090_a'), ('active_batch_sources', lambda **kw: []), ('update_block', lambda: None),
                         ('spawn_reset', lambda *a: ui._finish_reset(*a))]:
            p = patch.object(ui, name, side_effect=fn); p.start(); self.addCleanup(p.stop)
        self.c = TestClient(ui.app); self.c.cookies.set('kaist_session', access.login('PC1', 'operator'))
        self.h = {'Origin': 'http://testserver', 'X-Platform-CSRF': ui.CSRF}

    def reset(self, stage):
        return self.c.post('/api/jobs/reset', headers=self.h, json={'dag_id': self.DAG, 'run_id': RUN, 'stage': stage})

    def test_reconstruct_resets_from_prepare_reconstruction(self):
        r = self.reset('reconstruct')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['stage'], 'prepare_reconstruction')
        self.assertEqual(self.worker[0], ('reset', {'episode': EP, 'run_id': RUN, 'start': 'prepare_reconstruction'}))
        self.assertEqual(self.cleared, [(True, ORDER[7:]), (False, ORDER[7:])])
        self.assertEqual(self.ui.RESETS[RUN]['state'], 'success')

    def test_running_run_is_refused(self):
        self.run_state = 'running'
        self.assertEqual(self.reset('track').status_code, 409)
        self.assertFalse(self.worker)

    def test_gate_stage_is_refused(self):
        self.assertEqual(self.reset('mask_approved').status_code, 400)

    def test_stage_after_failure_is_refused(self):
        self.run_state = 'failed'
        self.tasks[ORDER.index('track')]['state'] = 'failed'
        for t in self.tasks[ORDER.index('track') + 1:]: t['state'] = 'upstream_failed'
        self.assertEqual(self.reset('hands').status_code, 409)
        self.assertEqual(self.reset('sam3').status_code, 200)

    def test_day_run_mano_reset_only_rewinds_worker(self):
        self.tasks = self.tasks[:ORDER.index('hands')]
        self.worker_states = {'hands': 'success', 'validate': 'success', 'publish': 'success'}
        r = self.reset('hands')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()['airflow']); self.assertEqual(self.cleared, [])
        self.assertEqual(self.ui.RESETS[RUN]['message'], 'MANO 대기로 되돌림')

    def test_failed_worker_reset_does_not_clear_airflow(self):
        orig = self.ui.call.side_effect
        def call(method, path, json=None, **kw):
            if path.endswith('reset-status'): return {'state': 'failed', 'error': '코드 변경'}
            return orig(method, path, json=json, **kw)
        self.ui.call.side_effect = call
        self.assertEqual(self.reset('track').status_code, 200)
        self.assertEqual(self.cleared, [])
        self.assertEqual(self.ui.RESETS[RUN]['state'], 'failed')


if __name__ == '__main__':
    unittest.main()

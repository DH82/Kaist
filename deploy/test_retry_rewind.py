"""재시도: 마스크를 고쳐 다시 승인한 뒤 3D 복원이 '추적 입력이 현재 승인본과 다릅니다'로 실패하면,
실패 단계만 다시 하지 않고 마스크 승인 확인 단계부터 다시 실행합니다(추적 → 추적 검수 → 3D 복원).
가짜 Airflow·워커와 임시 DB·임시 작업 폴더만 사용합니다."""
import importlib.util, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code/platform'))
import access, fleet
import platform_app as ui
from fastapi.testclient import TestClient

DAG, RUN, EP = 'kaist_pc1_reconstruction', 'ui_x', 'ep_20261116_042614'
ORDER = ['prepare_raw', 'sam_settings_ready', 'sam3', 'mask_approved', 'preflight', 'track', 'tracking_approved',
         'prepare_reconstruction', 'reconstruct', 'hands', 'validate', 'publish']
STALE = '추적 입력이 현재 승인본과 다릅니다. 다시 준비하세요.'


def tasks_failed_at(stage):
    i = ORDER.index(stage)
    return [{'task_id': t, 'state': 'success' if j < i else 'failed' if j == i else 'upstream_failed'}
            for j, t in enumerate(ORDER)]


class RetryRewindTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, KAIST_INTAKE_DB=str(Path(tmp.name) / 'c.sqlite'), KAIST_AIRFLOW_API_URL='')
        env.start(); self.addCleanup(env.stop)
        self.tasks = tasks_failed_at('reconstruct')
        self.review = [{'episode': EP, 'stage': 'tracking', 'status': 'needs_review', 'ready': False, 'reason': STALE}]
        self.cleared = []; self.worker = []
        self.states = {t: 'success' for t in ORDER[:ORDER.index('reconstruct')]} | {'reconstruct': 'failed'}
        def local_api(method, path, **kw):
            if path.endswith('/taskInstances'): return {'task_instances': self.tasks}
            if 'clearTaskInstances' in path: self.cleared.append((kw['json']['dry_run'], kw['json']['task_ids'])); return {}
            if path.endswith('/dagRuns'): return {'dag_runs': []}
            return {'state': 'failed', 'conf': {'episode_id': EP, 'operator_id': 'PC1'}, 'logical_date': '2026'}
        def call(method, path, json=None, **kw):
            if path.endswith('/status'): return {'state': self.states.get(json['stage'], 'not_started')}
            self.worker.append((path.rsplit('/', 1)[1], json['stage'])); return {}
        for obj, name, value in [(ui, 'UI_PASSWORD_FILE', ''), (ui, 'ALLOWED_HOSTS', {'testserver'}), (fleet, 'CONTROL', Path(tmp.name) / 'control')]:
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)
        for name, fn in [('local_api', local_api), ('call', call), ('active_dags', lambda: [('gpu_4090_a', 'day', DAG)]),
                         ('dag_worker', lambda d: 'gpu_4090_a'), ('all_review_items', lambda wid=None: self.review)]:
            p = patch.object(ui, name, side_effect=fn); p.start(); self.addCleanup(p.stop)
        self.c = TestClient(ui.app); self.c.cookies.set('kaist_session', access.login('PC1', 'operator'))
        self.h = {'Origin': 'http://testserver', 'X-Platform-CSRF': ui.CSRF}

    def retry(self, stage='reconstruct'):
        return self.c.post('/api/jobs/retry', headers=self.h, json={'dag_id': DAG, 'run_id': RUN, 'stage': stage})

    def test_stale_tracking_rewinds_to_mask_approval(self):
        r = self.retry()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['stage'], 'mask_approved')
        self.assertEqual(r.json()['requested_stage'], 'reconstruct')
        cleared = self.cleared[-1]
        self.assertEqual(cleared[0], False)
        self.assertEqual(cleared[1], ORDER[ORDER.index('mask_approved'):])        # 마스크 승인 확인부터 끝까지
        rewound = [s for a, s in self.worker if a == 'rewind']
        self.assertEqual(rewound, ['mask_approved', 'preflight', 'track', 'tracking_approved', 'prepare_reconstruction'])
        # 실패 단계와 그 뒤 단계는 기존 retry 그대로
        self.assertEqual([s for a, s in self.worker if a == 'retry'], ['reconstruct', 'hands', 'validate', 'publish'])

    def test_waiting_gate_is_reattached_not_rewound(self):
        self.states['tracking_approved'] = 'waiting'
        r = self.retry()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(('rewind', 'tracking_approved'), self.worker)

    def test_fresh_tracking_keeps_failed_stage(self):
        self.review = [{'episode': EP, 'stage': 'tracking', 'status': 'approved', 'ready': True}]
        r = self.retry()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['stage'], 'reconstruct')
        self.assertEqual(self.cleared[-1][1], ORDER[ORDER.index('reconstruct'):])
        self.assertFalse([s for a, s in self.worker if a == 'rewind'])

    def test_other_tracking_problem_does_not_rewind(self):
        self.review[0]['reason'] = '자동 QC 구조 오류: qc/report.json 확인 후 재추적하세요.'
        r = self.retry()
        self.assertEqual(r.json()['stage'], 'reconstruct')

    def test_failure_before_tracking_is_unchanged(self):
        self.tasks = tasks_failed_at('track'); self.states['track'] = 'failed'
        r = self.retry('track')
        self.assertEqual(r.json()['stage'], 'track')
        self.assertFalse([s for a, s in self.worker if a == 'rewind'])

    def test_running_earlier_task_blocks_rewind(self):
        self.tasks[ORDER.index('track')]['state'] = 'running'
        r = self.retry()
        self.assertEqual(r.status_code, 409)


class RunnerRewindTests(unittest.TestCase):
    """GPU 워커 runner.rewind: 끝난 작업(성공 포함)을 시작 전으로 되돌리고 이전 기록을 보관합니다."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        spec = importlib.util.spec_from_file_location('runner_under_test', ROOT / 'worker/app/worker/runner.py')
        self.r = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.r)
        self.r.JOBS = Path(tmp.name) / 'jobs'
        self.req = {'stage': 'track', 'episode': EP, 'run_id': RUN}
        self.p = self.r.folder(self.req); self.p.mkdir(parents=True)
        self.r.write(self.p / 'request.json', self.req)

    def state(self, value):
        self.r.write(self.p / 'status.json', {'state': value, 'stage': 'track', 'created': 0})

    def test_success_job_is_rewound_and_archived(self):
        self.state('success'); (self.p / 'job.log').write_text('old log')
        st = self.r.rewind(self.req)
        self.assertEqual(st['state'], 'not_started'); self.assertEqual(st['rewound_from'], 'success')
        self.assertEqual(json.loads((self.p / 'status.json').read_text())['state'], 'not_started')
        archived = list((self.p / 'attempts').iterdir())
        self.assertEqual(len(archived), 1)
        self.assertEqual((archived[0] / 'job.log').read_text(), 'old log')

    def test_running_job_is_refused(self):
        self.r.write(self.p / 'status.json', {'state': 'running', 'stage': 'track', 'created': 9e18})
        with self.assertRaises(ValueError):
            self.r.rewind(self.req)

    def test_changed_request_is_refused(self):
        self.state('success')
        with self.assertRaises(ValueError):
            self.r.rewind({**self.req, 'sam_prompt': 'cup'})

    def test_retry_still_refuses_success(self):
        self.state('success')
        with self.assertRaises(ValueError):
            self.r.retry(self.req)


if __name__ == '__main__':
    unittest.main()

"""재시도: 워커 작업이 '검수 승인 대기(waiting)'면 워커 재시도를 요청하지 않고 Airflow만 다시 연결합니다.
워커가 거부하면 실제 사유를 보여 줍니다. 가짜 Airflow·워커와 임시 DB만 사용합니다."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code/platform'))
import access, fleet
import platform_app as ui
from fastapi.testclient import TestClient

DAG, RUN, EP = 'kaist_pc1_reconstruction', 'ui_x', 'ep_20261116_003217'
TASKS = [{'task_id': 'sam3', 'state': 'success'}, {'task_id': 'mask_approved', 'state': 'failed'},
         {'task_id': 'preflight', 'state': 'upstream_failed'}, {'task_id': 'track', 'state': 'upstream_failed'}]


class RetryWaitingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, KAIST_INTAKE_DB=str(Path(tmp.name) / 'c.sqlite'), KAIST_AIRFLOW_API_URL='')
        env.start(); self.addCleanup(env.stop)
        self.cleared = []; self.worker_retry = []; self.states = {'mask_approved': 'waiting'}
        def local_api(method, path, **kw):
            if path.endswith('/taskInstances'): return {'task_instances': TASKS}
            if 'clearTaskInstances' in path: self.cleared.append(kw['json']['dry_run']); return {}
            if path.endswith('/dagRuns'): return {'dag_runs': []}
            return {'state': 'failed', 'conf': {'episode_id': EP, 'operator_id': 'PC1'}, 'logical_date': '2026'}
        def call(method, path, json=None, **kw):
            if path.endswith('/status'): return {'state': self.states.get(json['stage'], 'not_started')}
            self.worker_retry.append(json['stage'])
            if self.states.get(json['stage']) == 'refuse':
                raise httpx.HTTPStatusError('409', request=httpx.Request('POST', 'http://w'),
                                            response=httpx.Response(409, json={'detail': 'Traceback...\nValueError: Retry request differs from original'}))
            return {}
        for obj, name, value in [(ui, 'UI_PASSWORD_FILE', ''), (ui, 'ALLOWED_HOSTS', {'testserver'}), (fleet, 'CONTROL', Path(tmp.name) / 'control')]:
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)
        for name, fn in [('local_api', local_api), ('call', call), ('active_dags', lambda: [('gpu_4090_a', 'day', DAG)]),
                         ('dag_worker', lambda d: 'gpu_4090_a')]:
            p = patch.object(ui, name, side_effect=fn); p.start(); self.addCleanup(p.stop)
        self.c = TestClient(ui.app); self.c.cookies.set('kaist_session', access.login('PC1', 'operator'))
        self.h = {'Origin': 'http://testserver', 'X-Platform-CSRF': ui.CSRF}

    def retry(self):
        return self.c.post('/api/jobs/retry', headers=self.h, json={'dag_id': DAG, 'run_id': RUN, 'stage': 'mask_approved'})

    def test_waiting_job_is_reattached_without_worker_retry(self):
        r = self.retry()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn('mask_approved', self.worker_retry)          # 승인 대기 작업은 건드리지 않음
        self.assertEqual(self.worker_retry, ['preflight', 'track'])   # 시작 전 단계는 기존대로
        self.assertEqual(self.cleared, [True, False])                 # Airflow 다시 연결

    def test_worker_refusal_shows_reason(self):
        self.states['mask_approved'] = 'refuse'
        r = self.retry()
        self.assertEqual(r.status_code, 409)
        self.assertIn('Retry request differs from original', r.json()['detail'])
        self.assertEqual(self.cleared, [True])                        # 실제 clear는 하지 않음


if __name__ == '__main__':
    unittest.main()

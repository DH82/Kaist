"""재시도: 3D 복원 뒤 단계(MANO·결과 검사·최종 저장)가 '3D 입력/코드/설정 계약 변경'으로 실패하면
(예: 파이프라인 업데이트로 kaist-full/PhysTwin/data_process/align.py가 바뀜) 실패 단계만 다시 하지 않고
3D 복원부터 다시 실행합니다. 3D 복원은 --auto-new-attempt로 기존 결과를 이력에 보관하고 새로 만듭니다.
가짜 Airflow·워커와 임시 DB만 사용합니다."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code/platform'))
import access, fleet
import platform_app as ui
from fastapi.testclient import TestClient

DAG, RUN, EP = 'kaist_pc2_reconstruction', 'ui_y', 'ep_20261116_042500'
ORDER = ['prepare_raw', 'sam_settings_ready', 'sam3', 'mask_approved', 'preflight', 'track', 'tracking_approved',
         'prepare_reconstruction', 'reconstruct', 'hands', 'validate', 'publish']
STALE_LOG = ('WORKER stage=hands episode=%s run=%s gpu_slot=0\n'
             '중단: 3D 입력/코드/설정 계약 변경: 기존 결과는 보존하고 10_reconstruct.py --new-attempt로 재생성하세요.\n'
             'subprocess.CalledProcessError: ... returned non-zero exit status 1.\n') % (EP, RUN)


def tasks_failed_at(stage):
    i = ORDER.index(stage)
    return [{'task_id': t, 'state': 'success' if j < i else 'failed' if j == i else 'upstream_failed'}
            for j, t in enumerate(ORDER)]


class RetryReconstructTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, KAIST_INTAKE_DB=str(Path(tmp.name) / 'c.sqlite'), KAIST_AIRFLOW_API_URL='')
        env.start(); self.addCleanup(env.stop)
        self.tasks = tasks_failed_at('hands')
        self.log = STALE_LOG
        self.cleared = []; self.worker = []
        self.states = {t: 'success' for t in ORDER[:ORDER.index('hands')]} | {'hands': 'failed'}
        def local_api(method, path, **kw):
            if path.endswith('/taskInstances'): return {'task_instances': self.tasks}
            if 'clearTaskInstances' in path: self.cleared.append((kw['json']['dry_run'], kw['json']['task_ids'])); return {}
            if path.endswith('/dagRuns'): return {'dag_runs': []}
            return {'state': 'failed', 'conf': {'episode_id': EP, 'operator_id': 'PC2'}, 'logical_date': '2026'}
        def call(method, path, json=None, **kw):
            if path.endswith('/status'): return {'state': self.states.get(json['stage'], 'not_started')}
            if path == '/worker/log':
                if self.log is None: raise RuntimeError('worker offline')
                return {'log': self.log if json['stage'] == 'hands' else ''}
            self.worker.append((path.rsplit('/', 1)[1], json['stage'])); return {}
        for obj, name, value in [(ui, 'UI_PASSWORD_FILE', ''), (ui, 'ALLOWED_HOSTS', {'testserver'}), (fleet, 'CONTROL', Path(tmp.name) / 'control')]:
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)
        for name, fn in [('local_api', local_api), ('call', call), ('active_dags', lambda: [('gpu_4090_a', 'day', DAG)]),
                         ('dag_worker', lambda d: 'gpu_4090_a'), ('all_review_items', lambda wid=None: [])]:
            p = patch.object(ui, name, side_effect=fn); p.start(); self.addCleanup(p.stop)
        self.c = TestClient(ui.app); self.c.cookies.set('kaist_session', access.login('PC2', 'operator'))
        self.h = {'Origin': 'http://testserver', 'X-Platform-CSRF': ui.CSRF}

    def retry(self, stage='hands'):
        return self.c.post('/api/jobs/retry', headers=self.h, json={'dag_id': DAG, 'run_id': RUN, 'stage': stage})

    def test_stale_3d_contract_rewinds_to_reconstruct(self):
        r = self.retry()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['stage'], 'reconstruct')
        self.assertEqual(r.json()['requested_stage'], 'hands')
        self.assertEqual(self.cleared[-1], (False, ORDER[ORDER.index('reconstruct'):]))
        self.assertEqual([s for a, s in self.worker if a == 'rewind'], ['reconstruct'])
        self.assertEqual([s for a, s in self.worker if a == 'retry'], ['hands', 'validate', 'publish'])

    def test_only_last_attempt_counts(self):
        # 이전 시도의 '계약 변경' 문구가 남아 있어도, 마지막 시도가 다른 오류면 실패 단계만 다시 합니다.
        self.log = STALE_LOG + 'WORKER stage=hands episode=%s run=%s gpu_slot=0\nCUDA out of memory\n' % (EP, RUN)
        r = self.retry()
        self.assertEqual(r.json()['stage'], 'hands')
        self.assertFalse([s for a, s in self.worker if a == 'rewind'])

    def test_other_failure_keeps_failed_stage(self):
        self.log = 'WORKER stage=hands episode=%s run=%s gpu_slot=0\nFileNotFoundError: gcc\n' % (EP, RUN)
        r = self.retry()
        self.assertEqual(r.json()['stage'], 'hands')
        self.assertFalse([s for a, s in self.worker if a == 'rewind'])

    def test_worker_log_unavailable_keeps_failed_stage(self):
        self.log = None
        r = self.retry()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['stage'], 'hands')

    def test_validate_failure_also_rewinds(self):
        self.tasks = tasks_failed_at('validate'); self.states |= {'hands': 'success', 'validate': 'failed'}
        self.log = STALE_LOG.replace('stage=hands', 'stage=validate')
        def call_validate(method, path, json=None, **kw):
            if path.endswith('/status'): return {'state': self.states.get(json['stage'], 'not_started')}
            if path == '/worker/log': return {'log': self.log if json['stage'] == 'validate' else ''}
            self.worker.append((path.rsplit('/', 1)[1], json['stage'])); return {}
        with patch.object(ui, 'call', side_effect=call_validate):
            r = self.retry('validate')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['stage'], 'reconstruct')
        self.assertEqual([s for a, s in self.worker if a == 'rewind'], ['reconstruct', 'hands'])


if __name__ == '__main__':
    unittest.main()

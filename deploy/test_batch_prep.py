"""RAW 전처리 일괄 실행 → SAM3 설정 대기 → 분할 대상 저장. 가짜 Airflow·워커와 임시 DB/NAS 폴더만 사용."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code/platform'))
import access, fleet
import platform_app as ui
from fastapi.testclient import TestClient

DAG = 'kaist_direct_pipeline_4090'
EPS = [f'ep_20261116_10000{i}' for i in range(1, 6)]


class FakeAirflow:
    def __init__(self):
        self.runs = {}      # run_id -> {'state','conf','tasks':{stage:state}}
        self.cleared = []

    def api(self, method, path, **kw):
        if path.startswith('/api/v2/pools'):
            return {'slots': 6}
        if method == 'PATCH':
            return {}
        if 'clearTaskInstances' in path:
            self.cleared.append(kw['json']['task_ids']); return {}
        if path.endswith('/dagRuns') and method == 'POST':
            j = kw['json']; self.runs[j['dag_run_id']] = {'state': 'running', 'conf': j['conf'], 'tasks': {'prepare_raw': 'running'}}
            return {'dag_run_id': j['dag_run_id'], 'state': 'queued'}
        if path.endswith('/dagRuns'):
            want = (kw.get('params') or {}).get('state')
            rows = [{'dag_run_id': k, 'state': v['state'], 'conf': v['conf'], 'logical_date': k} for k, v in self.runs.items()]
            return {'dag_runs': [r for r in rows if not want or r['state'] in want]}
        if '/taskInstances/' in path:
            rid, stage = path.split('/dagRuns/')[1].split('/taskInstances/')
            return {'state': self.runs[rid]['tasks'].get(stage)}
        if path.endswith('/taskInstances'):
            rid = path.split('/dagRuns/')[1].split('/')[0]
            return {'task_instances': [{'task_id': k, 'state': v} for k, v in self.runs[rid]['tasks'].items()]}
        if '/dagRuns/' in path:
            rid = path.split('/dagRuns/')[1]; v = self.runs[rid]
            return {'dag_run_id': rid, 'state': v['state'], 'conf': v['conf']}
        raise AssertionError(path)


class BatchPrepTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, KAIST_INTAKE_DB=str(Path(tmp.name) / 'c.sqlite'), KAIST_AIRFLOW_API_URL='')
        env.start(); self.addCleanup(env.stop)
        self.af = FakeAirflow(); self.saved = []; self.prepared = set()
        def call(method, path, json=None, **kw):
            if path == '/worker/info': return {'episodes': EPS, 'intake_manifests': {}}
            if path.startswith('/worker/outputs/catalog'): return {'groups': []}
            if path.endswith('/status'): return {'state': 'success' if json['stage'] == 'prepare_raw' and json['run_id'] in self.prepared else 'not_started'}
            raise AssertionError(path)
        def worker_action(action, payload, wid):
            self.saved.append((action, payload)); return {'ok': True, 'settings': {'prompt': payload['prompt']}}
        for obj, name, value in [(ui, 'UI_PASSWORD_FILE', ''), (ui, 'ALLOWED_HOSTS', {'testserver'}), (fleet, 'CONTROL', Path(tmp.name) / 'control')]:
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)
        for name, fn in [('local_api', self.af.api), ('call', call), ('worker_action', worker_action),
                         ('active_dags', lambda: [('gpu_4090_a', 'pipeline', DAG)]), ('dag_worker', lambda d: 'gpu_4090_a'),
                         ('worker', lambda *a, **k: {'dag_id': DAG, 'pool': 'gpu_4090_a', 'label': 'PC 1', 'enabled': True})]:
            p = patch.object(ui, name, side_effect=fn); p.start(); self.addCleanup(p.stop)
        self.c = TestClient(ui.app); self.c.cookies.set('kaist_session', access.login('PC1', 'admin'))
        self.h = {'Origin': 'http://testserver', 'X-Platform-CSRF': ui.CSRF}

    def post(self, path, body):
        return self.c.post(path, headers=self.h, json=body)

    def test_batch_starts_all_without_prompt_beyond_limit(self):
        r = self.post('/api/run/batch-prep', {'episodes': EPS[:4]}).json()
        self.assertEqual([x['raw'] for x in r['started']], EPS[:4]); self.assertEqual(r['failed'], [])
        for run in self.af.runs.values():
            self.assertEqual((run['conf']['sam_prompt'], run['conf']['sam_settings_confirmed']), ('', False))

    def test_waiting_runs_do_not_block_normal_launch(self):
        self.post('/api/run/batch-prep', {'episodes': EPS[:3]})
        for run in self.af.runs.values():   # 모두 전처리 끝나고 SAM3 설정 대기
            run['tasks'] = {'prepare_raw': 'success', 'sam_settings_ready': 'up_for_reschedule'}
        body = {'mode': 'pipeline', 'episode_id': EPS[4], 'sam_prompt': 'towel', 'confirmed': True, 'preview_episode': EPS[4]}
        self.assertEqual(self.post('/api/run', body).status_code, 200)
        # 계산 중인 실행 2개가 있으면 일반 실행은 막힘
        for run in list(self.af.runs.values())[:2]:
            run['tasks'] = {'prepare_raw': 'success', 'sam_settings_ready': 'success', 'sam3': 'running'}
        body2 = dict(body, episode_id=EPS[3], preview_episode=EPS[3])
        self.assertEqual(self.post('/api/run', body2).status_code, 409)

    def test_sam_waiting_list_and_save(self):
        self.post('/api/run/batch-prep', {'episodes': EPS[:2]})
        rid = list(self.af.runs)[0]
        for run in self.af.runs.values():
            run['tasks'] = {'prepare_raw': 'success', 'sam_settings_ready': 'up_for_reschedule'}
        self.prepared.add(rid)
        runs = self.c.get('/api/run/sam-waiting').json()['runs']
        self.assertEqual(len(runs), 2); self.assertTrue(next(x for x in runs if x['run_id'] == rid)['prepared'])
        r = self.post('/api/run/sam-settings', {'dag_id': DAG, 'run_id': rid, 'sam_prompt': ' towel '})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.saved, [('sam-settings', {'episode': self.af.runs[rid]['conf']['episode_id'], 'prompt': 'towel'})])
        self.assertIn(['sam_settings_ready'], self.af.cleared)          # 바로 다시 확인
        other = list(self.af.runs)[1]                                     # 전처리 안 끝난 실행은 거부
        self.assertEqual(self.post('/api/run/sam-settings', {'dag_id': DAG, 'run_id': other, 'sam_prompt': 'cup'}).status_code, 409)


if __name__ == '__main__':
    unittest.main()

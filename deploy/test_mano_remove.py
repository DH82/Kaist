"""MANO 일괄 실행 대기열에서 에피소드 하나 제거. 격리된 DB와 가짜 Airflow/Worker만 사용합니다."""
import os, tempfile, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code/platform'))
import access
import fleet
import platform_app as ui
from fastapi.testclient import TestClient

SRC = [{'run_id': 'day_a', 'conf': {'episode_id': 'ep_20261116_000001'}},
       {'run_id': 'day_b', 'conf': {'episode_id': 'ep_20261116_000002'}}]
BATCH = {'dag_run_id': 'mano_x', 'state': 'running', 'conf': {'sources': SRC}}


class ManoRemoveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, KAIST_INTAKE_DB=str(Path(tmp.name) / 'c.sqlite'), KAIST_AIRFLOW_API_URL='')
        env.start(); self.addCleanup(env.stop)
        self.status = {('day_a', 'hands'): 'running', ('day_b', 'hands'): 'not_started'}
        self.actions = []
        def call(method, path, json=None, **kw):
            return {'state': self.status.get((json['run_id'], json['stage']), 'not_started')}
        def action(name, payload, wid):
            self.actions.append((name, payload.get('run_id'), payload.get('stage')))
            return {'stopped': ['hands']} if name == 'stop' and payload['run_id'] == 'day_a' else {'stopped': []}
        # 실제 NAS(control/) 대신 임시 폴더
        _ctl = tempfile.TemporaryDirectory(); self.addCleanup(_ctl.cleanup)
        _p = patch.object(fleet, 'CONTROL', Path(_ctl.name) / 'control'); _p.start(); self.addCleanup(_p.stop)
        for name, value in [('UI_PASSWORD_FILE', ''), ('ALLOWED_HOSTS', {'testserver'})]:
            p = patch.object(ui, name, value); p.start(); self.addCleanup(p.stop)
        for name, fn in [('local_api', lambda *a, **k: BATCH), ('call', call), ('worker_action', action),
                         ('airflow_runs', lambda dag, **k: [BATCH])]:
            p = patch.object(ui, name, side_effect=fn); p.start(); self.addCleanup(p.stop)
        with access.db() as c:
            for s in SRC:
                c.execute('INSERT INTO episode_owners VALUES(?,?)', (s['conf']['episode_id'], 'PC1'))
        self.client = TestClient(ui.app); self.addCleanup(self.client.close)
        self.client.cookies.set('kaist_session', access.login('PC1', 'operator'))
        self.h = {'Origin': 'http://testserver', 'X-Platform-CSRF': ui.CSRF}

    def remove(self, run):
        return self.client.post('/api/pc1/mano/remove', headers=self.h, json={'run_id': 'mano_x', 'source_run_id': run})

    def test_running_episode_is_stopped_and_back_to_waiting(self):
        self.assertEqual(self.remove('day_a').status_code, 200)
        self.assertIn(('stop', 'day_a', None), self.actions)
        ui.batch_history()
        self.assertEqual(ui.mano_progress({'dag_run_id': 'day_a'}, {'day_a': 'running'}), ('MANO 대기 · 일괄 실행에서 제거됨', True))

    def test_waiting_episode_is_skipped(self):
        self.assertEqual(self.remove('day_b').status_code, 200)
        self.assertIn(('skip', 'day_b', 'hands'), self.actions)
        self.assertEqual(self.remove('day_b').json().get('already'), True)

    def test_other_operator_cannot_remove(self):
        self.client.cookies.set('kaist_session', access.login('PC2', 'operator'))
        self.assertEqual(self.remove('day_a').status_code, 403)


if __name__ == '__main__':
    unittest.main()

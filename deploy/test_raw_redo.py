"""다시 검수(NAS control/redo): 표시 전 실행은 대체되고 RAW가 공용 대기열로 돌아오는지. 임시 DB·임시 control만 씁니다."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code/platform'))
import access
import fleet
import platform_app as ui
from fastapi.testclient import TestClient

DAG = 'kaist_pc1_pipeline'


def run(run_id, raw, state='success', queued='2026-11-25T02:00:00Z'):
    return {'dag_run_id': run_id, 'state': state, 'queued_at': queued, 'logical_date': queued,
            'conf': {'raw_episode_id': raw, 'episode_id': raw, 'operator_id': 'PC2'}}


class RawRedoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, KAIST_INTAKE_DB=str(Path(self.tmp.name) / 'catalog.sqlite'), KAIST_AIRFLOW_API_URL='')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.episodes = [f'ep_20261125_{i:06}' for i in range(1, 4)]
        _p = patch.object(fleet, 'CONTROL', Path(self.tmp.name) / 'control'); _p.start(); self.addCleanup(_p.stop)
        (Path(self.tmp.name) / 'control').mkdir()
        fleet.queue_target_set('PC1', 3, 'PC1')
        ui._info_cache.clear(); self.addCleanup(ui._info_cache.clear)
        ui._redo_cache[:] = [0.0, frozenset()]
        self.runs = [run('old1', self.episodes[0]), run('busy2', self.episodes[1], state='running')]
        for name, value in [('UI_PASSWORD_FILE', ''), ('ALLOWED_HOSTS', {'testserver'})]:
            p = patch.object(ui, name, value); p.start(); self.addCleanup(p.stop)
        for name, value in [('worker', {}), ('call', {'episodes': self.episodes}), ('active_runs', []), ('active_batch_sources', []),
                            ('active_dags', [('gpu_4090_a', 'pipeline', DAG)]), ('deferred_runs', {}), ('batch_history', {})]:
            p = patch.object(ui, name, return_value=value); p.start(); self.addCleanup(p.stop)
        p = patch.object(ui, 'airflow_runs', side_effect=lambda dag, **f: iter([r for r in self.runs if dag == DAG and r['state'] == f.get('state', r['state'])]))
        p.start(); self.addCleanup(p.stop)
        p = patch.object(ui, 'local_api', return_value={'task_instances': [{'task_id': 'mask_approved', 'state': 'running'}]})
        p.start(); self.addCleanup(p.stop)
        with access.db() as c:
            for i, (rid, ep) in enumerate([('old1', self.episodes[0]), ('busy2', self.episodes[1])]):
                c.execute('INSERT INTO execution_versions VALUES(?,?,?,?,?)', (rid, ep, ep, 0, 'PC2'))
                c.execute('INSERT INTO episode_owners VALUES(?,?)', (ep, 'PC2'))
        fleet.raw_claim(self.episodes[0])
        self.client = TestClient(ui.app)
        self.addCleanup(self.client.close)
        self.client.cookies.set('kaist_session', access.login('PC1', 'operator'))
        self.headers = {'Origin': 'http://testserver', 'X-Platform-CSRF': ui.CSRF}

    def rows(self):
        return {r['episode']: r for r in self.client.get('/api/raw-episodes').json()['episodes']}

    def test_redo_returns_done_raw_to_pool(self):
        ep = self.episodes[0]
        before = self.rows()[ep]
        self.assertEqual(before['operator'], 'PC2')
        self.assertTrue(before['done'])
        fleet.raw_redo_mark(ep, 'test', '보정값 오류')
        self.assertEqual(ui.fleet_redo_apply(), [ep])
        self.assertEqual(ui.fleet_redo_apply(), [])          # 한 번만 반영
        self.assertNotIn(ep, access.owners())
        self.assertIsNone(fleet.raw_owner(ep))               # NAS 선점 해제
        after = self.rows()[ep]
        self.assertIsNone(after['operator'])
        self.assertFalse(after['done'])
        self.assertEqual(after['assigned_to'], 'PC1')         # 공용 대기열 → 다시 자동 배정
        self.assertTrue(after['can_run'])
        self.assertNotIn('old1', [r['dag_run_id'] for r in ui.mano_ready_runs()])
        r = self.client.post('/api/jobs/retry', headers=self.headers, json={'dag_id': DAG, 'run_id': 'old1', 'stage': 'reconstruct'})
        self.assertEqual(r.status_code, 409)
        self.assertIn('대체된 실행', r.text)

    def test_active_run_is_not_superseded(self):
        ep = self.episodes[1]
        fleet.raw_redo_mark(ep, 'test')
        self.assertEqual(ui.fleet_redo_apply(), [])
        self.assertEqual(access.owners().get(ep), 'PC2')

    def test_run_started_after_mark_is_kept(self):
        ep = self.episodes[0]
        fleet.raw_redo_mark(ep, 'test', at=time.time() - 3600)
        self.runs.append(run('new1', ep, state='success', queued='2099-01-01T00:00:00Z'))
        self.assertEqual(ui.fleet_redo_apply(), [ep])
        self.assertIn('old1', ui.redo_superseded(fresh=True))
        self.assertNotIn('new1', ui.redo_superseded())


if __name__ == '__main__':
    unittest.main()

"""Use an isolated catalogue and mocked services; never modify live assignments."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code/platform'))
import access
import fleet
import platform_app as ui
from fastapi.testclient import TestClient


class RawExclusionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, KAIST_INTAKE_DB=str(Path(self.tmp.name) / 'catalog.sqlite'), KAIST_AIRFLOW_API_URL='')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.episodes = [f'ep_20261116_{i:06}' for i in range(1, 7)]
        # 실제 NAS(control/) 대신 임시 폴더
        _ctl = tempfile.TemporaryDirectory(); self.addCleanup(_ctl.cleanup)
        _p = patch.object(fleet, 'CONTROL', Path(_ctl.name) / 'control'); _p.start(); self.addCleanup(_p.stop)
        for name, value in [('UI_PASSWORD_FILE', ''), ('ALLOWED_HOSTS', {'testserver'})]:
            p = patch.object(ui, name, value)
            p.start(); self.addCleanup(p.stop)
        for name, value in [('worker', {}), ('call', {'episodes': self.episodes}), ('active_dags', []), ('active_runs', []), ('active_batch_sources', [])]:
            p = patch.object(ui, name, return_value=value)
            setattr(self, name, p.start()); self.addCleanup(p.stop)
        self.client = TestClient(ui.app)
        self.addCleanup(self.client.close)
        self.client.cookies.set('kaist_session', access.login('PC1', 'operator'))
        self.headers = {'Origin': 'http://testserver', 'X-Platform-CSRF': ui.CSRF}
        self.client.get('/api/raw-episodes').raise_for_status()

    def change(self, episode, excluded=True, **kwargs):
        return self.client.post('/api/raw-episodes/exclude', headers=self.headers,
                                json={'episode': episode, 'excluded': excluded, **kwargs})

    def test_exclude_refills_and_survives_refresh_and_login(self):
        episode = self.episodes[0]
        self.assertEqual(self.change(episode).status_code, 200)
        for _ in range(2):
            data = self.client.get('/api/raw-episodes').json()
            row = next(r for r in data['episodes'] if r['episode'] == episode)
            self.assertTrue(row['excluded_from_mine'])
            self.assertFalse(row['can_run'])
            self.assertFalse(row['can_exclude'])
            self.assertEqual(data['team']['load']['PC1'], 3)
            self.assertNotEqual(access.assignments().get(episode), 'PC1')
            self.client.cookies.set('kaist_session', access.login('PC1', 'operator'))
        self.assertEqual(self.change(episode, False).status_code, 200)
        self.assertNotIn(episode, access.excluded_raw('PC1', 'gpu_4090_a'))

    def test_exclusion_is_global(self):
        # 한 작업자가 제외한 RAW는 다른 작업자에게도 배정하지 않습니다.
        episode = self.episodes[0]
        with patch.object(fleet, 'available', return_value=True):
            self.change(episode).raise_for_status()
        access.login('PC2', 'operator')
        access.rebalance('gpu_4090_a', self.episodes, set(), set())
        self.assertNotIn(episode, access.assignments())
        self.assertEqual(access.excluded_raw('PC2', 'gpu_4090_a'), set())
        # 다른 PC도 NAS 제외 표시를 보고 배정하지 않습니다.
        self.assertEqual(fleet.raw_excluded(), {episode: fleet.PC_ID})

    def test_excluded_elsewhere_is_hidden_and_not_assigned(self):
        episode = self.episodes[1]
        with access.db() as c:
            c.execute('DELETE FROM raw_assignments')
        (fleet.CONTROL / 'excluded').mkdir(parents=True, exist_ok=True)
        os.symlink('pc3', fleet.CONTROL / 'excluded' / episode)
        with patch.object(fleet, 'available', return_value=True):
            data = self.client.get('/api/raw-episodes').json()
        self.assertNotIn(episode, [r['episode'] for r in data['episodes']])
        self.assertNotIn(episode, access.assignments())

    def test_restore_removes_nas_mark(self):
        episode = self.episodes[0]
        with patch.object(fleet, 'available', return_value=True):
            self.change(episode).raise_for_status()
            self.assertIn(episode, fleet.raw_excluded())
            self.change(episode, False).raise_for_status()
        self.assertEqual(fleet.raw_excluded(), {})

    def test_other_assignment_wrong_worker_and_csrf_are_rejected(self):
        self.assertEqual(self.change(self.episodes[-1]).status_code, 403)
        self.assertEqual(self.change(self.episodes[0], worker_id='another-worker').status_code, 403)
        access.login('PC2', 'operator')
        access.rebalance('gpu_4090_a', self.episodes, set(), set())
        self.assertEqual(self.change(self.episodes[-1]).status_code, 403)
        response = self.client.post('/api/raw-episodes/exclude', json={'episode': self.episodes[0]})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(access.excluded_raw('PC1'), set())

    def test_active_version_and_batch_block_exclusion(self):
        episode = self.episodes[0]
        with access.db() as c:
            c.execute('INSERT INTO episode_owners VALUES(?,?)', (episode, 'PC1'))
            c.execute('INSERT INTO execution_versions VALUES(?,?,?,?,?)', ('run', episode, episode+'_2', 2, 'PC1'))
        self.active_runs.return_value = [('gpu_4090_a', 'dag', {'conf': {'episode_id': episode+'_2'}})]
        self.assertEqual(self.change(episode).status_code, 409)
        self.active_runs.return_value = []
        self.active_batch_sources.return_value = [({}, {'conf': {'raw_episode_id': episode}})]
        self.assertEqual(self.change(episode).status_code, 409)
        self.assertEqual(access.excluded_raw('PC1'), set())
        self.active_batch_sources.return_value = []
        self.change(episode).raise_for_status()
        data = self.client.get('/api/raw-episodes').json()
        self.assertEqual(data['team']['load']['PC1'], 3)
        self.assertEqual(access.owners()[episode], 'PC1')
        with access.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM execution_versions').fetchone()[0], 1)

    def test_direct_launch_cannot_bypass_exclusion(self):
        episode = self.episodes[0]
        self.change(episode).raise_for_status()
        response = self.client.post('/api/run', headers=self.headers,
                                    json={'worker_id':'gpu_4090_a','mode':'raw','episode_id':episode})
        self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main()

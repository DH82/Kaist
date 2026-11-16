"""PC1~PC6 login and NAS results/<pc>/<SAM3 target> layout. Isolated DB; no live services."""
import importlib.util, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code/platform'))
sys.path.insert(0, str(ROOT / 'code/dags'))
import access
import fleet
import platform_app as ui
from fastapi.testclient import TestClient


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


class PcLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, KAIST_INTAKE_DB=str(Path(self.tmp.name) / 'c.sqlite'), KAIST_AIRFLOW_API_URL='')
        env.start(); self.addCleanup(env.stop)
        # 실제 NAS(control/) 대신 임시 폴더
        _ctl = tempfile.TemporaryDirectory(); self.addCleanup(_ctl.cleanup)
        _p = patch.object(fleet, 'CONTROL', Path(_ctl.name) / 'control'); _p.start(); self.addCleanup(_p.stop)
        for name, value in [('UI_PASSWORD_FILE', ''), ('ALLOWED_HOSTS', {'testserver'})]:
            p = patch.object(ui, name, value); p.start(); self.addCleanup(p.stop)
        self.client = TestClient(ui.app); self.addCleanup(self.client.close)
        self.headers = {'Origin': 'http://testserver', 'X-Platform-CSRF': ui.CSRF}

    def login(self, name):
        return self.client.post('/api/login', headers=self.headers, json={'name': name, 'role': 'operator'})

    def test_only_pc1_to_pc6(self):
        for name in ['pc1', 'PC 3', 'pc-6', 'PC2']:
            r = self.login(name); self.assertEqual(r.status_code, 200, name)
            self.assertRegex(r.json()['name'], r'^PC[1-6]$')
        for name in ['PC7', 'PC0', 'Alice', '작업1', 'pc12']:
            self.assertEqual(self.login(name).status_code, 403, name)

    def test_old_name_session_is_rejected(self):
        self.client.cookies.set('kaist_session', access.login('작업1', 'operator'))
        self.assertIsNone(self.client.get('/api/session', headers=self.headers).json()['name'])
        self.assertEqual(access.logged_in()['operators'], [])


class RawQueueStartTests(unittest.TestCase):
    def test_before_1116_only_if_already_started(self):
        q = ui.raw_in_queue
        self.assertFalse(q('ep_20261115_235959', {}))
        self.assertTrue(q('ep_20261116_000000', {}))
        self.assertTrue(q('ep_20261113_061926', {'ep_20261113_061926': 'PC1'}))
        self.assertTrue(q('ep_20261111_083021', {}, {'ep_20261111_083021'}))


class ResultPathTests(unittest.TestCase):
    def test_request_carries_pc_only_for_publish(self):
        dw = load('direct_worker_t', ROOT / 'code/dags/direct_worker.py')
        conf = {'episode_id': 'ep_1_1', 'operator_id': 'PC3', 'sam_prompt': 'blue cloth'}
        self.assertEqual(dw.request_for('publish', 'r', conf)['result_pc'], 'pc3')
        self.assertNotIn('result_pc', dw.request_for('track', 'r', conf))
        self.assertNotIn('result_pc', dw.request_for('publish', 'r', dict(conf, operator_id='작업1')))

    def test_publish_destination(self):
        rt = load('kaist_runtime_t', ROOT / 'worker/app/dags/kaist_runtime.py')
        self.assertEqual(rt.target_folder('blue  cloth'), 'blue cloth')
        self.assertEqual(rt.target_folder('../a/b'), '_a_b')
        self.assertEqual(rt.target_folder('..'), '미지정')
        self.assertEqual(rt.target_folder(''), '미지정')
        with tempfile.TemporaryDirectory() as t:
            t = Path(t); cfg = {'nas_mount': str(t / 'nas'), 'project_dir': str(t / 'proj'), 'nas_results_subdir': 'results/pc1', 'artifacts': []}
            seen = {}
            with patch.object(rt, 'check_project_mount', return_value=True), \
                 patch.object(rt, 'publish', side_effect=lambda s, d, a, i: seen.setdefault('d', str(d))):
                (t / 'r').mkdir()
                rt.publish_result(t / 'src', cfg, 'ep_1_1', 'k', {}, t / 'r', 'pc4', 'blue cloth')
                self.assertEqual(seen['d'], str(t / 'nas/results/pc4/blue cloth/ep_1_1/k'))
                seen.clear(); rt.publish_result(t / 'src', cfg, 'ep_1_1', 'k', {}, t / 'r', None, 'cup')
                self.assertEqual(seen['d'], str(t / 'nas/results/pc1/cup/ep_1_1/k'))
                self.assertEqual(json.loads((t / 'r/published.json').read_text())['storage'], 'nas')


if __name__ == '__main__':
    unittest.main()

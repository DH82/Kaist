"""로그인 기준 PC 연결: 다른 PC 이름으로 로그인 → 그 PC UI로 1회용 토큰과 함께 이동. 임시 NAS 폴더만 사용."""
import os, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code/platform'))
import access, fleet
import platform_app as ui
from fastapi.testclient import TestClient


class HandoffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, KAIST_INTAKE_DB=str(Path(tmp.name) / 'c.sqlite'), KAIST_AIRFLOW_API_URL='', PC_ID='pc1')
        env.start(); self.addCleanup(env.stop)
        for obj, name, value in [(ui, 'UI_PASSWORD_FILE', ''), (ui, 'ALLOWED_HOSTS', {'testserver'}),
                                 (fleet, 'CONTROL', Path(tmp.name) / 'control'), (fleet, 'MACHINE_PC', 'pc1'), (fleet, 'PC_ID', 'pc1')]:
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)
        self.client = TestClient(ui.app); self.addCleanup(self.client.close)
        self.h = {'Origin': 'http://testserver', 'X-Platform-CSRF': ui.CSRF}

    def login(self, name):
        return self.client.post('/api/login', headers=self.h, json={'name': name, 'role': 'operator'})

    def pc2_online(self, url='http://10.0.0.2:18083'):
        with patch.object(fleet, 'PC_ID', 'pc2'):
            fleet.publish_status({'meta': {'ui_url': url}})

    def test_same_pc_logs_in_locally(self):
        r = self.login('PC1'); self.assertEqual(r.status_code, 200); self.assertNotIn('redirect', r.json())
        self.assertEqual(self.client.get('/api/session').json()['name'], 'PC1')

    def test_other_pc_offline_is_refused(self):
        self.assertEqual(self.login('PC3').status_code, 409)

    def test_other_pc_redirect_and_token_once(self):
        self.pc2_online()
        r = self.login('pc2'); self.assertEqual(r.status_code, 200)
        url = r.json()['redirect']; self.assertTrue(url.startswith('http://10.0.0.2:18083/sso?t='))
        token = url.split('t=', 1)[1]
        # PC2 쪽 UI에서 토큰 사용
        with patch.object(fleet, 'MACHINE_PC', 'pc2'), patch.dict(os.environ, PC_ID='pc2'):
            r = self.client.get('/sso', params={'t': token}, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertEqual(self.client.get('/api/session').json()['name'], 'PC2')
            self.assertEqual(self.client.get('/sso', params={'t': token}, follow_redirects=False).status_code, 403)

    def test_expired_or_wrong_pc_token(self):
        self.pc2_online()
        token = fleet.handoff_create('pc2', 'PC2', 'operator')
        self.assertIsNone(fleet.handoff_take(token))  # 이 PC(pc1)용이 아님
        with patch.object(fleet, 'MACHINE_PC', 'pc2'), patch.object(fleet.time, 'time', return_value=time.time() + 120):
            self.assertIsNone(fleet.handoff_take(token))

    def test_session_of_other_pc_rejected_here(self):
        self.client.cookies.set('kaist_session', access.login('PC2', 'operator'))
        self.assertIsNone(self.client.get('/api/session').json()['name'])

    def test_ui_url(self):
        self.assertEqual(fleet.ui_url({'127.0.0.1:18083', 'localhost:18083', '10.20.32.21:18083'}), 'http://10.20.32.21:18083')


if __name__ == '__main__':
    unittest.main()


class SitePasswordTests(unittest.TestCase):
    def test_sso_and_session_skip_site_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            pw = Path(tmp) / 'pw'; pw.write_text('secret')
            with patch.dict(os.environ, KAIST_INTAKE_DB=str(Path(tmp) / 'c.sqlite'), PC_ID='pc1'), \
                 patch.object(ui, 'UI_PASSWORD_FILE', str(pw)), patch.object(ui, 'ALLOWED_HOSTS', {'testserver'}), \
                 patch.object(fleet, 'MACHINE_PC', 'pc1'), patch.object(fleet, 'CONTROL', Path(tmp) / 'control'):
                c = TestClient(ui.app)
                self.assertEqual(c.get('/api/session').status_code, 401)          # 비밀번호 없음
                self.assertEqual(c.get('/sso', params={'t': 'x' * 30}).status_code, 403)  # 비밀번호 대신 토큰 검사
                c.cookies.set('kaist_session', access.login('PC1', 'operator'))
                self.assertEqual(c.get('/api/session').json()['name'], 'PC1')     # 유효한 세션
                c.cookies.set('kaist_session', 'bogus')
                self.assertEqual(c.get('/api/session').status_code, 401)

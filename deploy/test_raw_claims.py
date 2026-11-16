"""PC 간 RAW 선점: 한 PC가 배정·시작한 RAW는 다른 PC에서 배정·표시·실행되지 않습니다. 임시 NAS 폴더만 사용."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code/platform'))
import access, fleet
import platform_app as ui
from fastapi.testclient import TestClient

EPS = [f'ep_20261116_00000{i}' for i in range(1, 5)]


class RawClaimTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = patch.dict(os.environ, KAIST_INTAKE_DB=str(self.tmp / 'pc1.sqlite'), KAIST_AIRFLOW_API_URL='')
        env.start(); self.addCleanup(env.stop)
        for obj, name, value in [(fleet, 'CONTROL', self.tmp / 'control'), (fleet, 'PC_ID', 'pc1'),
                                 (ui, 'UI_PASSWORD_FILE', ''), (ui, 'ALLOWED_HOSTS', {'testserver'})]:
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)
        (self.tmp / 'control').mkdir()
        for name, value in [('worker', {}), ('call', {'episodes': EPS}), ('active_dags', []), ('active_runs', []), ('active_batch_sources', [])]:
            p = patch.object(ui, name, return_value=value); p.start(); self.addCleanup(p.stop)

    def as_pc(self, pc):
        return patch.object(fleet, 'PC_ID', pc)

    def test_claim_is_exclusive(self):
        self.assertTrue(fleet.raw_claim(EPS[0]))
        with self.as_pc('pc2'):
            self.assertFalse(fleet.raw_claim(EPS[0]))
            self.assertEqual(fleet.raw_taken_elsewhere(), {EPS[0]: 'pc1'})
            fleet.raw_release(EPS[0])  # 남의 선점은 못 풂
        self.assertEqual(fleet.raw_owner(EPS[0]), 'pc1')
        fleet.raw_release(EPS[0]); self.assertIsNone(fleet.raw_owner(EPS[0]))

    def raw_list(self, pc, db):
        with self.as_pc(pc), patch.dict(os.environ, KAIST_INTAKE_DB=str(self.tmp / db)):
            c = TestClient(ui.app); c.cookies.set('kaist_session', access.login(pc.upper(), 'operator'))
            return c.get('/api/raw-episodes').json()

    def test_assignment_on_pc1_hidden_and_not_reassigned_on_pc2(self):
        d1 = self.raw_list('pc1', 'pc1.sqlite')
        mine = {e['episode'] for e in d1['episodes'] if e.get('assigned_to') == 'PC1'}
        self.assertEqual(len(mine), 3)
        self.assertEqual({ep for ep, pc in fleet.raw_claims().items() if pc == 'pc1'}, mine)
        d2 = self.raw_list('pc2', 'pc2.sqlite')
        shown = {e['episode'] for e in d2['episodes']}
        self.assertFalse(shown & mine)                      # PC1 배정분은 PC2 목록에 없음
        self.assertEqual({e['episode'] for e in d2['episodes'] if e.get('assigned_to') == 'PC2'}, set(EPS) - mine)

    def test_launch_refuses_raw_claimed_elsewhere(self):
        with self.as_pc('pc2'):
            fleet.raw_claim(EPS[0])
        c = TestClient(ui.app); c.cookies.set('kaist_session', access.login('PC1', 'admin'))
        h = {'Origin': 'http://testserver', 'X-Platform-CSRF': ui.CSRF}
        with patch.object(ui, 'local_api', return_value={'dag_runs': []}), patch.object(ui, 'worker', return_value={'dag_id': 'd', 'pool': 'p'}):
            r = c.post('/api/run', headers=h, json={'mode': 'pipeline', 'episode_id': EPS[0], 'sam_prompt': 'towel', 'confirmed': True, 'preview_episode': EPS[0]})
        self.assertEqual(r.status_code, 409); self.assertIn('PC2', r.json()['detail'])

    def test_reconcile_releases_unused_and_drops_conflicting_assignment(self):
        fleet.raw_claim(EPS[3])  # 배정 없이 남은 선점 → 해제
        with access.db() as c:
            c.execute('INSERT INTO raw_assignments VALUES(?,?,?,?)', (EPS[1], 'PC1', 'gpu_4090_a', 0))
        with self.as_pc('pc2'):
            fleet.raw_claim(EPS[1])  # PC2가 먼저 선점 → PC1 배정 회수
        with patch.object(access, 'owners', return_value={}):
            ui.reconcile_raw_claims({'raw_started': [EPS[2]]})
        self.assertIsNone(fleet.raw_owner(EPS[3]))
        self.assertNotIn(EPS[1], access.assignments())
        self.assertEqual(fleet.raw_owner(EPS[2]), 'pc1')


if __name__ == '__main__':
    unittest.main()

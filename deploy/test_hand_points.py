"""손 포인트 분할(첫 프레임 포인트 → 손 추적)과 PC별 기본 분할 방식. 가짜 서비스·임시 DB만 사용."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for folder in ('code/platform', 'code/dags', 'worker/app/dags', 'worker/app/worker'):
    sys.path.insert(0, str(ROOT / folder))
import access
import platform_app as ui
import kaist_runtime as rt
import runner, sam3_fast
from direct_worker import request_for
from fastapi import HTTPException
from fastapi.testclient import TestClient

OBJ = {str(c): [[0.5, 0.5, 1]] for c in range(4)}
HAND = {str(c): [[0.3, 0.4, 1], [0.7, 0.4, 1], [0.1, 0.1, 0]] for c in range(4)}
BASE = {'episode_id': 'ep_20261118_000001', 'sam_prompt': 'towel'}


class RequestTests(unittest.TestCase):
    def test_hand_keys_only_with_hand_mode_and_object_tracking(self):
        plain = request_for('sam3', 'r', {**BASE, 'sam_object_mode': 'point_track', 'sam_object_points': OBJ})
        self.assertNotIn('sam_hand_mode', plain)
        with_hand = request_for('sam3', 'r', {**BASE, 'sam_object_mode': 'point_track', 'sam_object_points': OBJ,
                                              'sam_hand_mode': 'point_track', 'sam_hand_points': HAND})
        self.assertEqual((with_hand['sam_hand_mode'], with_hand['sam_hand_points']), ('point_track', HAND))
        self.assertEqual({k: v for k, v in with_hand.items() if not k.startswith('sam_hand')}, plain)
        # 물체가 텍스트 방식이면 손 포인트를 보내지 않습니다(손 포인트는 물체 추적과 함께만).
        self.assertNotIn('sam_hand_mode', request_for('sam3', 'r', {**BASE, 'sam_hand_mode': 'point_track', 'sam_hand_points': HAND}))

    def test_runner_validates_and_keeps_the_job_folder(self):
        req = {'stage': 'sam3', 'episode': 'ep_20261118_000001', 'run_id': 'r', 'sam_object_mode': 'point_track', 'sam_object_points': OBJ}
        self.assertEqual(runner.folder(req), runner.folder({**req, 'sam_hand_mode': 'point_track', 'sam_hand_points': HAND}))
        for bad in ({'sam_hand_mode': 'point_track'}, {'sam_hand_points': HAND}, {'sam_hand_mode': 'text', 'sam_hand_points': HAND}):
            with self.assertRaises(ValueError): runner.folder({**req, **bad})
        text = {'stage': 'sam3', 'episode': 'ep_20261118_000001', 'run_id': 'r'}
        with self.assertRaises(ValueError): runner.folder({**text, 'sam_hand_mode': 'point_track', 'sam_hand_points': HAND})

    def test_baseline_tag_follows_the_points(self):
        tag = sam3_fast.hand_tag(HAND)
        self.assertTrue(sam3_fast.is_hand_points(tag))
        self.assertEqual(tag, sam3_fast.hand_tag({k: HAND[k] for k in reversed(sorted(HAND))}))
        self.assertNotEqual(tag, sam3_fast.hand_tag({**HAND, '0': [[0.31, 0.4, 1]]}))
        self.assertFalse(sam3_fast.is_hand_points('controller')); self.assertFalse(sam3_fast.is_hand_points(None))

    def test_runtime_rejects_hand_points_without_object_tracking(self):
        with self.assertRaisesRegex(ValueError, 'hand points need object mode'):
            rt.execute('sam3', 'ep_20261118_000001', 'r', '/nonexistent', object_mode='text', hand_points=HAND)
        with self.assertRaisesRegex(ValueError, '손'):
            rt.execute('sam3', 'ep_20261118_000001', 'r', '/nonexistent', object_mode='point_track', object_points=OBJ,
                       hand_points={'0': [[0.5, 0.5, 1]]})


class UiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, KAIST_INTAKE_DB=str(Path(tmp.name) / 'catalog.sqlite'), KAIST_AIRFLOW_API_URL='')
        env.start(); self.addCleanup(env.stop)
        for name, value in [('UI_PASSWORD_FILE', ''), ('ALLOWED_HOSTS', {'testserver'})]:
            p = patch.object(ui, name, value); p.start(); self.addCleanup(p.stop)
        self.client = TestClient(ui.app); self.addCleanup(self.client.close)
        self.client.cookies.set('kaist_session', access.login('PC1', 'operator'))
        self.headers = {'Origin': 'http://testserver', 'X-Platform-CSRF': ui.CSRF}

    def launch(self, **kw):
        return ui.Launch(mode='pipeline', **kw)

    def test_hand_points_validation(self):
        self.assertIsNone(ui.hand_points(self.launch(sam_object_mode='point_track')))
        self.assertEqual(ui.hand_points(self.launch(sam_object_mode='point_track', sam_hand_mode='point_track', sam_hand_points=HAND)), HAND)
        for bad in (dict(sam_hand_mode='point_track', sam_hand_points=HAND),                                         # 물체가 텍스트
                    dict(sam_object_mode='point_track', sam_hand_points=HAND),                                    # 손 방식 없이 포인트만
                    dict(sam_object_mode='point_track', sam_hand_mode='point_track', sam_hand_points={'0': [[0.5, 0.5, 1]]}),  # 카메라 1대만
                    dict(sam_object_mode='point_track', sam_hand_mode='point_track', sam_hand_points={**HAND, '2': [[0.5, 0.5, 0]]}),  # 포함 포인트 없음
                    dict(sam_object_mode='point_track', sam_hand_mode='other')):
            with self.assertRaises(HTTPException) as caught: ui.hand_points(self.launch(**bad))
            self.assertEqual(caught.exception.status_code, 400)

    def test_default_mode_is_saved_per_pc(self):
        self.assertEqual(self.client.get('/api/sam-default').json()['mode'], 'point_track')        # 정한 적 없으면 지금까지의 기본값
        saved = self.client.post('/api/sam-default', headers=self.headers, json={'mode': 'point_track_hand'}).json()
        self.assertEqual((saved['mode'], saved['by']), ('point_track_hand', 'PC1'))
        self.assertEqual(self.client.get('/api/sam-default').json()['mode'], 'point_track_hand')
        self.assertEqual(self.client.post('/api/sam-default', headers=self.headers, json={'mode': 'nope'}).status_code, 400)
        self.assertEqual(self.client.get('/api/sam-default').json()['mode'], 'point_track_hand')

    def test_continuous_queue_keeps_hand_points(self):
        with ui.cont_db() as c:
            self.assertIn('hand_points', {r[1] for r in c.execute('PRAGMA table_info(sam_queue)')})


@unittest.skipUnless(__import__('importlib').util.find_spec('numpy') and __import__('importlib').util.find_spec('PIL'), 'numpy/PIL 없음(sam-env에서 실행)')
class SeedTests(unittest.TestCase):
    def test_each_clicked_hand_is_selected_once_and_excluded_hands_are_dropped(self):
        import numpy as np
        import sam_hand_point_track as hand
        left = np.zeros((100, 200), bool); left[20:60, 20:60] = True
        right = np.zeros((100, 200), bool); right[20:60, 140:180] = True
        other = np.zeros((100, 200), bool); other[70:95, 90:110] = True
        found = [{'id': 1, 'mask': left, 'prob': .9}, {'id': 2, 'mask': right, 'prob': .8}, {'id': 3, 'mask': other, 'prob': .7}]
        seeds, loose = hand.choose_seeds(found, [[0.2, 0.4, 1], [0.25, 0.45, 1], [0.8, 0.4, 1], [0.5, 0.8, 0], [0.5, 0.1, 1]], 200, 100)
        self.assertEqual([s['id'] for s in seeds], [1, 2])
        self.assertEqual(loose, [[0.5, 0.1, 1]])
        seeds, _ = hand.choose_seeds(found, [[0.5, 0.8, 1], [0.5, 0.85, 0]], 200, 100)   # 제외 점이 들어간 손은 고르지 않음
        self.assertEqual(seeds, [])


if __name__ == '__main__':
    unittest.main()

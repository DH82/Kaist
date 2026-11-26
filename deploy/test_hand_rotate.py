"""손 추적 180° 회전(sam_hand_rotate.py, sam3_fast.hand_rotate_cams) 단위 시험. GPU 없이 돌아갑니다(sam-env 등 numpy/PIL 필요).

  sam-env/bin/python deploy/test_hand_rotate.py
"""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'worker/app/dags'))
import sam3_fast


class RotateCamsTests(unittest.TestCase):
    def test_default_is_camera_2(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KAIST_HAND_ROTATE_CAMS', None)
            self.assertEqual(sam3_fast.hand_rotate_cams(), [1])

    def test_override_and_off(self):
        with patch.dict(os.environ, {'KAIST_HAND_ROTATE_CAMS': '3, 1'}):
            self.assertEqual(sam3_fast.hand_rotate_cams(), [1, 3])
        with patch.dict(os.environ, {'KAIST_HAND_ROTATE_CAMS': ''}):
            self.assertEqual(sam3_fast.hand_rotate_cams(), [])
        with patch.dict(os.environ, {'KAIST_HAND_ROTATE_CAMS': '4'}):
            with self.assertRaises(ValueError):
                sam3_fast.hand_rotate_cams()

    def test_tag_unchanged_without_rotation_and_changes_with_it(self):
        pts = {'0': [[0.5, 0.5, 1]], '1': [[0.4, 0.6, 1]], '2': [[0.5, 0.5, 1]], '3': [[0.5, 0.5, 1]]}
        old = 'points:' + __import__('hashlib').sha256(json.dumps(pts, sort_keys=True).encode()).hexdigest()[:16]
        self.assertEqual(sam3_fast.hand_tag(pts), old)          # 회전을 끄면 예전 기준선을 그대로 씁니다
        self.assertEqual(sam3_fast.hand_tag(pts, []), old)
        self.assertNotEqual(sam3_fast.hand_tag(pts, [1]), old)  # 회전을 켜면 다시 계산합니다
        self.assertTrue(sam3_fast.is_hand_points(sam3_fast.hand_tag(pts, [1])))


@unittest.skipUnless(__import__('importlib').util.find_spec('numpy') and __import__('importlib').util.find_spec('PIL'), 'numpy/PIL 없음(sam-env에서 실행)')
class WrapperTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        from PIL import Image
        self.np, self.Image = np, Image
        self.tmp = Path(tempfile.mkdtemp())
        self.case = self.tmp / 'case'
        (self.case / 'color/1').mkdir(parents=True)
        (self.case / 'metadata.json').write_text(json.dumps({'WH': [8, 6], 'frame_num': 2, 'fps': 30}))
        rng = np.random.default_rng(0)
        self.frames = [rng.integers(0, 255, (6, 8, 3), dtype=np.uint8) for _ in range(2)]
        for f, a in enumerate(self.frames):
            Image.fromarray(a).save(self.case / f'color/1/{f}.png')

    def test_points_flip_and_frames_rotate_exactly(self):
        import sam_hand_rotate as rot
        self.assertEqual(rot.flip_points([[0.25, 0.1, 1], [1.0, 0.0, 0]]), [[0.75, 0.9, 1], [0.0, 1.0, 0]])
        rot.rotated_case(self.case, 1, self.tmp / 'view')
        for f, a in enumerate(self.frames):
            b = self.np.asarray(self.Image.open(self.tmp / f'view/color/1/{f}.png'))
            self.assertTrue((b == a[::-1, ::-1]).all())

    def test_rotated_job_returns_masks_in_original_orientation(self):
        import sam_hand_rotate as rot
        np, Image = self.np, self.Image
        seen = {}

        def fake_run_camera(p, case, cam, points, out, thresh):
            # 돌린 영상에서 '손'을 왼쪽 위 2×3 칸으로 찾았다고 가정합니다.
            seen.update(case=case, points=points)
            first = np.asarray(Image.open(case / f'color/{cam}/0.png'))
            seen['first'] = first
            (out / '1').mkdir(parents=True); (out / 'seed').mkdir()
            m = np.zeros((6, 8), np.uint8); m[0:2, 0:3] = 255
            Image.fromarray(m).save(out / '1/0.png')
            Image.fromarray(first).save(out / 'seed/selected.jpg')
            (out / 'record.json').write_text(json.dumps({'points_norm': points, 'points_px': [], 'note': 'x'}))

        job = {'cam': 1, 'points': [[0.25, 0.1, 1]], 'out': self.tmp / 'jobs/cam1', 'rotate': 180}
        (self.tmp / 'jobs').mkdir()
        with patch.object(rot.hand, 'run_camera', fake_run_camera):
            rot.run_job(None, self.case, job, 0.5)
        self.assertTrue((seen['first'] == self.frames[0][::-1, ::-1]).all())
        self.assertEqual(seen['points'], [[0.75, 0.9, 1]])
        back = np.asarray(Image.open(self.tmp / 'jobs/cam1/1/0.png')) > 0
        want = np.zeros((6, 8), bool); want[4:6, 5:8] = True       # 원래 방향: 오른쪽 아래
        self.assertTrue((back == want).all())
        rec = json.loads((self.tmp / 'jobs/cam1/record.json').read_text())
        self.assertTrue(rec['rotated_180'])
        self.assertEqual(rec['points_norm'], [[0.25, 0.1, 1]])
        self.assertEqual(rec['tracked_points_norm'], [[0.75, 0.9, 1]])
        self.assertEqual(rec['points_px'], [[2, 0, 1]])
        self.assertFalse(any(p.name.startswith('.rotated-') for p in (self.tmp / 'jobs').iterdir()))   # 임시 사본 정리

    def test_unrotated_job_is_passed_through(self):
        import sam_hand_rotate as rot
        calls = []
        with patch.object(rot.hand, 'run_camera', lambda *a: calls.append(a)):
            rot.run_job('P', self.case, {'cam': 0, 'points': [[0.5, 0.5, 1]], 'out': self.tmp / 'o', 'rotate': 0}, 0.5)
        self.assertEqual(calls, [('P', self.case, 0, [[0.5, 0.5, 1]], self.tmp / 'o', 0.5)])

    def test_bad_rotate_value_is_rejected(self):
        import sam_hand_rotate as rot
        with self.assertRaises(ValueError):
            rot.parse_jobs(json.dumps([{'cam': 1, 'points': [[0.5, 0.5, 1]], 'out': '/tmp/x', 'rotate': 90}]))


if __name__ == '__main__':
    unittest.main()

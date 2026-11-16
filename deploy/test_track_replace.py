"""추적 일괄 교체 저장(mask_editor.apply_track): 기존 마스크가 있어도 교체, 이전 파일 보관, 구간 밖은 그대로.
가짜 검수 모듈과 임시 폴더만 사용합니다(실제 에피소드 초안은 건드리지 않음)."""
import importlib.util, json, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'worker/app/review'))
spec = importlib.util.spec_from_file_location('mask_editor_t', ROOT / 'worker/app/worker/mask_editor.py')
me = importlib.util.module_from_spec(spec); spec.loader.exec_module(me)
W, H, N = 16, 8, 6


class FakeReview:
    def __init__(self, d): self.d = d; self.writes = []
    def root(self, project, ep): return self.d
    def source(self, d): return d / 'source'
    def prepare_draft(self, project, ep): return self.d / 'review/draft'
    def approval_summary(self, project, ep): return {'status': 'draft'}
    def write_mask(self, project, ep, cam, obj, frame, image):
        p = self.d / f'review/draft/mask/{cam}/{obj}/{frame}.png'; p.parent.mkdir(parents=True, exist_ok=True)
        image.convert('L').point(lambda v: 255 if v else 0).save(p); self.writes.append(frame)


def box(path, x0, x1):
    im = Image.new('L', (W, H), 0); im.paste(255, (x0, 0, x1, H)); path.parent.mkdir(parents=True, exist_ok=True); im.save(path)


class ApplyTrackTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); t = Path(t.name)
        self.d = t / 'ep'; (self.d / 'source').mkdir(parents=True)
        (self.d / 'source/metadata.json').write_text(json.dumps({'WH': [W, H], 'frame_num': N}))
        self.worker = t / 'worker'; (self.worker / 'jobs').mkdir(parents=True)
        p = patch.object(me, 'ROOT', self.worker); p.start(); self.addCleanup(p.stop)
        self.src = self.worker / 'wand_tracks/job1'; self.src.mkdir(parents=True)
        mask = self.d / 'review/draft/mask/2/1'
        for f in range(N):
            box(mask / f'{f}.png', 0, 4)          # 기존(품질 나쁜) 손 마스크
        for f in (1, 2, 4):
            box(self.src / f'{f}.png', 6, 12)      # 추적 결과
        box(self.src / '3.png', 0, 4)              # 기존과 같은 결과 → 그대로
        # 프레임 5는 추적 결과 없음 → 구간 밖이라 영향 없음, 0도 구간 밖
        self.review = FakeReview(self.d)
        self.project = t / 'project'

    def test_replaces_existing_masks_in_range_and_keeps_history(self):
        r = me.apply_track(self.review, self.project, 'ep_x', {'cam': 2, 'obj': 1, 'from': 1, 'to': 4, 'start': 2,
                                                             'dir': str(self.src), 'points': [[0.5, 0.5, 1]], 'text': 'hand'}, 'PC1')
        self.assertEqual((r['changed'], r['unchanged'], r['emptied']), (3, 1, 0))
        self.assertEqual(sorted(self.review.writes), [1, 2, 4])
        hist = next((self.d / 'review/edit_history').iterdir())
        audit = json.loads((hist / 'edit.json').read_text())
        self.assertEqual((audit['kind'], audit['changed_frames'], audit['operator']), ('track_replace', [1, 2, 4], 'PC1'))
        self.assertEqual(sorted(p.name for p in (hist / 'before').iterdir()), ['1.png', '2.png', '4.png'])
        with Image.open(self.d / 'review/draft/mask/2/1/0.png') as im:   # 구간 밖은 그대로
            self.assertEqual(im.getbbox(), (0, 0, 4, H))

    def test_missing_track_frame_becomes_empty(self):
        r = me.apply_track(self.review, self.project, 'ep_x', {'cam': 2, 'obj': 1, 'from': 5, 'to': 5, 'start': 5, 'dir': str(self.src)}, 'PC1')
        self.assertEqual((r['changed'], r['emptied']), (1, 1))

    def test_rejects_foreign_dir_and_running_episode(self):
        with self.assertRaises(ValueError):
            me.apply_track(self.review, self.project, 'ep_x', {'cam': 2, 'obj': 1, 'from': 1, 'to': 2, 'dir': '/tmp'}, 'PC1')
        job = self.worker / 'jobs/j'; job.mkdir()
        (job / 'request.json').write_text(json.dumps({'episode': 'ep_x', 'stage': 'track'}))
        (job / 'status.json').write_text(json.dumps({'state': 'running'}))
        with self.assertRaises(ValueError):
            me.apply_track(self.review, self.project, 'ep_x', {'cam': 2, 'obj': 1, 'from': 1, 'to': 2, 'dir': str(self.src)}, 'PC1')


if __name__ == '__main__':
    unittest.main()

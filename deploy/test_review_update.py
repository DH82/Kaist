"""검수 경계의 코드 갱신과 입력·설정 보호."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'worker/app/dags'))
import kaist_runtime as rt


class ReviewUpdateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.pin = Path(tmp.name) / 'identity.json'
        self.old = {'episode': 'ep_1', 'run_id': 'r1', 'code': {'script.py': 'old'},
                    'config': {'gpu_uuid': 'gpu1'}, 'runtime': {'pipeline_root': '/pipeline'},
                    'config_env_sha256': 'config', 'sam_request': {'prompt': 'towel',
                    'object_tracker_sha256': 'old'}}
        self.pin.write_text(json.dumps(self.old))
        self.new = copy.deepcopy(self.old)
        self.new['code'] = {'script.py': 'new', 'added.py': 'new'}
        self.new['sam_request']['object_tracker_sha256'] = 'new'

    def test_review_preserves_provenance_and_resumes(self):
        rt.resume_review_identity(self.pin, self.new, 'mask_approved')
        self.assertEqual(rt.read(self.pin), self.new)
        archive = list(self.pin.parent.glob('identity.json.review-*'))
        self.assertEqual(len(archive), 1)
        self.assertEqual(rt.read(archive[0])['previous'], self.old)
        rt.resume_review_identity(self.pin, self.new, 'mask_approved')
        self.assertEqual(len(list(self.pin.parent.glob('identity.json.review-*'))), 1)

    def test_compute_stage_keeps_original_identity(self):
        for stage in ('sam3', 'track', 'reconstruct', 'hands'):
            rt.resume_review_identity(self.pin, self.new, stage)
            self.assertEqual(rt.read(self.pin), self.old)

    def test_changed_inputs_and_settings_are_not_accepted(self):
        for key, value in [('episode', 'ep_2'), ('run_id', 'r2'), ('config', {}),
                           ('runtime', {}), ('config_env_sha256', 'other'),
                           ('sam_request', {'prompt': 'cup'})]:
            new = dict(self.new, **{key: value})
            rt.resume_review_identity(self.pin, new, 'tracking_approved')
            self.assertEqual(rt.read(self.pin), self.old)
        self.assertFalse(list(self.pin.parent.glob('identity.json.review-*')))

    def test_execute_review_after_update_still_requires_approval(self):
        from deploy.test_stage_reset import Workspace, EP, RUN
        with tempfile.TemporaryDirectory() as folder:
            w = Workspace(folder)
            w.cfg.update(gpu_uuid='gpu1', control_python=sys.executable)
            w.identity['config'] = w.cfg
            pin = w.receipt / 'identity.json'
            pin.write_text(json.dumps(w.identity))
            cfg = Path(folder) / 'config.json'
            cfg.write_text(json.dumps(w.cfg))
            (w.scripts / '18_fit_hands.py').write_text('# updated code')
            review = Mock()
            review.approved.side_effect = ValueError('not approved')
            with patch.object(rt, 'check_project_mount'), patch.object(rt, 'module', return_value=review), \
                    patch('pipeline_progress.install'):
                args = ('mask_approved', EP, RUN, cfg)
                self.assertFalse(rt.execute(*args, sam_prompt='towel', sam_confirmed=True))
                self.assertNotEqual(rt.read(pin)['code'], w.identity['code'])
                review.approved.side_effect = None
                self.assertTrue(rt.execute(*args, sam_prompt='towel', sam_confirmed=True))


if __name__ == '__main__':
    unittest.main()

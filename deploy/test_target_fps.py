"""처리 fps(15 기본 / 30): 동기화 프레임 솎아내기, 전처리 계약, 작업 요청·실행 고정, UI 실행 설정."""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'pipeline/current/project/00_scripts'
for folder in ('code/platform', 'code/dags', 'worker/app/dags', 'worker/app/worker'):
    sys.path.insert(0, str(ROOT / folder))
sys.path.insert(0, str(SCRIPTS))
import sync_mapping, preparation_contract
import kaist_runtime as rt
import runner
from direct_worker import request_for


def write_raw(folder, frames=60, rate=30.0, late=0.004):
    """카메라 4대의 타임스탬프 CSV(카메라마다 조금씩 늦게 찍힘)."""
    for cam in range(1, 5):
        rows = ['frame_idx,hw_timestamp_ms,host_time_s'] + [f'{i},{i * 1000 / rate},{100 + i / rate + cam * late:.6f}' for i in range(frames)]
        (Path(folder) / f'cam_{cam}_timestamps.csv').write_text('\n'.join(rows) + '\n')


class SyncTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.src = tmp.name; write_raw(self.src)
        self.cams = [f'cam_{c}' for c in range(1, 5)]

    def test_30fps_keeps_every_frame_and_adds_no_keys(self):
        full = sync_mapping.source_mapping(self.src, self.cams, fps=30)
        self.assertEqual(full, sync_mapping.source_mapping(self.src, self.cams))
        self.assertNotIn('frame_stride', full); self.assertNotIn('target_fps', full)
        self.assertGreaterEqual(len(full['frames']), 59)

    def test_15fps_keeps_every_second_frame(self):
        full = sync_mapping.source_mapping(self.src, self.cams, fps=30)
        half = sync_mapping.source_mapping(self.src, self.cams, fps=15)
        self.assertEqual(half['raw_indices'], full['raw_indices'][::2])
        self.assertEqual([f['output_idx'] for f in half['frames']], list(range(len(half['frames']))))
        self.assertEqual((half['frame_stride'], half['target_fps']), (2, 15.0))
        times = [f['reference_time_s'] for f in half['frames']]
        self.assertAlmostEqual(times[1] - times[0], 1 / 15, places=3)

    def test_preprocess_sync_matches_recorded_mapping(self):
        rows = [sync_mapping.read_rows(self.src, c) for c in self.cams]
        times = [{i: float(r['host_time_s']) for i, r in rs.items()} for rs in rows]
        for fps in (15.0, 30.0):
            picks, _ = sync_mapping.sync_frames(times, fps, .025)
            self.assertEqual([list(p) for p in picks], sync_mapping.source_mapping(self.src, self.cams, fps=fps)['raw_indices'])

    def test_capture_already_at_target_rate_is_not_halved(self):
        write_raw(self.src, rate=15.0)
        self.assertNotIn('frame_stride', sync_mapping.source_mapping(self.src, self.cams, fps=15))

    def test_qc_and_preprocess_copies_are_identical(self):
        self.assertEqual((SCRIPTS / 'sync_mapping.py').read_bytes(),
                         (ROOT / 'pipeline/current/workspace/kaist-full/PhysTwin/data_process/sync_mapping.py').read_bytes())


class ContractTests(unittest.TestCase):
    def contract(self, *fps):
        return preparation_contract.contract('raw', SCRIPTS, ROOT / 'pipeline/current/workspace/kaist-full/PhysTwin', *fps)

    def test_default_contract_is_the_30fps_contract(self):
        self.assertEqual(self.contract()['settings'], preparation_contract.SETTINGS)
        self.assertEqual(self.contract(), self.contract(30))

    def test_15fps_contract_differs_only_in_fps(self):
        self.assertEqual(self.contract(15)['settings'], {**preparation_contract.SETTINGS, 'fps': 15.0})
        with self.assertRaises(ValueError):
            self.contract(20)

    def test_prepared_fps_reads_receipt_and_defaults_to_30(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(preparation_contract.prepared_fps(tmp), 30.0)
            (Path(tmp) / 'preparation.json').write_text(json.dumps({'contract': self.contract(15)}))
            self.assertEqual(preparation_contract.prepared_fps(tmp), 15.0)


class RequestTests(unittest.TestCase):
    base = {'episode_id': 'ep_1', 'sam_prompt': 'towel', 'sam_settings_confirmed': True}

    def test_only_15fps_adds_a_key(self):
        plain = request_for('sam3', 'r1', self.base)
        self.assertEqual(request_for('sam3', 'r1', {**self.base, 'target_fps': 30}), plain)
        self.assertNotIn('target_fps', plain)
        for stage in ('prepare_raw', 'sam3', 'hands', 'publish'):
            self.assertEqual(request_for(stage, 'r1', {**self.base, 'target_fps': 15})['target_fps'], 15)

    def test_runner_accepts_15_only_and_keeps_job_folder(self):
        req = request_for('prepare_raw', 'r1', self.base)
        self.assertEqual(runner.folder(req), runner.folder({**req, 'target_fps': 15}))
        for bad in (30, 20, '15', None):
            with self.assertRaises(ValueError):
                runner.folder({**req, 'target_fps': bad})


class IdentityTests(unittest.TestCase):
    def run_prepare(self, w, cfg, fps, run):
        pipeline = Mock()
        with patch.object(rt, 'check_project_mount'), patch.object(rt, 'module', return_value=pipeline), \
                patch('pipeline_progress.install'):
            rt.execute('prepare_raw', self.ep, run, cfg, **({} if fps is None else {'target_fps': fps}))
        return pipeline

    def test_fps_is_pinned_and_passed_to_preprocessing(self):
        from deploy.test_stage_reset import Workspace, EP
        with tempfile.TemporaryDirectory() as folder:
            w = Workspace(folder)
            w.cfg.update(gpu_uuid='gpu1', control_python=sys.executable, raw_root=str(Path(folder) / 'raw'))
            self.ep = EP
            cfg = Path(folder) / 'config.json'; cfg.write_text(json.dumps(w.cfg))
            key = lambda run: w.project / '_airflow' / EP / __import__('hashlib').sha256(run.encode()).hexdigest()[:24] / 'identity.json'
            pipeline = self.run_prepare(w, cfg, 15, 'run15')
            self.assertEqual(rt.read(key('run15'))['target_fps'], 15)
            self.assertEqual(pipeline.run_qc.call_args.kwargs['fps'], 15)
            # 같은 실행을 다른 fps로 이어갈 수 없습니다.
            with self.assertRaises(RuntimeError):
                self.run_prepare(w, cfg, 30, 'run15')
            pipeline = self.run_prepare(w, cfg, None, 'run30')
            self.assertNotIn('target_fps', rt.read(key('run30')))
            self.assertEqual(pipeline.run_qc.call_args.kwargs['fps'], 30)
            with self.assertRaises(ValueError):
                self.run_prepare(w, cfg, 20, 'run20')


class LaunchTests(unittest.TestCase):
    def setUp(self):
        from deploy.test_batch_prep import BatchPrepTests
        BatchPrepTests.setUp(self)

    def post(self, path, body):
        return self.c.post(path, headers=self.h, json=body)

    def test_default_is_15_and_30_is_selectable(self):
        from deploy.test_batch_prep import EPS
        body = {'mode': 'pipeline', 'episode_id': EPS[0], 'sam_prompt': 'towel', 'confirmed': True, 'preview_episode': EPS[0]}
        self.assertEqual(self.post('/api/run', dict(body, target_fps=24)).status_code, 400)
        self.assertEqual(self.post('/api/run', body).status_code, 200)
        self.assertEqual(self.post('/api/run', dict(body, episode_id=EPS[1], preview_episode=EPS[1], target_fps=30)).status_code, 200)
        self.assertEqual([r['conf']['target_fps'] for r in self.af.runs.values()], [15, 30])

    def test_batch_prep_passes_fps(self):
        from deploy.test_batch_prep import EPS
        self.post('/api/run/batch-prep', {'episodes': EPS[:1]})
        self.post('/api/run/batch-prep', {'episodes': EPS[1:2], 'target_fps': 30})
        self.assertEqual([r['conf']['target_fps'] for r in self.af.runs.values()], [15, 30])


if __name__ == '__main__':
    unittest.main()

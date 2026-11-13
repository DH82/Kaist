"""Regression tests for GPU-less builds and service readiness gates."""
import ast
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('scan_patch', ROOT / 'gpu/env/patch_selective_scan.py')
scan_patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan_patch)
sys.path.insert(0, str(ROOT / 'repro'))
from weights import ASSETS, resolve_weights


class WeightPathsTests(unittest.TestCase):
    def test_independent_locations_and_shared_faces(self):
        with tempfile.TemporaryDirectory() as folder:
            overrides = []
            for key, _, relative, _ in ASSETS:
                if key == 'mano-faces-mono':
                    continue
                path = Path(folder) / key
                if relative.endswith(('.pkl', '.npz')):
                    path.touch()
                else:
                    path.mkdir()
                overrides.append(f'{key}={path}')
            result = resolve_weights(None, {}, overrides)
            self.assertEqual(len(result), 10)
            self.assertEqual(result['WEIGHT_MANO_FACES'], result['WEIGHT_MANO_FACES_MONO'])
            self.assertEqual(resolve_weights(None, result), result)

    def test_missing_assets_and_unknown_names_fail(self):
        with self.assertRaises(ValueError):
            resolve_weights(None, {})
        with self.assertRaises(ValueError):
            resolve_weights(None, {}, ['unknown=/tmp'])


class ScanBuildTests(unittest.TestCase):
    def test_build_without_gpu_and_multiple_architectures(self):
        source = ROOT / 'pipeline/dependency-source/kaist-full/PhysTwin/Mono/data_process/Hamba/VMamba/kernels/selective_scan/setup.py'
        original = source.read_bytes()
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / 'setup.py'
            copy.write_bytes(original)
            scan_patch.patch(copy)
            patched = copy.read_text()
            tree = ast.parse(patched)
            fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'get_compute_capability')
            # No torch binding: any GPU access would fail this test.
            scope = {'os': os}
            exec(compile(ast.Module(body=[fn], type_ignores=[]), str(copy), 'exec'), scope)
            for archs, minimum in [('8.9', 89), ('8.9;12.0+PTX', 89), ('12.0', 120)]:
                with patch.dict(os.environ, TORCH_CUDA_ARCH_LIST=archs):
                    self.assertEqual(scope['get_compute_capability'](), minimum)
            self.assertNotIn('-arch=sm_', patched)
            scan_patch.patch(copy)
            self.assertEqual(copy.read_text(), patched)
        self.assertEqual(source.read_bytes(), original)

    def test_unexpected_source_fails_instead_of_silent_patch(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'setup.py'
            source.write_text('def get_compute_capability():\n    return 89\n')
            with self.assertRaises(ValueError):
                scan_patch.patch(source)


if __name__ == '__main__':
    unittest.main()

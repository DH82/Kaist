import contextlib
import importlib.util
import io
import json
from pathlib import Path
import queue
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pipeline_progress', ROOT/'worker/app/dags/pipeline_progress.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class ProgressTest(unittest.TestCase):
    def test_counters(self):
        cases = [
            ('SAM_PROGRESS {"done":37,"frames":100,"prompt":"hand"}', 37, 100),
            ('export:  37%|###       | 37/100 [00:12<00:20, 2it/s]', 37, 100),
            ('완료 2/9 (작업 수 기준)', 2, 9),
            ('완료 3/10구간', 3, 10),
            ('[sync] 100 aligned frames over 3.00s', 0, 400),
            ('[export] cam_2 -> cam 1: 100 frames', 200, 400),
        ]
        for line, done, total in cases:
            with self.subTest(line=line):
                result = p.parse(line)
                self.assertEqual((result['done'], result['total']), (done, total))
                self.assertEqual(p.parse(p.PREFIX+json.dumps(result)), result)

    def test_ignore_unrelated_and_invalid(self):
        for line in ['GPU 0 37% | VRAM 37/100 MiB', '/2026/11/13',
                     'SAM_PROGRESS {}', 'SAM_PROGRESS {"done":101,"frames":100}',
                     'SAM_PROGRESS {"done":1,"frames":0}', 'SAM_PROGRESS nope']:
            self.assertIsNone(p.parse(line))

    def test_bridge_preserves_queue_and_step_failure(self):
        class Display:
            def render(self, lines): return lines
        live = types.SimpleNamespace(queue=queue, Display=Display, execute=lambda *a: 'result')
        def original(root, name, action, expected): return action()
        steps = types.SimpleNamespace(step=original)
        output = io.StringIO()
        with patch.dict(sys.modules, live_log=live, resume_steps=steps), patch.dict('os.environ', KAIST_PROGRESS_STAGE='hands'), contextlib.redirect_stdout(output):
            p.install()
            q = live.queue.Queue()
            line = 'SAM_PROGRESS {"done":37,"frames":100,"prompt":"hand"}'
            q.put(line)
            self.assertEqual(q.get(), line)
            self.assertEqual(live.execute([], Path('03.log'), None, {}), 'result')
            self.assertEqual(steps.step(None, '18', lambda: 42, []), 42)
            with self.assertRaises(RuntimeError):
                steps.step(None, '19', lambda: (_ for _ in ()).throw(RuntimeError('failed')), [])
        events = [p.parse(line) for line in output.getvalue().splitlines()]
        self.assertTrue(any(e['done'] == 37 for e in events))
        self.assertFalse(any(e['done'] == 2 and e['total'] == 3 for e in events))


if __name__ == '__main__':
    unittest.main()

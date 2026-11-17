"""Legacy automatic requests must never reset a user's existing run."""
import ast
from pathlib import Path
import unittest
from unittest.mock import Mock


class DisabledAutoReconstructTests(unittest.TestCase):
    def test_received_and_partly_processed_requests_are_cancelled(self):
        source = Path(__file__).resolve().parents[1] / 'code/platform/platform_app.py'
        tree = ast.parse(source.read_text())
        handler = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'fleet_reconstruct_redo')
        for state, done in [('received', []), ('running', ['ep_started'])]:
            with self.subTest(state=state):
                update = Mock()
                scope = {'task_update': update}
                exec(compile(ast.Module(body=[handler], type_ignores=[]), str(source), 'exec'), scope)
                payload = {'episodes': ['ep_started', 'ep_pending'], 'todo': ['ep_pending'], 'done': done}
                scope['fleet_reconstruct_redo']({'id': 'legacy', 'state': state}, payload)
                update.assert_called_once()
                self.assertEqual(update.call_args.args, ('legacy',))
                self.assertEqual(update.call_args.kwargs['state'], 'cancelled')
                self.assertEqual(payload['todo'], ['ep_pending'])
                self.assertEqual(payload['done'], done)


if __name__ == '__main__':
    unittest.main()

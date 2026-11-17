"""MANO 일괄 실행: 한 에피소드가 실패해도 멈추지 않고 다음 에피소드로 진행. 가짜 Worker만 사용합니다."""
import datetime as dt, sys, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code/dags'))
import direct_worker as dw

SRC = [{'run_id': r, 'conf': {'episode_id': 'ep_20261117_00000' + str(i)}} for i, r in enumerate(('day_a', 'day_b', 'day_c'), 1)]


class ManoBatchSkipTests(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.started = []
        self.broken = set()
        def call(method, path, json=None, **kw):
            key = (json['run_id'], json['stage'])
            if json['run_id'] in self.broken:
                raise httpx.HTTPStatusError('400', request=httpx.Request('POST', 'http://x'), response=httpx.Response(400))
            if json['stage'] == 'reconstruct':
                return {'state': self.state.get(key, 'success')}
            if path.endswith('/start') and self.state.get(key, 'not_started') == 'not_started':
                self.started.append(key); self.state[key] = 'running'
            return {'state': self.state.get(key, 'not_started')}
        p = patch.object(dw, 'call', side_effect=call); p.start(); self.addCleanup(p.stop)
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        p = patch.object(dw, 'mano_cutoff', return_value=future); p.start(); self.addCleanup(p.stop)
        run = SimpleNamespace(conf={'sources': SRC}, start_date=None, queued_at=None, logical_date=None)
        self.poke = lambda: dw.poll_mano_batch(dag_run=run)

    def test_failed_episode_is_skipped(self):
        self.state[('day_a', 'hands')] = 'failed'
        self.assertFalse(self.poke())
        self.assertEqual(self.started, [('day_b', 'hands')])
        # 다음 확인에서도 실패한 에피소드는 다시 시작하지 않습니다.
        for s in dw.MANO_STAGES: self.state[('day_b', s)] = 'success'
        self.assertFalse(self.poke())
        self.assertEqual(self.started[-1], ('day_c', 'hands'))
        for s in dw.MANO_STAGES: self.state[('day_c', s)] = 'success'
        self.assertTrue(self.poke())
        self.assertNotIn(('day_a', 'validate'), self.started)

    def test_missing_reconstruct_and_request_error_are_skipped(self):
        self.state[('day_a', 'reconstruct')] = 'failed'
        self.broken.add('day_b')
        self.assertFalse(self.poke())
        self.assertEqual(self.started, [('day_c', 'hands')])

    def test_connection_error_retries_without_skipping(self):
        with patch.object(dw, 'call', side_effect=httpx.ConnectError('down')):
            self.assertFalse(self.poke())


if __name__ == '__main__':
    unittest.main()

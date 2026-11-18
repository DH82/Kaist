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


class ManoBatchRedo3DTests(unittest.TestCase):
    """업데이트 전 대기열 3D 복원 다시: 업데이트 전 코드로 고정된 에피소드만 3D 복원부터 다시 한 뒤 MANO."""
    def setUp(self):
        self.state = {}                      # (run, stage) → 작업 상태
        self.started = []
        self.drift = {'day_a': ['x.py']}     # 업데이트 전 코드로 고정된 실행
        self.config_drift = set()
        self.resets = {}                     # run → reset-status
        self.calls = []
        self.batch_start = 1000.0
        def call(method, path, json=None, **kw):
            action = path.rsplit('/', 1)[1]
            self.calls.append((action, json.get('run_id'), json.get('stage')))
            run = json['run_id']
            if action == 'reset-status':
                return self.resets.get(run, {'state': 'not_started'})
            if action == 'drift':
                ch = self.drift.get(run, [])
                return {'pinned': True, 'changed': ch, 'code_only': run not in self.config_drift, 'transfer': False}
            if action == 'reset':
                self.resets[run] = {'state': 'running', 'start': json['start'], 'started': self.batch_start + 5}
                return self.resets[run]
            key = (run, json['stage'])
            if json['stage'] in ('prepare_reconstruction', 'reconstruct') and key not in self.state:
                self.state[key] = 'success'  # 원래 3D 복원은 끝나 있음
            if action == 'start' and self.state.get(key, 'not_started') == 'not_started':
                self.started.append(key); self.state[key] = 'running'
            return {'state': self.state.get(key, 'not_started')}
        p = patch.object(dw, 'call', side_effect=call); p.start(); self.addCleanup(p.stop)
        self.cut = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        p = patch.object(dw, 'mano_cutoff', side_effect=lambda *_: self.cut); p.start(); self.addCleanup(p.stop)
        start = dt.datetime.fromtimestamp(self.batch_start, dt.timezone.utc)
        self.run = SimpleNamespace(conf={'sources': SRC[:2], 'redo_3d': True}, start_date=start, queued_at=None, logical_date=None)
        self.poke = lambda: dw.poll_mano_batch(dag_run=self.run)

    def finish_reset(self, run):
        self.resets[run]['state'] = 'success'
        for s in ('prepare_reconstruction', 'reconstruct', 'hands', 'validate', 'publish'):
            self.state[(run, s)] = 'not_started'   # Worker 초기화가 3D 복원부터 작업을 되돌림
        self.drift.pop(run, None)                  # 새 코드로 다시 고정

    def test_old_episode_redoes_3d_then_mano(self):
        self.assertFalse(self.poke())
        self.assertIn(('reset', 'day_a', None), self.calls)
        self.assertEqual(self.started, [])
        self.assertFalse(self.poke())                       # 초기화 중: 다시 요청하지 않고 기다림
        self.assertEqual([c for c in self.calls if c[0] == 'reset'], [('reset', 'day_a', None)])
        self.finish_reset('day_a')
        self.assertFalse(self.poke())
        self.assertEqual(self.started, [('day_a', 'prepare_reconstruction')])
        self.state[('day_a', 'prepare_reconstruction')] = 'success'
        self.assertFalse(self.poke())
        self.assertEqual(self.started[-1], ('day_a', 'reconstruct'))
        self.state[('day_a', 'reconstruct')] = 'success'
        self.assertFalse(self.poke())
        self.assertEqual(self.started[-1], ('day_a', 'hands'))
        for s in dw.MANO_STAGES: self.state[('day_a', s)] = 'success'
        self.assertFalse(self.poke())
        # 업데이트 뒤 실행(day_b)은 3D 복원 없이 MANO만
        self.assertEqual(self.started[-1], ('day_b', 'hands'))
        self.assertNotIn(('day_b', 'reconstruct'), self.started)
        self.assertEqual([c for c in self.calls if c[0] == 'reset'], [('reset', 'day_a', None)])

    def test_redo_off_still_redoes_old_results(self):
        # 옵션을 꺼도 업데이트 전 3D 결과는 3D 복원부터 다시 합니다(그대로 MANO만 하면 코드 변경 오류로 실패).
        self.run.conf['redo_3d'] = False
        self.assertFalse(self.poke())
        self.assertIn(('reset', 'day_a', None), self.calls)
        self.assertEqual(self.started, [])

    def test_config_change_is_skipped(self):
        self.config_drift.add('day_a')
        self.assertFalse(self.poke())
        self.assertEqual(self.started, [('day_b', 'hands')])
        self.assertNotIn(('reset', 'day_a', None), self.calls)

    def test_old_manual_reset_is_not_mistaken_for_this_batch(self):
        self.resets['day_a'] = {'state': 'success', 'start': 'prepare_reconstruction', 'started': self.batch_start - 86400}
        self.assertFalse(self.poke())
        self.assertIn(('reset', 'day_a', None), self.calls)

    def test_cutoff_does_not_stop_episode_already_redoing(self):
        self.assertFalse(self.poke())
        self.finish_reset('day_a')
        self.cut = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
        self.assertFalse(self.poke())
        self.assertEqual(self.started, [('day_a', 'prepare_reconstruction')])

    def test_failed_redo_skips_to_next(self):
        self.assertFalse(self.poke())
        self.finish_reset('day_a')
        self.state[('day_a', 'prepare_reconstruction')] = 'failed'
        self.assertFalse(self.poke())
        self.assertEqual(self.started, [('day_b', 'hands')])

    def test_interrupted_redo_resumes_in_next_batch(self):
        self.assertFalse(self.poke())
        self.finish_reset('day_a')
        self.state[('day_a', 'prepare_reconstruction')] = 'success'
        # 다음 날 새 일괄 실행: 이미 다시 고정했으므로 코드 차이는 없지만 3D 복원이 끝나지 않음 → 이어서
        self.batch_start += 86400
        self.run.start_date = dt.datetime.fromtimestamp(self.batch_start, dt.timezone.utc)
        self.assertFalse(self.poke())
        self.assertEqual(self.started, [('day_a', 'reconstruct')])
        self.assertEqual([c for c in self.calls if c[0] == 'reset'], [('reset', 'day_a', None)])


if __name__ == '__main__':
    unittest.main()

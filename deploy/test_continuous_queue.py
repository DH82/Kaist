"""연속 모드 대기열: SAM3까지 끝난(또는 끝난/실패한) 실행은 목록에서 내리고, 진행 중인 것만 남깁니다."""
import os, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code/platform'))
import access, fleet
import platform_app as ui
from fastapi.testclient import TestClient

DAG = 'kaist_direct_pipeline_4090'


class ContinuousQueueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, KAIST_INTAKE_DB=str(Path(tmp.name) / 'c.sqlite'), KAIST_AIRFLOW_API_URL='')
        env.start(); self.addCleanup(env.stop)
        self.runs = {}   # run_id -> (dag run state, {stage: task state}) · 없으면 Airflow 조회 실패
        def api(method, path, **kw):
            rid = path.split('/dagRuns/')[1].split('/')[0]
            state, tasks = self.runs[rid]
            if path.endswith('/taskInstances'):
                return {'task_instances': [{'task_id': k, 'state': v} for k, v in tasks.items()]}
            return {'dag_run_id': rid, 'state': state, 'conf': {}}
        for obj, name, value in [(ui, 'UI_PASSWORD_FILE', ''), (ui, 'ALLOWED_HOSTS', {'testserver'}), (fleet, 'CONTROL', Path(tmp.name) / 'control')]:
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)
        for name, fn in [('local_api', api), ('call', lambda *a, **k: {'episodes': []}), ('raw_frames', lambda: [])]:
            p = patch.object(ui, name, side_effect=fn); p.start(); self.addCleanup(p.stop)

    def add(self, ep, state='started', run=None, age=60):
        with ui.cont_db() as c:
            c.execute("INSERT INTO sam_queue(raw_episode,operator,worker_id,sam_prompt,points,created,state,run_id,dag_id,episode,started) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (ep, 'PC1', ui.DEFAULT_WORKER, 'towel', '{}', time.time() - age, state, run, DAG, ep,
                                                       time.time() - age if state != 'waiting' else None))

    def states(self):
        with ui.cont_db() as c:
            return {r['raw_episode']: r['state'] for r in c.execute('SELECT raw_episode,state FROM sam_queue')}

    def settle(self):
        with ui.cont_db() as c:
            return ui.cont_settle(c)

    def test_finished_runs_leave_the_list_and_busy_ones_stay(self):
        done = {'prepare_raw': 'success', 'sam_settings_ready': 'success', 'sam3': 'success'}
        self.runs = {'r_sam': ('running', {'prepare_raw': 'success', 'sam_settings_ready': 'success', 'sam3': 'running'}),
                     'r_review': ('running', {**done, 'mask_approved': 'up_for_reschedule'}),
                     'r_success': ('success', {**done, 'mask_approved': 'success'}),
                     'r_failed': ('failed', {'prepare_raw': 'failed'}),
                     'r_failing': ('running', {'prepare_raw': 'success', 'sam_settings_ready': 'success', 'sam3': 'failed'})}
        for ep, run in [('ep_sam', 'r_sam'), ('ep_review', 'r_review'), ('ep_success', 'r_success'), ('ep_failed', 'r_failed'), ('ep_failing', 'r_failing')]:
            self.add(ep, run=run)
        self.add('ep_old', run='r_gone', age=3 * 86400)
        self.add('ep_wait', state='waiting')
        self.assertTrue(self.settle())
        self.assertEqual(self.states(), {'ep_sam': 'started', 'ep_review': 'done', 'ep_success': 'done', 'ep_failed': 'done',
                                         'ep_failing': 'done', 'ep_old': 'done', 'ep_wait': 'waiting'})
        # SAM3가 끝나면 다음 실행을 시작할 수 있고, 목록에서도 내려갑니다.
        self.runs['r_sam'] = ('running', {**done, 'mask_approved': 'up_for_reschedule'})
        self.assertFalse(self.settle())
        self.assertEqual(self.states()['ep_sam'], 'done')

    def test_unreachable_airflow_keeps_the_run_and_waits(self):
        self.add('ep_x', run='r_unknown')
        self.assertTrue(self.settle())
        self.assertEqual(self.states(), {'ep_x': 'started'})

    def test_status_lists_only_active_rows_but_counts_todays_starts(self):
        self.runs = {'r_sam': ('running', {'prepare_raw': 'running'})}
        self.add('ep_sam', run='r_sam'); self.add('ep_done', state='done', run='r_done'); self.add('ep_wait', state='waiting')
        self.add('ep_yesterday', state='done', run='r_y', age=2 * 86400)
        c = TestClient(ui.app); c.cookies.set('kaist_session', access.login('PC1', 'admin'))
        d = c.get('/api/continuous').json()
        self.assertEqual([r['raw_episode'] for r in d['queue']], ['ep_sam', 'ep_wait'])
        self.assertEqual(d['started_today'], 2)
        self.assertEqual(d['queue'][0]['stage'], 'RAW 전처리')


if __name__ == '__main__':
    unittest.main()

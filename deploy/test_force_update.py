"""강제 업데이트: 관리자 요청(force) · UI가 진행 중인 작업 강제 종료 · Worker 전체 강제 종료(stop-all).
임시 NAS 폴더·가짜 Airflow/Worker·임시 jobs 폴더만 사용합니다."""
import importlib.util, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code/platform'))
import access, fleet
import platform_app as ui
from fastapi.testclient import TestClient

SRC = {'run_id': 'day_a', 'conf': {'episode_id': 'ep_20261117_000001'}}
BATCH = {'dag_run_id': 'mano_x', 'state': 'running', 'conf': {'sources': [SRC]}}
RUNS = [('gpu_4090_a', 'kaist_pc1', {'dag_run_id': 'r_track', 'state': 'running', 'conf': {'episode_id': 'ep_20261117_000002'}}),
        ('gpu_4090_a', 'kaist_pc1', {'dag_run_id': 'r_review', 'state': 'running', 'conf': {'episode_id': 'ep_20261117_000003'}})]


class ForceUpdateUiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name); (self.tmp / 'control').mkdir()
        env = patch.dict(os.environ, KAIST_INTAKE_DB=str(self.tmp / 'c.sqlite'), KAIST_AIRFLOW_API_URL='')
        env.start(); self.addCleanup(env.stop)
        for obj, name, value in [(fleet, 'CONTROL', self.tmp / 'control'), (fleet, 'PC_ID', 'pc1'), (fleet, 'MACHINE_PC', 'pc1'),
                                 (ui, 'UI_PASSWORD_FILE', ''), (ui, 'ALLOWED_HOSTS', {'testserver'})]:
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)
        self.failed, self.actions = [], []
        def action(name, payload, wid):
            self.actions.append((name, payload.get('run_id')))
            if name == 'stop-all': return {'stopped': ['ep_9 hands'], 'left': []}
            return {'stopped': ['track'] if payload.get('run_id') == 'r_track' else []}
        fakes = [('airflow_runs', lambda dag, **k: [BATCH]), ('active_runs', lambda *a, **k: RUNS),
                 ('run_waiting', lambda dag, r: r['dag_run_id'] == 'r_review'),
                 ('fail_airflow_run', lambda dag, run, note: self.failed.append((dag, run, note))),
                 ('worker_action', action), ('active_dags', lambda: [('gpu_4090_a', 'pipeline', 'kaist_pc1')])]
        for name, fn in fakes:
            p = patch.object(ui, name, side_effect=fn); p.start(); self.addCleanup(p.stop)
        self.h = {'Origin': 'http://testserver', 'X-Platform-CSRF': ui.CSRF}

    def order_stop(self, req):
        d = self.tmp / 'control/update/pc1'
        (d / 'stop.json').write_text(json.dumps({'id': req['id'], 'at': time.time()}))

    def test_stop_waits_for_updater_order_then_runs_once(self):
        req = fleet.update_request('pc1', '관리자@pc1', force=True)
        ui.force_update_stop()
        self.assertEqual(self.actions, [])                       # 빌드가 끝나 stop.json이 오기 전에는 아무것도 멈추지 않음
        self.order_stop(req)
        ui.force_update_stop()
        done = json.loads((self.tmp / 'control/update/pc1/stopped.json').read_text())
        self.assertEqual(done['id'], req['id'])
        self.assertEqual([f[1] for f in self.failed], ['mano_x', 'r_track'])   # 검수 대기 실행(r_review)은 그대로
        self.assertIn(('stop', 'day_a'), self.actions)
        self.assertIn(('stop-all', None), self.actions)
        self.assertNotIn(('stop', 'r_review'), self.actions)
        self.assertEqual(set(done['stopped']), {'MANO 일괄 실행 mano_x', 'ep_20261117_000002 (track)', 'ep_9 hands'})
        self.assertEqual(done['errors'], [])
        n = len(self.actions)
        ui.force_update_stop()
        self.assertEqual(len(self.actions), n)                   # 한 번만

    def test_normal_request_or_other_id_does_not_stop(self):
        req = fleet.update_request('pc1', '관리자@pc1')
        self.order_stop(req)
        ui.force_update_stop()
        forced = fleet.update_request('pc1', '관리자@pc1', force=True)
        ui.force_update_stop()                                   # stop.json이 이전 요청 것
        self.assertEqual(self.actions, [])
        self.order_stop(forced); ui.force_update_stop()
        self.assertTrue(self.actions)

    def test_one_failure_does_not_stop_the_rest(self):
        self.order_stop(fleet.update_request('pc1', '관리자@pc1', force=True))
        def broken(dag, run, note):
            if run == 'mano_x': raise RuntimeError('airflow down')
            self.failed.append((dag, run, note))
        with patch.object(ui, 'fail_airflow_run', side_effect=broken):
            ui.force_update_stop()
        done = json.loads((self.tmp / 'control/update/pc1/stopped.json').read_text())
        self.assertIn('airflow down', ' '.join(done['errors']))
        self.assertEqual([f[1] for f in self.failed], ['r_track'])

    def test_admin_api_force_converts_pending_request(self):
        c = TestClient(ui.app); c.cookies.set('kaist_session', access.login('관리자', 'admin'))
        c.post('/api/fleet/update', headers=self.h, json={'pcs': ['pc2'], 'action': 'request'})
        first = fleet.update_info('pc2')['request']
        self.assertFalse(first.get('force'))
        self.assertEqual(c.post('/api/fleet/update', headers=self.h, json={'pcs': ['pc2', 'pc3'], 'action': 'force'}).json()['pcs'], ['pc2', 'pc3'])
        second = fleet.update_info('pc2')['request']
        self.assertEqual((second['id'], second['force'], second['forced_by']), (first['id'], True, first['requested_by']))
        self.assertTrue(fleet.update_info('pc3')['request']['force'])
        op = TestClient(ui.app); op.cookies.set('kaist_session', access.login('PC1', 'operator'))
        self.assertEqual(op.post('/api/fleet/update', headers=self.h, json={'pcs': ['pc2'], 'action': 'force'}).status_code, 403)


class WorkerStopAllTests(unittest.TestCase):
    """runner.stop_all: 대기·실행 중인 단계 작업 프로세스를 모두 끝내고 '사용자 강제 종료'로 기록(검수 확인·끝난 작업은 그대로)."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        spec = importlib.util.spec_from_file_location('runner_force_test', ROOT / 'worker/app/worker/runner.py')
        self.r = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.r)
        self.r.JOBS = Path(tmp.name) / 'jobs'; self.r.JOBS.mkdir()
        self.procs = []

    def job(self, stage, state, alive=True):
        d = self.r._job_dir('run1', 'ep_1', stage); d.mkdir()
        self.r.write(d / 'request.json', {'stage': stage, 'episode': 'ep_1', 'run_id': 'run1'})
        st = {'state': state, 'stage': stage, 'created': time.time()}
        if alive:
            # 실제 runner처럼 명령줄에 작업 폴더가 들어간 프로세스(+자식)
            p = subprocess.Popen([sys.executable, '-c', 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);time.sleep(60)', str(d)])
            self.procs.append(p); self.addCleanup(p.kill); st['pid'] = p.pid
        self.r.write(d / 'status.json', st)
        return d

    def test_kills_running_and_queued_keeps_gate_and_finished(self):
        run = self.job('reconstruct', 'running'); queued = self.job('hands', 'queued')
        gate = self.job('mask_approved', 'running'); done = self.job('track', 'success', alive=False)
        time.sleep(0.3)
        t = time.time(); res = self.r.stop_all({})
        self.assertLess(time.time() - t, 15)
        self.assertEqual(sorted(res['stopped']), ['ep_1 hands', 'ep_1 reconstruct']); self.assertEqual(res['left'], [])
        for d in (run, queued):
            st = json.loads((d / 'status.json').read_text())
            self.assertEqual((st['state'], st['stopped_by_user']), ('interrupted', True))
        self.assertEqual(self.procs[0].wait(5), self.procs[0].returncode)
        self.assertEqual(json.loads((gate / 'status.json').read_text())['state'], 'running')
        self.assertEqual(json.loads((done / 'status.json').read_text())['state'], 'success')


if __name__ == '__main__':
    unittest.main()

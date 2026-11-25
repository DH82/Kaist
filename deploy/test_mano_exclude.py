"""3D 복원 일괄 실행 대기 목록에서 제거 = 작업 환경에서 내리고 작업 데이터 삭제.
UI 쪽은 격리된 DB와 가짜 Airflow/Worker, 삭제 본체(episode_purge)는 임시 폴더만 사용합니다."""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code/platform'))
sys.path.insert(0, str(ROOT / 'worker/app/dags'))
import access
import fleet
import platform_app as ui
import episode_purge
from fastapi.testclient import TestClient

RUNS = [{'dag_run_id': 'day_a', 'state': 'success', 'end_date': '2026-11-25T01:00:00Z', 'conf': {'episode_id': 'ep_20261116_000001'}},
        {'dag_run_id': 'day_b', 'state': 'success', 'end_date': '2026-11-25T02:00:00Z', 'conf': {'episode_id': 'ep_20261116_000002'}}]


class ManoDeleteUiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, KAIST_INTAKE_DB=str(Path(tmp.name) / 'c.sqlite'), KAIST_AIRFLOW_API_URL='')
        env.start(); self.addCleanup(env.stop)
        ctl = tempfile.TemporaryDirectory(); self.addCleanup(ctl.cleanup)
        p = patch.object(fleet, 'CONTROL', Path(ctl.name) / 'control'); p.start(); self.addCleanup(p.stop)
        self.batch, self.actions, self.excluded, self.refuse = [], [], [], False
        def action(name, payload, wid):
            self.actions.append((name, payload['episode']))
            if name == 'purge' and self.refuse:
                raise ui.HTTPException(409, 'GPU PC 처리 실패: 이 에피소드의 단계 작업이 실행 중입니다.')
            return {'state': 'running' if name == 'purge' else 'success', 'bytes': 1500000000}
        for name, value in [('UI_PASSWORD_FILE', ''), ('ALLOWED_HOSTS', {'testserver'})]:
            p = patch.object(ui, name, value); p.start(); self.addCleanup(p.stop)
        ui.mano_excluded(fresh=True)
        for name, fn in [('mano_ready_runs', lambda: iter(RUNS)), ('batch_history', lambda: {}),
                         ('waiting_label', lambda run: '3D 복원 대기'),
                         ('local_api', lambda method, path, **k: next(r for r in RUNS if path.endswith(r['dag_run_id']))),
                         ('active_batch_sources', lambda episode=None, run_id=None: self.batch),
                         ('active_runs', lambda *a, **k: []), ('worker_action', action), ('update_block', lambda: None)]:
            p = patch.object(ui, name, side_effect=fn); p.start(); self.addCleanup(p.stop)
        p = patch.object(fleet, 'raw_exclude', side_effect=self.excluded.append); p.start(); self.addCleanup(p.stop)
        with access.db() as c:
            for r in RUNS:
                c.execute('INSERT INTO episode_owners VALUES(?,?)', (r['conf']['episode_id'], 'PC1'))
        self.client = TestClient(ui.app); self.addCleanup(self.client.close)
        self.client.cookies.set('kaist_session', access.login('PC1', 'operator'))
        self.h = {'Origin': 'http://testserver', 'X-Platform-CSRF': ui.CSRF}

    def waiting(self):
        return sorted(r['run'] for r in self.client.get('/api/pc1/mano', headers=self.h).json())

    def delete(self, run):
        return self.client.post('/api/pc1/mano/delete', headers=self.h, json={'run_id': run})

    def test_delete_removes_everywhere_and_starts_purge(self):
        self.assertEqual(self.waiting(), ['day_a', 'day_b'])
        r = self.delete('day_a'); self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.actions, [('purge', 'ep_20261116_000001')])
        self.assertEqual(self.waiting(), ['day_b'])
        self.assertEqual(self.excluded, ['ep_20261116_000001'])            # RAW는 모든 PC에서 제외
        self.assertEqual(ui.mano_progress(RUNS[0], {}), ('데이터 삭제됨', False))
        with access.db() as c:                                               # 작업 현황에서도 내림
            self.assertEqual([r['episode'] for r in c.execute("SELECT episode FROM ui_hidden WHERE scope='jobs'")], ['ep_20261116_000001'])
        gone = self.client.get('/api/pc1/mano/deleted', headers=self.h).json()
        self.assertEqual([(g['run'], g['state']) for g in gone], [('day_a', 'success')])
        # 삭제한 실행은 일괄 실행도, 단계 초기화도 할 수 없습니다.
        self.assertEqual(self.client.post('/api/pc1/mano', headers=self.h, json={'runs': ['day_a', 'day_b']}).status_code, 409)
        with self.assertRaises(ui.HTTPException) as e:
            ui.start_reset(ui.ResetJob(dag_id=ui.DAY_DAG, run_id='day_a', stage='track'), who='test')
        self.assertEqual(e.exception.status_code, 409)

    def test_listed_only_rows_keep_data_and_can_be_restored(self):
        """삭제 기능 이전에 목록에서만 제외해 둔 기록: 데이터 삭제로 취급하지 않고, 되돌리거나 확인 후 삭제할 수 있습니다."""
        with ui.mano_excluded_db() as c:
            c.execute('INSERT INTO mano_excluded(run_id,episode,name,at) VALUES(?,?,?,?)', ('day_a', 'ep_20261116_000001', 'PC1', 1795569300.0))
        ui.mano_excluded(fresh=True)
        self.assertEqual(self.waiting(), ['day_b'])
        self.assertEqual(ui.mano_progress(RUNS[0], {}), ('3D 복원 대기', True))
        gone = self.client.get('/api/pc1/mano/deleted', headers=self.h).json()
        self.assertEqual([(g['run'], g['state']) for g in gone], [('day_a', 'listed_only')])
        self.assertEqual(self.actions, [])                                   # 삭제 상태 조회도 하지 않음
        self.assertEqual(self.client.post('/api/pc1/mano/restore', headers=self.h, json={'run_id': 'day_a'}).status_code, 200)
        self.assertEqual(self.waiting(), ['day_a', 'day_b'])
        # 데이터를 삭제한 것은 되돌릴 수 없습니다.
        self.assertEqual(self.delete('day_a').status_code, 200)
        self.assertEqual(self.client.post('/api/pc1/mano/restore', headers=self.h, json={'run_id': 'day_a'}).status_code, 409)

    def test_nothing_changes_when_worker_refuses(self):
        self.refuse = True
        self.assertEqual(self.delete('day_a').status_code, 409)
        self.assertEqual(self.waiting(), ['day_a', 'day_b'])
        self.assertEqual(self.excluded, [])

    def test_refused_while_in_running_batch(self):
        self.batch = [('batch', 'src')]
        self.assertEqual(self.delete('day_a').status_code, 409)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.waiting(), ['day_a', 'day_b'])


class EpisodePurgeTests(unittest.TestCase):
    def test_only_this_episode_work_data_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); project = tmp / 'work/pc1'; nas = tmp / 'nas'
            ep, other = 'ep_20261116_000001', 'ep_20261116_000002'
            made = []
            for root in (project, nas / 'work/pc9'):
                for rel in ('01_raw', '03_prepared', '04_masks/approved', '05_tracking/approved', '06_reconstruction', '_airflow',
                            '99_history/reset', '99_history/hands', '99_history/reset_results'):
                    for e in (ep, other):
                        d = root / rel / e; d.mkdir(parents=True); (d / 'f.bin').write_bytes(b'x' * 10); made.append(d)
                for rel in ('99_history/manifests/{}.json', '99_history/nas_sync/{}.json'):
                    for e in (ep, other):
                        f = root / rel.format(e); f.parent.mkdir(parents=True, exist_ok=True); f.write_text('{}'); made.append(f)
            keep = [nas / 'rgbd_raw' / ep, nas / 'results/pc9/cloth' / ep / 'key']
            for d in keep:
                d.mkdir(parents=True); (d / 'f.bin').write_bytes(b'x')
            cfg = {'project_dir': str(project), 'nas_mount': str(nas), 'nas_results_subdir': 'results/pc9'}
            with patch.object(Path, 'is_mount', lambda self: True):
                rec = episode_purge.purge(cfg, ep, log=lambda m: None)
            for p in made:
                kept = other in str(p) or 'reset_results' in str(p)
                self.assertEqual(p.exists(), kept, p)
            for d in keep:                                  # RAW 원본·NAS 최종 결과는 그대로
                self.assertTrue((d / 'f.bin').is_file(), d)
            self.assertEqual(rec['bytes'], 2 * (8 * 10 + 2 * 2))
            with self.assertRaises(ValueError):
                episode_purge.purge(cfg, '../x', log=lambda m: None)


if __name__ == '__main__':
    unittest.main()

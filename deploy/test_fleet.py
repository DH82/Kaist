"""NAS 관제(상태 SQLite·수신함)와 MANO 이관 가져오기. 임시 폴더만 사용합니다."""
import importlib.util, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code/platform'))
import fleet


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


class FleetFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        p = patch.object(fleet, 'CONTROL', Path(self.tmp.name) / 'control'); p.start(); self.addCleanup(p.stop)

    def publish(self, pc, **snap):
        with patch.object(fleet, 'PC_ID', pc):
            fleet.publish_status(snap)

    def test_status_roundtrip_and_idle(self):
        self.publish('pc1', gpu=[{'idx': 0, 'name': '4090', 'uuid': 'u', 'used_mb': 1000, 'total_mb': 24000, 'util': 3, 'processes': 0}],
                     runs=[{'run_id': 'r', 'dag': 'd', 'mode': 'pipeline', 'episode': 'ep_1', 'raw_episode': 'ep_20261116_000001',
                            'operator': 'PC1', 'sam_prompt': 'towel', 'state': 'success', 'stage': None, 'started': None, 'ended': None}],
                     raw_started=['ep_20261116_000001'])
        self.publish('pc2', gpu=[{'idx': 0, 'name': '4090', 'uuid': 'u', 'used_mb': 20000, 'total_mb': 24000, 'util': 90, 'processes': 2}])
        s = {p['pc']: p for p in fleet.read_all()}
        self.assertTrue(s['pc1']['online'] and s['pc1']['idle'])
        self.assertFalse(s['pc2']['idle'])
        self.assertTrue(s['pc3'].get('missing'))
        with patch.object(fleet, 'PC_ID', 'pc2'):
            self.assertEqual(fleet.started_elsewhere(), {'ep_20261116_000001': 'pc1'})
        # NAS의 상태 파일은 일반 sqlite3로도 조회됩니다.
        out = subprocess.run([sys.executable, '-c', 'import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute("select sam_prompt from runs").fetchone()[0])',
                              str(fleet.CONTROL / 'status/pc1.sqlite')], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), 'towel')

    def test_stale_pc_is_offline(self):
        self.publish('pc1', gpu=[{'idx': 0, 'name': 'g', 'uuid': 'u', 'used_mb': 0, 'total_mb': 24000, 'util': 0, 'processes': 0}])
        with patch.object(fleet.time, 'time', return_value=time.time() + 600):
            st = fleet.read_pc('pc1')
        self.assertFalse(st['online']); self.assertFalse(st['idle'])

    def test_inbox_claimed_once(self):
        cmd = fleet.send('pc3', {'type': 'run_raw', 'episode': 'ep_1'})
        with patch.object(fleet, 'PC_ID', 'pc3'):
            got = fleet.claim(); again = fleet.claim()
        self.assertEqual([c['id'] for c in got], [cmd['id']]); self.assertEqual(again, [])
        with self.assertRaises(ValueError):
            fleet.send('pc9', {'type': 'x'})


class TransferImportTests(unittest.TestCase):
    def test_import_episode_and_code_compare(self):
        sw = load('sync_work_t', ROOT / 'worker/app/dags/sync_work.py')
        rt = load('kaist_runtime_t2', ROOT / 'worker/app/dags/kaist_runtime.py')
        with tempfile.TemporaryDirectory() as t:
            t = Path(t); nas = t / 'nas'; ep = 'ep_20261116_000001_1'
            src = nas / 'work/pc1'
            (src / '06_reconstruction' / ep).mkdir(parents=True)
            (src / '06_reconstruction' / ep / 'final_data.pkl').write_text('x')
            (src / '05_tracking/approved' / ep / 'mask').mkdir(parents=True)
            os.symlink('../../05_tracking/approved/' + ep + '/mask', src / '06_reconstruction' / ep / 'mask')
            rec = {'paths': ['05_tracking/approved/' + ep, '06_reconstruction/' + ep]}
            (src / '99_history/nas_sync').mkdir(parents=True)
            (src / '99_history/nas_sync' / f'{ep}.json').write_text(json.dumps(rec))
            cfg = {'nas_mount': str(nas), 'project_dir': str(t / 'pc3')}
            with patch.object(sw.Path, 'is_mount', return_value=True):
                out = sw.import_episode(cfg, 'pc1', ep, log=lambda m: None)
                self.assertEqual(out['paths'], rec['paths'])
                self.assertTrue((t / 'pc3/06_reconstruction' / ep / 'mask').is_symlink())
                with self.assertRaises(RuntimeError):  # 같은 에피소드가 이미 있으면 거부
                    sw.import_episode(cfg, 'pc1', ep, log=lambda m: None)
                with self.assertRaises(ValueError):
                    sw.import_episode(cfg, 'pc7', ep, log=lambda m: None)
        a = {'scripts_dir': '/a/00_scripts', 'pipeline_root': '/a/ws'}
        b = {'scripts_dir': '/home/x/00_scripts', 'pipeline_root': '/home/x/ws'}
        dp = '/kaist-full/PhysTwin/Mono/data_process/'
        self.assertEqual(rt.code_by_role({'/a/00_scripts/1.py': 'h', '/a/ws' + dp + 'm.py': 'k'}, a),
                         rt.code_by_role({'/home/x/00_scripts/1.py': 'h', '/home/x/ws' + dp + 'm.py': 'k'}, b))

    def test_runner_transfer_validation(self):
        rn = load('runner_t', ROOT / 'worker/app/worker/runner.py')
        base = {'episode': 'ep_1_1', 'run_id': 'transfer__pc1__x'}
        rn.folder(dict(base, stage='adopt', transfer_from='pc1', transfer_key='a' * 24))
        for bad in [dict(base, stage='adopt'), dict(base, stage='hands', transfer_from='pc1', transfer_key='a' * 24),
                    dict(base, stage='adopt', transfer_from='pc9', transfer_key='a' * 24)]:
            with self.assertRaises(ValueError):
                rn.folder(bad)


if __name__ == '__main__':
    unittest.main()

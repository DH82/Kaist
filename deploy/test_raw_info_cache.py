"""RAW 목록(/worker/info) 캐시: 워커는 NAS 폴더를 요청마다 다시 훑지 않고, UI는 마지막 목록을 바로 씁니다."""
import hashlib, json, os, re, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code/platform'))
import platform_app as ui
from fastapi import HTTPException


def worker_cache():
    """worker_service.py는 비밀 파일·GPU 설정이 있어야 import되므로 캐시 부분만 떼어 실행합니다."""
    src = (ROOT / 'worker/app/worker/worker_service.py').read_text()
    part = src[src.index('def intake_fingerprint(folder):'):src.index("@app.get('/worker/intake/fingerprint')")]
    ns = {'json': json, 'os': os, 're': re, 'Path': Path, 'HTTPException': HTTPException}
    exec(part, ns)
    return ns


def wait_idle(ns):
    for _ in range(500):
        with ns['_INFO_DONE']:
            if not ns['_INFO']['busy']: return
        time.sleep(0.01)
    raise AssertionError('refresh did not finish')


class WorkerCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.raw = Path(self.tmp.name)
        for name in ('ep_20261118_000001', 'ep_20261118_000002', 'not_an_episode'):
            (self.raw / name).mkdir()
        (self.raw / 'ep_file').write_text('x')
        self.value = {'episode': 'ep_20261118_000001', 'files': [1, 2]}
        (self.raw / 'ep_20261118_000001/upload_manifest.json').write_text(json.dumps(self.value))
        (self.raw / 'ep_20261118_000001/upload_complete.json').write_text('{}')
        self.ns = worker_cache()

    def tearDown(self):
        wait_idle(self.ns); self.tmp.cleanup()

    def test_list_and_fingerprints_match_the_uncached_scan(self):
        episodes, _ = self.ns['raw_info'](str(self.raw)); wait_idle(self.ns)
        _, prints = self.ns['raw_info'](str(self.raw))
        self.assertEqual(episodes, ['ep_20261118_000001', 'ep_20261118_000002'])
        self.assertEqual(prints, {'ep_20261118_000001': hashlib.sha256(json.dumps(self.value, sort_keys=True).encode()).hexdigest()})

    def test_fresh_list_is_served_without_touching_the_nas(self):
        self.ns['raw_info'](str(self.raw)); wait_idle(self.ns)
        with patch.object(self.ns['os'], 'scandir', side_effect=AssertionError('rescanned')):
            self.assertEqual(len(self.ns['raw_info'](str(self.raw))[0]), 2)

    def test_stale_list_returns_at_once_and_refreshes_behind(self):
        self.ns['INFO_FRESH'] = 0
        self.ns['raw_info'](str(self.raw)); wait_idle(self.ns)
        (self.raw / 'ep_20261118_000003').mkdir()
        gate = threading.Event(); real = os.scandir
        def slow(path):
            gate.wait(5); return real(path)
        with patch.object(self.ns['os'], 'scandir', side_effect=slow):
            started = time.time()
            first, _ = self.ns['raw_info'](str(self.raw))
            again, _ = self.ns['raw_info'](str(self.raw))   # 새로 읽는 중: 또 시작하지 않고 예전 목록
            self.assertLess(time.time() - started, 1)
            self.assertEqual(len(first), 2); self.assertEqual(len(again), 2)
            gate.set(); wait_idle(self.ns)
        self.ns['INFO_FRESH'] = 60
        self.assertEqual(len(self.ns['raw_info'](str(self.raw))[0]), 3)

    def test_manifests_are_not_reread_until_recheck(self):
        self.ns['INFO_FRESH'] = 0
        self.ns['raw_info'](str(self.raw)); wait_idle(self.ns)
        with patch.dict(self.ns, {'intake_fingerprint': lambda folder: self.fail('reread ' + folder.name)}):
            self.ns['raw_info'](str(self.raw)); wait_idle(self.ns)
        self.ns['INFO_RECHECK'] = 0
        (self.raw / 'ep_20261118_000001/upload_complete.json').unlink()
        self.ns['raw_info'](str(self.raw)); wait_idle(self.ns)
        self.assertEqual(self.ns['raw_info'](str(self.raw))[1], {})

    def test_unreadable_root_is_an_error_not_an_empty_list(self):
        with self.assertRaises(HTTPException) as caught:
            self.ns['raw_info'](str(self.raw / 'missing'))
        self.assertEqual(caught.exception.status_code, 503)


class UiCacheTests(unittest.TestCase):
    def setUp(self):
        ui._info_cache.clear(); self.calls = 0

    def tearDown(self):
        for _ in range(500):
            if not any(slot['busy'] for slot in ui._info_cache.values()): break
            time.sleep(0.01)
        ui._info_cache.clear()

    def test_slow_worker_does_not_empty_the_queue(self):
        gate = threading.Event(); gate.set()
        def call(method, path, **kw):
            self.calls += 1; gate.wait(5)
            return {'episodes': ['ep_a'] * self.calls, 'project_dir': '/p'}
        with patch.object(ui, 'call', call), patch.object(ui, 'INFO_FRESH', 0), patch.object(ui, 'INFO_WAIT', 0.1):
            self.assertEqual(ui.raw_worker_info('w')['episodes'], ['ep_a'])
            gate.clear(); started = time.time()
            self.assertEqual(ui.raw_worker_info('w')['episodes'], ['ep_a'])   # 워커가 답하지 않아도 마지막 목록
            self.assertEqual(ui.raw_worker_info('w')['episodes'], ['ep_a'])
            self.assertLess(time.time() - started, 1)
            self.assertEqual(self.calls, 2)                                    # 뒤에서 다시 받는 요청은 하나뿐
            gate.set()

    def test_no_list_yet_is_reported(self):
        def call(method, path, **kw): raise OSError('worker down')
        with patch.object(ui, 'call', call):
            with self.assertRaises(HTTPException) as caught: ui.raw_worker_info('w', first=1)
        self.assertEqual(caught.exception.status_code, 503)

    def test_callers_get_their_own_copy(self):
        with patch.object(ui, 'call', lambda *a, **kw: {'episodes': ['ep_a']}):
            ui.raw_worker_info('w')['central_ready'] = True
            self.assertNotIn('central_ready', ui.raw_worker_info('w'))

    def test_nas_lookup_is_remembered_and_reread_after_a_local_change(self):
        state = {'claims': {'ep_a': 'pc2'}, 'reads': 0}
        def read():
            state['reads'] += 1; return dict(state['claims'])
        self.assertEqual(ui.nas_view('claims', read), {'ep_a': 'pc2'})
        state['claims']['ep_b'] = 'pc3'
        self.assertEqual(ui.nas_view('claims', read), {'ep_a': 'pc2'})          # 20초 안: NAS를 다시 읽지 않음
        self.assertEqual(state['reads'], 1)
        ui.forget('nas:claims')                                                # 이 PC가 바꾼 직후: 새로 읽은 값
        self.assertEqual(ui.nas_view('claims', read), {'ep_a': 'pc2', 'ep_b': 'pc3'})

    def test_slow_nas_keeps_the_last_value(self):
        gate = threading.Event(); gate.set()
        def read():
            gate.wait(5); return {'ep_a': 'pc2'}
        with patch.object(ui, 'NAS_FRESH', 0), patch.object(ui, 'INFO_WAIT', 0.1):
            ui.nas_view('claims', read)
            gate.clear(); started = time.time()
            self.assertEqual(ui.nas_view('claims', read), {'ep_a': 'pc2'})
            self.assertLess(time.time() - started, 1); gate.set()

    def test_unread_nas_raises_instead_of_pretending_empty(self):
        def read(): raise OSError('nas down')
        with patch.object(ui, 'NAS_FIRST', 0.2):
            with self.assertRaises(OSError): ui.nas_view('x', read)


class RestartTests(unittest.TestCase):
    """UI를 재시작해도 마지막 값을 파일에서 올려 바로 화면을 띄웁니다."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.file = Path(self.tmp.name) / 'view_cache.json'
        ui._info_cache.clear()

    def tearDown(self):
        ui._info_cache.clear(); self.tmp.cleanup()

    def test_last_values_survive_a_restart_and_refresh_behind(self):
        gate = threading.Event()
        with patch.object(ui, 'VIEW_FILE', self.file), patch.object(ui, 'VIEW_SAVE_EVERY', 0), patch.object(ui, 'INFO_WAIT', 0.1), patch.object(ui, 'NAS_WAIT', 0.1):
            with patch.object(ui, 'call', lambda *a, **kw: {'episodes': ['ep_a']}):
                ui.raw_worker_info('w')
            ui.nas_view('claims', lambda: {'ep_a': 'pc2'})
            ui.views_save(force=True)
            ui._info_cache.clear()                                   # 재시작
            self.assertEqual(ui.views_load(), 2)
            def slow(*a, **kw):
                gate.wait(5); return {'episodes': ['ep_a', 'ep_b']}
            with patch.object(ui, 'call', slow):
                started = time.time()
                self.assertEqual(ui.raw_worker_info('w')['episodes'], ['ep_a'])      # NAS를 기다리지 않음
                self.assertEqual(ui.nas_view('claims', lambda: (gate.wait(5), {})[1]), {'ep_a': 'pc2'})
                self.assertLess(time.time() - started, 1)
                gate.set()
                for _ in range(200):
                    if len(ui.raw_worker_info('w')['episodes']) == 2: break
                    time.sleep(0.02)
                self.assertEqual(ui.raw_worker_info('w')['episodes'], ['ep_a', 'ep_b'])  # 뒤에서 새로 읽은 값으로 바뀜

    def test_old_or_broken_file_is_ignored(self):
        with patch.object(ui, 'VIEW_FILE', self.file):
            self.file.write_text('{broken'); self.assertEqual(ui.views_load(), 0)
            self.file.write_text(json.dumps({'w': {'data': {'episodes': []}, 'at': time.time() - ui.VIEW_KEEP - 10}}))
            self.assertEqual(ui.views_load(), 0)


if __name__ == '__main__':
    unittest.main()

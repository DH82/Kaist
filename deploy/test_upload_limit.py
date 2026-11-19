"""NAS 작업 폴더 업로드의 낮 시간대 속도 제한(sync_work.upload_limit / rsync)."""
import datetime, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'worker/app/dags'))
import sync_work

SPEC = {'mb_per_s': 3, 'from_hour': 8, 'to_hour': 19, 'weekdays_only': True}
at = lambda day, hour, minute=0: datetime.datetime(2026, 11, day, hour, minute)   # 2026-11-19 = 목요일


class WindowTests(unittest.TestCase):
    def test_limited_only_in_weekday_daytime(self):
        self.assertEqual(sync_work.upload_limit(SPEC, at(19, 7, 59)), 0)
        self.assertEqual(sync_work.upload_limit(SPEC, at(19, 8)), 3072)
        self.assertEqual(sync_work.upload_limit(SPEC, at(19, 18, 59)), 3072)
        self.assertEqual(sync_work.upload_limit(SPEC, at(19, 19)), 0)
        self.assertEqual(sync_work.upload_limit(SPEC, at(21, 12)), 0)                       # 토요일
        self.assertEqual(sync_work.upload_limit({**SPEC, 'weekdays_only': False}, at(21, 12)), 3072)

    def test_window_can_start_and_end_on_the_half_hour(self):
        work = {'mb_per_s': 8, 'from': '09:30', 'to': '18:30', 'weekdays_only': True}
        self.assertEqual(sync_work.upload_limit(work, at(19, 9, 29)), 0)
        self.assertEqual(sync_work.upload_limit(work, at(19, 9, 30)), 8192)
        self.assertEqual(sync_work.upload_limit(work, at(19, 18, 29)), 8192)
        self.assertEqual(sync_work.upload_limit(work, at(19, 18, 30)), 0)
        self.assertEqual(sync_work.window_text(work), '09:30~18:30 평일에 8 MB/s')
        self.assertEqual(sync_work.upload_limit({'mb_per_s': 8, 'from': '9:xx', 'to': '18:30'}, at(19, 12)), 0)   # 잘못된 설정 = 제한 없음

    def test_window_may_cross_midnight(self):
        night = {'mb_per_s': 1, 'from_hour': 22, 'to_hour': 6, 'weekdays_only': False}
        self.assertEqual(sync_work.upload_limit(night, at(19, 23)), 1024)
        self.assertEqual(sync_work.upload_limit(night, at(19, 5)), 1024)
        self.assertEqual(sync_work.upload_limit(night, at(19, 12)), 0)

    def test_bad_setting_means_no_limit(self):
        for spec in (None, {}, {'mb_per_s': 'x'}, {'mb_per_s': 0}, {'mb_per_s': 3, 'from_hour': 8, 'to_hour': 8}, {'mb_per_s': 3, 'from_hour': 30}):
            self.assertEqual(sync_work.upload_limit(spec, at(19, 12)), 0, spec)


class RsyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        self.src, self.dst = root / 'src/ep', root / 'nas/ep'
        (self.src / 'sub').mkdir(parents=True)
        (self.src / 'a.bin').write_bytes(b'a' * 300_000); (self.src / 'sub/b.bin').write_bytes(b'b' * 300_000)

    def tearDown(self):
        self.tmp.cleanup()

    def test_limited_copy_is_complete(self):
        with patch.object(sync_work, 'upload_limit', return_value=100_000):
            sync_work.rsync(self.src, self.dst, limit=SPEC, log=lambda m: None)
        self.assertEqual(sync_work.rsync(self.src, self.dst, dry=True), [])

    def test_restarts_with_the_new_speed_when_the_window_changes(self):
        """느린 속도(1 KiB/s)로 올리는 중 제한 시간대가 끝나면 rsync를 다시 시작해 끝까지 올립니다."""
        state = {'calls': 0}; said = []
        def moving(spec, now=None):
            state['calls'] += 1
            return 1 if state['calls'] <= 2 else 0   # 시작할 때·첫 확인까지는 제한, 그 뒤 제한 없음
        with patch.object(sync_work, 'upload_limit', moving):
            sync_work.rsync(self.src, self.dst, limit=SPEC, log=said.append, poll=0.5)
        self.assertEqual(sync_work.rsync(self.src, self.dst, dry=True), [])
        self.assertEqual(len(said), 1); self.assertIn('다시 시작', said[0])

    def test_failure_is_still_reported(self):
        with patch.object(sync_work, 'RSYNC', ['rsync', '--no-such-option']), patch.object(sync_work, 'upload_limit', return_value=0):
            with self.assertRaises(RuntimeError): sync_work.rsync(self.src, self.dst, limit=SPEC, log=lambda m: None)

    def test_no_setting_runs_exactly_as_before(self):
        with patch.object(sync_work, '_rsync_limited', side_effect=AssertionError('limited path used')):
            sync_work.rsync(self.src, self.dst)
        self.assertEqual(sync_work.rsync(self.src, self.dst, dry=True), [])


if __name__ == '__main__':
    unittest.main()

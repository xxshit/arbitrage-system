import os
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"

import app as app_module


class StopWorker(BaseException):
    pass


class BackgroundWorkerResilienceTests(unittest.TestCase):
    def test_early_trend_worker_recovers_after_runtime_rule_read_failure(self):
        sleep_calls = 0

        def fake_sleep(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 3:
                raise StopWorker()

        with app_module.app.app_context(), \
                patch("app.runtime_interval", side_effect=[RuntimeError("temporary database error"), 1800]) as interval, \
                patch("app.scan_intraday_early_trends") as scan, \
                patch("app.mark_automation_status") as status, \
                patch("app.time.time", return_value=3600), \
                patch("app.time.sleep", side_effect=fake_sleep):
            with self.assertRaises(StopWorker):
                app_module.background_intraday_early_trend_scan()

        self.assertEqual(interval.call_count, 2)
        scan.assert_called_once_with()
        self.assertTrue(any(call.args[:2] == ("intraday_early_trend_scan", "error") for call in status.call_args_list))
        self.assertTrue(any(call.args[:2] == ("intraday_early_trend_scan", "success") for call in status.call_args_list))


if __name__ == "__main__":
    unittest.main()

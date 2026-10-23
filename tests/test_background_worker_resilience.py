import os
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace
from background_runtime import WorkerRuntime, mysql_engine_options

os.environ["DATABASE_URL"] = "sqlite://"

import app as app_module


class StopWorker(BaseException):
    pass


class BackgroundWorkerResilienceTests(unittest.TestCase):
    def test_health_diagnostics_require_admin_and_do_not_expose_configuration(self):
        with app_module.app.test_request_context():
            app_module.g.current_user = SimpleNamespace(role="viewer")
            self.assertEqual(app_module.runtime_health()[1], 403)
            app_module.g.current_user = SimpleNamespace(role="admin")
            payload = app_module.runtime_health().get_json()
            self.assertIn("workers", payload)
            self.assertIn("pid", payload)
            self.assertNotIn("DATABASE_URL", str(payload))

    def test_guard_retries_with_bounded_delay_and_does_not_leak_exception_values(self):
        runtime, logger = WorkerRuntime(), Mock()
        calls = 0
        def target():
            nonlocal calls
            calls += 1
            if calls == 7:
                raise StopWorker()
            raise RuntimeError("secret-db-parameters")
        with patch("background_runtime.time.sleep") as sleep:
            with self.assertRaises(StopWorker):
                runtime.guard(target, logger)()
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10, 20, 40, 60, 60])
        self.assertNotIn("secret-db-parameters", str(logger.mock_calls))
        self.assertEqual(next(iter(runtime.snapshot()["workers"].values()))["restarts"], 6)

    def test_status_database_failure_does_not_escape(self):
        with patch("app.mark_automation_status", side_effect=RuntimeError("offline")):
            app_module.report_background_error("transfer_network_sync", ValueError("private"))

    def test_backfill_exception_is_reported_after_context_closed(self):
        with patch("app.load_latest_market_snapshot", side_effect=ValueError("failed")), \
                patch("app.report_background_error") as report, \
                patch("app.runtime_interval", return_value=60), \
                patch("app.time.sleep", side_effect=[None, StopWorker()]):
            with self.assertRaises(StopWorker):
                app_module.background_price_history_backfill()
        self.assertEqual(report.call_args.args[0], "price_history_backfill")
        self.assertIsInstance(report.call_args.args[1], ValueError)

    def test_pool_limits_are_bounded_and_sqlite_unchanged(self):
        self.assertEqual(mysql_engine_options("sqlite://"), {})
        options = mysql_engine_options("mysql+pymysql://example")
        self.assertEqual(options["pool_size"]+options["max_overflow"], 15)
        self.assertTrue(options["pool_pre_ping"])
        self.assertTrue(options["hide_parameters"])
        self.assertLessEqual(options["pool_timeout"], 5)
        self.assertEqual(options["connect_args"]["read_timeout"], 30)

    def test_connection_metrics_only_retain_count_age_thread_name(self):
        runtime, record = WorkerRuntime(), object()
        runtime.checkout("private-connection", record, None)
        state = runtime.snapshot()
        self.assertEqual(state["db_checked_out"], 1)
        self.assertNotIn("private", str(state))
        runtime.checkin(None, record)
        self.assertEqual(runtime.snapshot()["db_checked_out"], 0)

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

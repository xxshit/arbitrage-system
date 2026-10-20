"""Display conversions must not change stored instants or depend on server TZ."""
import ast
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from zoneinfo import ZoneInfo


SOURCE = ast.parse(Path(__file__).resolve().parents[1].joinpath("app.py").read_text())
FUNCTIONS = {node.name: node for node in SOURCE.body if isinstance(node, ast.FunctionDef)}
NAMESPACE = {"datetime": datetime, "timezone": timezone, "SHANGHAI_TZ": ZoneInfo("Asia/Shanghai")}
HELPERS = ["format_shanghai_time", "thought_watch_datetime"]
seed = next(node for node in SOURCE.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "THOUGHT_WATCH_SEED" for target in node.targets))
exec(compile(ast.Module(body=[seed, *[FUNCTIONS[name] for name in HELPERS]], type_ignores=[]), "app.py", "exec"), NAMESPACE)


@contextmanager
def server_timezone(zone):
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = zone
        time.tzset()
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


class DisplayTimezoneTests(unittest.TestCase):
    def field_values(self, function, field, **values):
        expressions = [value for node in ast.walk(FUNCTIONS[function]) if isinstance(node, ast.Dict)
                       for key, value in zip(node.keys, node.values)
                       if isinstance(key, ast.Constant) and key.value == field]
        self.assertTrue(expressions, f"{function}.{field} must remain covered")
        return [eval(compile(ast.Expression(value), "app.py", "eval"), dict(NAMESPACE, **values)) for value in expressions]

    def test_utc_naive_and_aware_input_have_same_beijing_day_across_server_timezones(self):
        stamp = datetime(2026, 10, 20, 16, 0, 5)
        fmt = NAMESPACE["format_shanghai_time"]
        for zone in ["UTC", "Asia/Bangkok", "Asia/Shanghai", "America/New_York"]:
            with self.subTest(zone=zone), server_timezone(zone):
                self.assertEqual(fmt(stamp), "2026-10-21 00:00:05")
                self.assertEqual(fmt(stamp.replace(tzinfo=timezone.utc)), "2026-10-21 00:00:05")
                self.assertEqual(fmt(stamp.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))), "2026-10-21 00:00:05")
                self.assertIsNone(fmt(None))
        self.assertIsNone(stamp.tzinfo)
        self.assertEqual(stamp.hour, 16)  # Never mutate the persisted value.

    def test_new_year_and_leap_day_roll_over_in_beijing(self):
        fmt = NAMESPACE["format_shanghai_time"]
        self.assertEqual(fmt(datetime(2026, 12, 31, 16)), "2027-01-01 00:00:00")
        self.assertEqual(fmt(datetime(2028, 2, 28, 16)), "2028-02-29 00:00:00")

    def test_account_device_security_and_rule_records_use_same_display_contract(self):
        stamp = datetime(2026, 10, 20, 16, 1, 2)
        fields = {
            "device_payload": ["first_bound_at", "last_login_at", "last_seen_at"],
            "list_users": ["created_at", "last_login_at", "last_seen_at", "password_changed_at"],
            "list_login_security_alerts": ["created_at"],
            "symbol_alias_payload": ["created_at", "updated_at"],
            "runtime_rule_payload": ["updated_at"],
        }
        row = SimpleNamespace(**{name: stamp for names in fields.values() for name in names})
        for function, names in fields.items():
            for name in names:
                with self.subTest(function=function, field=name):
                    self.assertEqual(self.field_values(function, name, row=row), ["2026-10-21 00:01:02"])

    def test_invite_times_keep_minute_precision_and_cross_day(self):
        stamp = datetime(2026, 10, 20, 16, 1, 2)
        row = SimpleNamespace(created_at=stamp, expires_at=stamp, used_at=stamp)
        for field in ["created_at", "expires_at"]:
            self.assertEqual(self.field_values("list_invites", field, row=row), ["2026-10-21 00:01"])
        self.assertEqual(self.field_values("list_invites", "used_at", row=row), ["2026-10-21 00:01:02"])
        self.assertEqual(self.field_values("create_invite", "expires_at", expires_at=stamp), ["2026-10-21 00:01"])

    def test_cached_and_live_market_snapshot_display_clocks_match(self):
        stamp = datetime(2026, 10, 20, 16, 1, 2)
        for function in ["load_latest_market_snapshot", "load_latest_dual_futures_snapshot",
                         "spot_futures_snapshot", "dual_futures_snapshot"]:
            with self.subTest(function=function):
                self.assertEqual(self.field_values(function, "updated_at", captured_at=stamp), ["00:01:02"])

    def test_basis_opening_and_divergence_times_are_beijing(self):
        stamp = datetime(2026, 10, 20, 16, 1, 2)
        row = SimpleNamespace(started_at=stamp, max_at=stamp, last_seen_at=stamp)
        for name in ["opened_at", "max_at"]:
            self.assertEqual(self.field_values("enrich_basis_openings", name, tracking=row), ["10-21 00:01:02"])
        for name in ["started_at", "observed_at"]:
            self.assertEqual(self.field_values("spot_divergence_payload", name, item=row), ["10-21 00:01:02 北京时间"])

    def test_trade_history_and_candle_fallback_use_beijing_not_local_server_time(self):
        stamp = datetime(2026, 10, 20, 16, 1, 2)
        plan = SimpleNamespace(created_at=stamp, opened_at=stamp, closed_at=stamp)
        for function in ["trade_validation", "trade_validation_detail"]:
            for field in ["created_at", "opened_at", "closed_at"]:
                self.assertEqual(self.field_values(function, field, plan=plan), ["10-21 00:01:02"])
        row = SimpleNamespace(bucket_at=stamp.replace(tzinfo=timezone.utc).timestamp())
        for zone in ["UTC", "Asia/Bangkok", "America/New_York"]:
            with server_timezone(zone):
                self.assertEqual(self.field_values("validation_candles", "time", row=row), ["10-21 00:01"] * 2)

    def test_beijing_horizon_dates_are_not_shifted_again(self):
        # These tables deliberately store naive BEIJING dates, unlike UTC alert tables.
        stamp = datetime(2026, 10, 21, 0, 1, 2)
        row = SimpleNamespace(anchor_at=stamp, review_after_at=stamp, expires_at=stamp, first_hit_at=stamp)
        self.assertEqual(self.field_values("trend_horizon_payload", "anchor_at", row=row), ["2026-10-21 00:01:02"])
        for field in ["review_after_at", "expires_at", "first_hit_at"]:
            self.assertEqual(self.field_values("trend_horizon_payload", field, row=row), ["10-21 00:01"])
        self.assertEqual(self.field_values("trend_horizon_payload", "at", event=SimpleNamespace(observed_at=stamp)), ["10-21 00:01"])

    def test_seed_watch_dates_stay_beijing_but_web_created_or_resumed_dates_are_utc(self):
        resolve, fmt = NAMESPACE["thought_watch_datetime"], NAMESPACE["format_shanghai_time"]
        row = SimpleNamespace(symbol="AKE/USDT", started_at=datetime(2026, 7, 16, 19, 7), stopped_at=None)
        self.assertEqual(fmt(resolve(row, "started_at")), "2026-07-16 19:07:00")
        self.assertIsNone(resolve(row, "stopped_at"))
        row.started_at = datetime(2026, 10, 20, 16)
        row.stopped_at = row.started_at + timedelta(minutes=15)
        self.assertEqual(fmt(resolve(row, "started_at")), "2026-10-21 00:00:00")
        self.assertEqual(fmt(resolve(row, "stopped_at")), "2026-10-21 00:15:00")
        self.assertEqual((resolve(row, "stopped_at") - resolve(row, "started_at")).total_seconds(), 900)
        row.symbol = "NEW/USDT"
        self.assertEqual(fmt(resolve(row, "started_at")), "2026-10-21 00:00:00")

    def test_thought_market_times_and_push_records_share_beijing_contract(self):
        stamp = datetime(2026, 10, 20, 16, 1, 2)
        row = SimpleNamespace(captured_at=stamp, sent_at=stamp, created_at=stamp)
        self.assertEqual(self.field_values("thought_market_context", "updated_at", dual=row, preferred=row), ["2026-10-21 00:01:02"] * 2)
        self.assertEqual(self.field_values("early_trend_thought_item", "entry_time", row=row), ["2026-10-21 00:01"])
        self.assertEqual(self.field_values("thought_watchlist_payload", "last_push_at", latest_push=row), ["2026-10-21 00:01:02"])


if __name__ == "__main__":
    unittest.main()

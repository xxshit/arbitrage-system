"""Exercise production timestamp code without app workers, credentials or I/O."""
import ast
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import time
import unittest
from zoneinfo import ZoneInfo


SOURCE = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text())
HELPERS = {"format_funding_time", "parse_funding_time"}
NAMESPACE = dict(datetime=datetime, timedelta=timedelta, timezone=timezone,
                 SHANGHAI_TZ=ZoneInfo("Asia/Shanghai"))
exec(compile(ast.Module(body=[node for node in SOURCE.body if isinstance(node, ast.FunctionDef)
                             and node.name in HELPERS], type_ignores=[]), "app.py", "exec"), NAMESPACE)
SPOT = next(node for node in SOURCE.body if isinstance(node, ast.FunctionDef)
            and node.name == "spot_futures_snapshot")
SPOT_NEXT = next(value for node in ast.walk(SPOT) if isinstance(node, ast.Dict)
                 for key, value in zip(node.keys, node.values)
                 if isinstance(key, ast.Constant) and key.value == "next_funding_time")


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


def millis(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


class FundingTimezoneTests(unittest.TestCase):
    def spot_time(self, value):
        return eval(compile(ast.Expression(SPOT_NEXT), "app.py", "eval"),
                    dict(NAMESPACE, funding_item={"nextFundingTime": value}))

    def test_actual_spot_builder_uses_beijing_not_server_timezone(self):
        for zone in ("UTC", "Asia/Bangkok", "Asia/Shanghai", "America/New_York"):
            with self.subTest(zone=zone), server_timezone(zone):
                self.assertEqual(self.spot_time(1792512000000), "10-21 00:00")
                self.assertEqual(self.spot_time("1792512000000"), "10-21 00:00")

    def test_exchange_time_not_inferred_from_period_or_current_hour(self):
        for utc, expected in (("2026-10-20T15:00:00+00:00", "10-20 23:00"),
                              ("2026-10-20T16:00:00+00:00", "10-21 00:00"),
                              ("2026-10-20T16:30:00+00:00", "10-21 00:30"),
                              ("2026-10-20T00:00:00+00:00", "10-20 08:00")):
            with self.subTest(utc=utc):
                self.assertEqual(self.spot_time(millis(utc)), expected)

    def test_new_year_leap_day_and_month_rollover(self):
        for utc, expected in (("2026-12-31T16:00:00+00:00", "01-01 00:00"),
                              ("2028-02-28T16:00:00+00:00", "02-29 00:00"),
                              ("2026-10-31T16:00:00+00:00", "11-01 00:00")):
            self.assertEqual(self.spot_time(millis(utc)), expected)

    def test_invalid_exchange_time_is_missing_not_a_guessed_future_time(self):
        for value in (None, "", 0, -1, "invalid", float("nan"), float("inf"), 10**30, True):
            with self.subTest(value=value):
                self.assertIsNone(NAMESPACE["format_funding_time"](value))
                self.assertEqual(self.spot_time(value), "—")

    def test_all_public_exchange_clocks_use_explicit_formatter(self):
        # Cached OKX times use this formatter in fetch_okx_funding; no server-local conversion.
        entries = [value for node in ast.walk(SOURCE) if isinstance(node, ast.Dict)
                   for key, value in zip(node.keys, node.values)
                   if isinstance(key, ast.Constant) and key.value == "next_funding_time"]
        clock_calls = [call for value in entries for call in ast.walk(value)
                       if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                       and call.func.id == "format_funding_time"]
        self.assertEqual(len(clock_calls), 4)  # spot Binance; dual Binance / Bybit / OKX
        self.assertFalse(any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                             and call.func.attr == "astimezone" for value in entries for call in ast.walk(value)))


if __name__ == "__main__":
    unittest.main()

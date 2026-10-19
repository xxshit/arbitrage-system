import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "rolling-funding-test")
import app as module


class RollingFundingTests(unittest.TestCase):
    def setUp(self):
        self.context = module.app.app_context()
        self.context.push()
        for model in (module.FundingRateRecord, module.ExchangeFundingRateRecord):
            model.__table__.create(module.db.engine, checkfirst=True)
        self.now = datetime(2026, 10, 20, 2, 43, 19, tzinfo=module.SHANGHAI_TZ)
        clock = patch.object(module, "datetime", wraps=datetime)
        clock.start().now.return_value = self.now
        self.addCleanup(clock.stop)
        for name, value in (
            ("FUNDING_STATISTICS_CACHE", {"ts": 0, "symbols": frozenset(), "data": {}}),
            ("DUAL_FUNDING_STATISTICS_CACHE", {"ts": 0, "key": frozenset(), "data": {}}),
        ):
            cache = patch.object(module, name, value)
            cache.start()
            self.addCleanup(cache.stop)
        module.db.session.begin_nested()

    def tearDown(self):
        module.db.session.rollback()
        module.db.session.remove()
        self.context.pop()

    def row(self, age, rate, milliseconds=0):
        return {"funding_time": int((self.now - timedelta(hours=age)).timestamp() * 1000) + milliseconds,
                "funding_rate": rate}

    def insert(self, exchange, rows, symbol="ROLLTEST/USDT"):
        model = module.FundingRateRecord if exchange == "Binance" else module.ExchangeFundingRateRecord
        module.db.session.execute(model.__table__.insert(), [
            {"symbol": symbol.replace("/", "") if exchange == "Binance" else symbol,
             **({"exchange": exchange} if exchange != "Binance" else {}), **row} for row in rows
        ])

    def groups(self):
        return [{"symbol": "ROLLTEST/USDT", "rows": [
            {"long_exchange": a, "short_exchange": b, "funding_difference": 9}
            for a, b in (("Bybit", "Binance"), ("Binance", "Bybit"), ("OKX", "Binance"))]}]

    def test_early_morning_sums_six_settlements_instead_of_only_today(self):
        self.insert("Binance", [self.row(age, .005) for age in (2.5, 6.5, 10.5, 14.5, 18.5, 22.5)])
        result = module.funding_statistics(["ROLLTESTUSDT"])["ROLLTESTUSDT"]
        self.assertAlmostEqual(result["previous"], .005)
        self.assertAlmostEqual(result["day_1"], .030)
        groups = [{"symbol": "ROLLTEST/USDT", "rows": [{"funding_rate": 9}]}]
        module.enrich_funding_statistics(groups)
        self.assertAlmostEqual(groups[0]["rows"][0]["funding_24h"], .030)
        self.assertEqual(groups[0]["rows"][0]["funding_rate"], 9)  # estimated current is never added

    def test_mixed_rates_are_added_with_sign_not_last_rate_times_count(self):
        self.insert("Binance", [self.row(age, rate) for age, rate in ((2, .005), (6, -.010), (10, .020), (14, 0), (18, .005), (22, .005))])
        result = module.funding_statistics(["ROLLTESTUSDT"])["ROLLTESTUSDT"]
        self.assertAlmostEqual(result["day_1"], .025)
        self.assertAlmostEqual(result["previous"], .005)

    def test_left_endpoint_excluded_right_included_and_future_ignored(self):
        self.insert("Binance", [self.row(24, 100), self.row(24, .01, 1), self.row(0, .02), self.row(0, 200, 1)])
        result = module.funding_statistics(["ROLLTESTUSDT"])["ROLLTESTUSDT"]
        self.assertAlmostEqual(result["day_1"], .03)
        self.assertAlmostEqual(result["previous"], .02)

    def test_hourly_boundary_counts_24_not_25_settlements(self):
        self.insert("Binance", [self.row(hour, .005) for hour in range(25)])
        self.assertAlmostEqual(module.funding_statistics(["ROLLTESTUSDT"])["ROLLTESTUSDT"]["day_1"], .120)

    def test_all_windows_roll_through_midnight_independently_of_timezone(self):
        before = datetime(2026, 10, 19, 23, 59, 59, tzinfo=module.SHANGHAI_TZ)
        after = before + timedelta(seconds=2)
        start_before = module.funding_statistics_window_starts(before)
        start_after = module.funding_statistics_window_starts(after)
        for days in (1, 3, 7, 30):
            self.assertEqual(start_after[days] - start_before[days], 2000)
            expected = after - timedelta(hours=days * 24)
            self.assertEqual(start_after[days], int(expected.timestamp() * 1000) + 1)
        self.assertEqual(start_after, module.funding_statistics_window_starts(after.astimezone(timezone.utc)))

    def test_all_periods_exclude_exact_left_endpoint_and_include_next_millisecond(self):
        for days in (1, 3, 7, 30):
            with self.subTest(days=days):
                symbol = f"ROLLBOUND{days}/USDT"
                self.insert("Binance", [self.row(days * 24, 10), self.row(days * 24, .01, 1),
                                        self.row(0, .02), self.row(0, 200, 1)], symbol)
                actual = module.funding_statistics([symbol.replace("/", "")])[symbol.replace("/", "")]
                self.assertAlmostEqual(actual[f"day_{days}"], .03)
                self.assertAlmostEqual(actual["previous"], .02)

    def test_hourly_settlements_count_24_72_168_and_720_not_an_extra_event(self):
        self.insert("Binance", [self.row(hour, .005) for hour in range(721)])
        actual = module.funding_statistics(["ROLLTESTUSDT"])["ROLLTESTUSDT"]
        for days in (1, 3, 7, 30):
            self.assertAlmostEqual(actual[f"day_{days}"], days * 24 * .005)

    def test_30_day_sql_includes_the_tail_previously_lost_by_calendar_prefilter(self):
        self.insert("Binance", [self.row(1, .02), self.row(719, .03), self.row(720, 9)])
        self.assertAlmostEqual(module.funding_statistics(["ROLLTESTUSDT"])["ROLLTESTUSDT"]["day_30"], .05)

    def test_dual_all_periods_follow_each_legs_actual_events_and_reverse_direction(self):
        long = [self.row(hour, .01 if hour % 16 else -.02) for hour in range(0, 745, 8)]
        short = [self.row(hour, .005 if hour % 6 else -.003) for hour in range(0, 745, 4)]
        self.insert("Bybit", long)
        self.insert("Binance", short)
        groups = self.groups()
        module.enrich_dual_funding_statistics(groups)
        reference = module.calculate_dual_funding_differences(long, short, self.now)
        end = int(self.now.timestamp() * 1000)
        for days, suffix in ((1, "24h"), (3, "3d"), (7, "7d"), (30, "30d")):
            start = end - days * 86400000
            expected = sum(row["funding_rate"] for row in short if start < row["funding_time"] <= end)
            expected -= sum(row["funding_rate"] for row in long if start < row["funding_time"] <= end)
            self.assertAlmostEqual(reference[f"day_{days}"], expected)
            self.assertAlmostEqual(groups[0]["rows"][0][f"funding_difference_{suffix}"], expected)
            self.assertAlmostEqual(groups[0]["rows"][1][f"funding_difference_{suffix}"], -expected)
        self.assertEqual(groups[0]["rows"][0]["funding_difference"], 9)

    def test_backfill_uses_31_day_buffer_unless_existing_history_covers_left_edge(self):
        end = int(self.now.timestamp() * 1000)
        full_start = end - 31 * 86400000
        self.assertEqual(module.funding_history_sync_start(None, None, self.now), full_start)
        self.assertEqual(module.funding_history_sync_start(end - 29 * 86400000, end - 3600000, self.now), full_start)
        self.assertEqual(module.funding_history_sync_start(end - 30 * 86400000, end - 3600000, self.now), end - 25 * 3600000)
        self.assertEqual(module.funding_history_sync_start(full_start, full_start, self.now), full_start)

    def test_binance_repairs_old_left_edge_and_keeps_buffer_without_counting_it(self):
        self.insert("Binance", [self.row(696, .01), self.row(1, .005)])
        self.insert("Binance", [self.row(745, 99)], "ROLLEXPIRED/USDT")
        payload = [{"fundingTime": row["funding_time"], "fundingRate": str(row["funding_rate"] / 100)}
                   for row in [self.row(745, 99), self.row(731, 5), self.row(718, .03), self.row(1, .005)]]
        with patch.object(module, "get_json", return_value=payload), patch.object(module.db.session, "commit"):
            actual = module.sync_funding_history(["ROLLTESTUSDT"])["ROLLTESTUSDT"]
        self.assertAlmostEqual(actual["day_30"], .045)
        stored = [row[0] for row in module.db.session.query(module.FundingRateRecord.funding_time).filter_by(symbol="ROLLTESTUSDT")]
        self.assertIn(self.row(731, 5)["funding_time"], stored)
        self.assertNotIn(self.row(745, 99)["funding_time"], stored)
        self.assertEqual(module.FundingRateRecord.query.filter_by(symbol="ROLLEXPIREDUSDT").count(), 0)
        self.assertEqual(actual, module.funding_statistics(["ROLLTESTUSDT"])["ROLLTESTUSDT"])

    def test_dual_repairs_old_left_edge_and_keeps_31_day_buffer_for_both_exchanges(self):
        for exchange in ("Bybit", "OKX"):
            self.insert(exchange, [self.row(696, .01), self.row(1, .005)])
            self.insert(exchange, [self.row(745, 99)], "ROLLEXPIRED/USDT")
        rows = [(row["funding_time"], row["funding_rate"]) for row in (self.row(731, 5), self.row(718, .03), self.row(1, .005))]
        with patch.object(module, "fetch_exchange_funding_history", return_value=rows) as fetch, patch.object(module.db.session, "commit"):
            self.assertEqual(module.sync_dual_funding_history([("Bybit", "ROLLTEST/USDT"), ("OKX", "ROLLTEST/USDT")]), 4)
        expected_start = int(self.now.timestamp() * 1000) - 31 * 86400000
        self.assertTrue(all(call.args[2] == expected_start for call in fetch.call_args_list))
        for exchange in ("Bybit", "OKX"):
            summary = module.settled_funding_summaries(module.ExchangeFundingRateRecord, {(exchange, "ROLLTEST/USDT")}, self.now)[(exchange, "ROLLTEST/USDT")]
            self.assertAlmostEqual(summary["day_30"], .045)
            stored = [row[0] for row in module.db.session.query(module.ExchangeFundingRateRecord.funding_time).filter_by(exchange=exchange, symbol="ROLLTEST/USDT")]
            self.assertIn(self.row(731, 5)["funding_time"], stored)
            self.assertNotIn(self.row(745, 99)["funding_time"], stored)
            self.assertEqual(module.ExchangeFundingRateRecord.query.filter_by(exchange=exchange, symbol="ROLLEXPIRED/USDT").count(), 0)

    def test_empty_targets_do_not_fetch_or_clean_up_history(self):
        with patch.object(module, "get_json", side_effect=AssertionError("No request")), patch.object(module.db.session, "query", side_effect=AssertionError("No query")):
            self.assertEqual(module.sync_funding_history([]), {})
            self.assertEqual(module.sync_dual_funding_history([]), 0)

    def test_buffer_only_history_keeps_previous_but_does_not_fabricate_window_totals(self):
        self.insert("Binance", [self.row(731, .03)])
        with patch.object(module, "get_json", return_value=[]), patch.object(module.db.session, "commit"):
            actual = module.sync_funding_history(["ROLLTESTUSDT"])["ROLLTESTUSDT"]
        self.assertAlmostEqual(actual["previous"], .03)
        self.assertTrue(all(actual[f"day_{days}"] is None for days in (1, 3, 7, 30)))

    def test_dual_uses_own_settlement_events_for_each_leg_and_reverses_sign(self):
        long = [self.row(age, .001) for age in (2, 10, 18)]
        short = [self.row(age, .005) for age in (2, 6, 10, 14, 18, 22)]
        self.insert("Bybit", long)
        self.insert("Binance", short)
        groups = self.groups()
        module.enrich_dual_funding_statistics(groups)
        forward, reverse, missing = groups[0]["rows"]
        self.assertAlmostEqual(forward["funding_difference_24h"], .027)
        self.assertAlmostEqual(reverse["funding_difference_24h"], -.027)
        self.assertAlmostEqual(forward["funding_difference_previous"], .004)
        self.assertIsNone(missing["funding_difference_24h"])
        reference = module.calculate_dual_funding_differences(long, short, self.now)
        self.assertAlmostEqual(reference["day_1"], .027)

    def test_reference_and_sql_both_reject_future_and_24h_left_boundary(self):
        long = [self.row(24, 5), self.row(1, .01), self.row(-1, 8)]
        short = [self.row(24, 9), self.row(1, .02), self.row(-1, 4)]
        self.insert("Bybit", long)
        self.insert("Binance", short)
        expected = module.calculate_dual_funding_differences(long, short, self.now)
        actual = module.dual_funding_statistics(self.groups())[("ROLLTEST/USDT", "Bybit", "Binance")]
        self.assertEqual(actual, expected)
        self.assertAlmostEqual(actual["day_1"], .01)
        self.assertAlmostEqual(actual["previous"], .01)

    def test_unknown_stays_unknown_while_zero_is_preserved(self):
        self.insert("Binance", [self.row(1, 0)])
        result = module.funding_statistics(["ROLLTESTUSDT", "ROLLMISSINGUSDT"])
        self.assertEqual(result["ROLLTESTUSDT"]["day_1"], 0)
        self.assertIsNone(result["ROLLMISSINGUSDT"]["day_1"])
        with patch.object(module, "funding_statistics", return_value={}):
            groups = [{"symbol": "ROLLMISSING/USDT", "rows": [{}]}]
            module.enrich_funding_statistics(groups)
            self.assertIsNone(groups[0]["rows"][0]["funding_24h"])

    def test_backfill_return_uses_same_window_as_page(self):
        rows = [self.row(age, .005) for age in (2, 6, 10, 14, 18, 22, 26)]
        self.insert("Binance", rows)
        with patch.object(module, "get_json", return_value=[]), patch.object(module.db.session, "commit"):
            actual = module.sync_funding_history(["ROLLTESTUSDT"])["ROLLTESTUSDT"]
        expected = module.funding_statistics(["ROLLTESTUSDT"])["ROLLTESTUSDT"]
        self.assertEqual(actual, expected)
        self.assertAlmostEqual(actual["day_1"], .030)


if __name__ == "__main__":
    unittest.main()

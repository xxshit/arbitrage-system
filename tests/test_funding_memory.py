import os
import random
import tracemalloc
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "funding-memory-test")
import app as module
from sqlalchemy import event


class FundingMemoryTests(unittest.TestCase):
    def setUp(self):
        self.context = module.app.app_context()
        self.context.push()
        # Other integration tests deliberately drop the shared in-memory schema.
        for model in (module.FundingRateRecord, module.ExchangeFundingRateRecord):
            model.__table__.create(module.db.engine, checkfirst=True)
        self.now = datetime(2026, 10, 20, 12, tzinfo=module.SHANGHAI_TZ)
        self.clock = patch.object(module, "datetime", wraps=datetime)
        self.clock.start().now.return_value = self.now
        self.cache = patch.object(module, "DUAL_FUNDING_STATISTICS_CACHE", {"ts": 0, "key": frozenset(), "data": {}})
        self.cache.start()
        module.db.session.begin_nested()

    def tearDown(self):
        module.db.session.rollback()
        module.db.session.remove()
        self.cache.stop()
        self.clock.stop()
        self.context.pop()

    def insert(self, exchange, symbol, rows):
        model = module.FundingRateRecord if exchange == "Binance" else module.ExchangeFundingRateRecord
        values = [{"symbol": symbol.replace("/", "") if exchange == "Binance" else symbol,
                   **({"exchange": exchange} if exchange != "Binance" else {}), **row} for row in rows]
        if values:
            module.db.session.execute(model.__table__.insert(), values)

    def row(self, age, rate, milliseconds=0):
        return {"funding_time": int((self.now - timedelta(hours=age)).timestamp()*1000) + milliseconds,
                "funding_rate": rate}

    def groups(self, symbol="MEMTEST/USDT"):
        return [{"symbol": symbol, "rows": [
            {"long_exchange": a, "short_exchange": b}
            for a, b in (("Bybit", "Binance"), ("OKX", "Binance"), ("Bybit", "OKX"), ("Binance", "Bybit"))]}]

    def test_all_paths_match_reference_with_mixed_settlement_times_and_day_boundaries(self):
        randomizer = random.Random(482)
        records = {}
        for exchange, interval, lag in (("Binance", 1, 0), ("Bybit", 4, 2), ("OKX", 8, 4)):
            records[exchange] = [self.row(hour, randomizer.uniform(-.15, .15)) for hour in range(lag, 29*24+13, interval)]
            self.insert(exchange, "MEMTEST/USDT", records[exchange])
        output = module.dual_funding_statistics(self.groups())
        for (_, long, short), actual in output.items():
            expected = module.calculate_dual_funding_differences(records[long], records[short], self.now)
            for field in expected:
                self.assertAlmostEqual(actual[field], expected[field], places=11)

    def test_previous_groups_same_minute_but_never_an_older_leg(self):
        bybit = [self.row(1, .03, 100), self.row(1, .02, 59000)]
        binance = [self.row(1, .09, 30000)]
        okx = [self.row(2, .8)]
        for exchange, rows in (("Bybit", bybit), ("Binance", binance), ("OKX", okx)):
            self.insert(exchange, "MEMTEST/USDT", rows)
        output = module.dual_funding_statistics(self.groups())
        self.assertAlmostEqual(output[("MEMTEST/USDT", "Bybit", "Binance")]["previous"], .04)
        self.assertAlmostEqual(output[("MEMTEST/USDT", "OKX", "Binance")]["previous"], .09)
        self.assertAlmostEqual(output[("MEMTEST/USDT", "Bybit", "OKX")]["previous"], -.05)

    def test_missing_leg_is_unknown_and_empty_target_avoids_database(self):
        self.insert("Binance", "MEMTEST/USDT", [self.row(1, .02)])
        self.assertTrue(all(value is None for data in module.dual_funding_statistics(self.groups()).values() for value in data.values()))
        with patch.object(module.db.session, "query", side_effect=AssertionError("No query")):
            self.assertEqual(module.dual_funding_statistics([]), {})

    def test_window_excludes_old_and_unsettled_future_records_preserves_real_zero(self):
        self.insert("Binance", "MEMTEST/USDT", [self.row(1, 0), self.row(-1, 8), self.row(31*24, 9)])
        self.insert("Bybit", "MEMTEST/USDT", [self.row(1, 0)])
        output = module.dual_funding_statistics(self.groups())[("MEMTEST/USDT", "Bybit", "Binance")]
        self.assertEqual(output, dict(previous=0, day_1=0, day_3=0, day_7=0, day_30=0))

    def test_cache_reuses_aggregates_and_changes_with_requested_paths(self):
        for exchange in ("Binance", "Bybit", "OKX"):
            self.insert(exchange, "MEMTEST/USDT", [self.row(1, .02)])
        with patch.object(module, "settled_funding_summaries", wraps=module.settled_funding_summaries) as read:
            first = module.dual_funding_statistics(self.groups())
            self.assertIs(module.dual_funding_statistics(self.groups()), first)
            self.assertEqual(read.call_count, 2)
            module.dual_funding_statistics(self.groups("MEMOTHER/USDT"))
            self.assertEqual(read.call_count, 4)

    def test_large_history_never_materializes_orm_rows_or_history_sized_python_buffers(self):
        groups = []
        for index in range(80):
            symbol = f"MEMLOAD{index}/USDT"
            groups.extend(self.groups(symbol))
            for exchange in ("Binance", "Bybit", "OKX"):
                self.insert(exchange, symbol, [self.row(hour, (hour % 11 - 5)*.001) for hour in range(600)])
        module.db.session.flush()
        statements = []
        def loaded(*_):
            self.fail("Historical ORM entity materialized")
        def queried(_conn, _cursor, statement, *_):
            statements.append(statement)
        models = (module.FundingRateRecord, module.ExchangeFundingRateRecord)
        for model in models:
            event.listen(model, "load", loaded)
        event.listen(module.db.engine, "before_cursor_execute", queried)
        tracemalloc.start()
        try:
            for _ in range(4):
                module.DUAL_FUNDING_STATISTICS_CACHE["ts"] = 0
                output = module.dual_funding_statistics(groups)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            event.remove(module.db.engine, "before_cursor_execute", queried)
            for model in models:
                event.remove(model, "load", loaded)
        self.assertEqual(len(output), 320)
        self.assertEqual(len(statements), 4*4)  # four cold refreshes, not per path
        self.assertTrue(all("GROUP BY" in statement for statement in statements))
        self.assertLess(peak, 6*1024*1024)  # 144,000 histories, only 240 summaries


if __name__ == "__main__":
    unittest.main()

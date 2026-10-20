import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "trend-reference-test")
os.environ.setdefault("CHAT_ENCRYPTION_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")

import app as module
from trend_reference import BUCKET, WINDOW, TrendReferenceCache, fetch_reference, reference_bounds

NOW = 2_000_000_100 // BUCKET * BUCKET
TARGET = NOW - WINDOW


def data(points=None, multiplier=1):
    start, end = reference_bounds(NOW)
    return {"source_symbol": "BTCUSDT", "multiplier": multiplier, "start": start,
            "end": end, "checked_at": NOW, "points": points if points is not None else [[TARGET, 100]]}


def candle(at=TARGET, opening="100", close="900"):
    return [at * 1000, opening, "1000", "1", close, "1", at * 1000 + 299999]


class ReferenceTests(unittest.TestCase):
    def test_public_request_uses_bounded_5m_range_and_open_not_future_close(self):
        get = MagicMock(return_value=[candle()])
        result = fetch_reference(get, "BTCUSDT", 1, NOW)
        query = parse_qs(urlsplit(get.call_args.args[0]).query)
        self.assertEqual(query["interval"], ["5m"])
        self.assertEqual(query["limit"], ["99"])
        self.assertEqual(int(query["startTime"][0]), (TARGET-600)*1000)
        self.assertEqual(int(query["endTime"][0]), (TARGET+21600)*1000-1)
        self.assertEqual(get.call_args.kwargs, {"timeout": 4, "weight_limit": 1800})
        self.assertEqual(result["points"], [(TARGET, 100)])

    def test_no_accounts_keys_or_private_endpoints(self):
        get = MagicMock(return_value=[])
        fetch_reference(get, "BTCUSDT", 1, NOW)
        self.assertTrue(get.call_args.args[0].startswith("https://fapi.binance.com/fapi/v1/klines?"))
        with self.assertRaises(ValueError):
            fetch_reference(get, "BTCUSDT&signature=x", 1, NOW)
        self.assertEqual(get.call_count, 1)

    def test_empty_listing_history_is_unknown_not_zero_or_shorter_window(self):
        cache = TrendReferenceCache()
        cache.put("NEW", fetch_reference(lambda *a, **k: [], "NEWUSDT", 1, NOW))
        self.assertIsNone(cache.change("NEW", 10, NOW))
        self.assertFalse(cache.due("NEW", "NEWUSDT", 1, NOW+300))

    def test_invalid_upstream_is_not_saved_as_success(self):
        for payload in ({}, {"code": -1121}, "invalid", [candle(opening="NaN")],
                        [candle(opening="0")], [candle(at=TARGET+21600)],
                        [candle(), candle()], [candle(at=TARGET+1)], [[]]):
            with self.subTest(payload=payload):
                with self.assertRaises((ValueError, TypeError)):
                    fetch_reference(lambda *a, **k: payload, "BTCUSDT", 1, NOW)

    def test_invalid_multiplier_rejected(self):
        for multiplier in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                fetch_reference(lambda *a, **k: [], "BTCUSDT", multiplier, NOW)

    def test_backward_tolerance_never_takes_later_price(self):
        cache = TrendReferenceCache()
        cache.put("BTC", data([[TARGET-600, 100], [TARGET+300, 400]]))
        self.assertAlmostEqual(cache.change("BTC", 120, NOW), 20)
        self.assertIsNone(cache.price("BTC", NOW+1800))
        self.assertIsNone(cache.price("BTC", NOW-900))
        cache.put("FUTURE_ONLY", data([[TARGET+300, 400]]))
        self.assertIsNone(cache.price("FUTURE_ONLY", NOW))

    def test_rolling_window_moves_with_time_and_refresh_before_expiry(self):
        cache = TrendReferenceCache()
        cache.put("BTC", data([[TARGET, 100], [TARGET+300, 200]]))
        self.assertAlmostEqual(cache.change("BTC", 110, NOW), 10)
        self.assertAlmostEqual(cache.change("BTC", 110, NOW+300), -45)
        self.assertFalse(cache.due("BTC", "BTCUSDT", 1, NOW))
        self.assertTrue(cache.due("BTC", "BTCUSDT", 1, NOW+5*3600))
        self.assertIsNone(cache.change("BTC", 110, NOW+21600))

    def test_multiplier_has_same_percent_for_raw_and_normalized_current(self):
        cache = TrendReferenceCache()
        result = fetch_reference(lambda *a, **k: [candle(opening="100")], "1000XECUSDT", 1000, NOW)
        cache.put("XEC", result)
        self.assertAlmostEqual(cache.change("XEC", 110, NOW, raw_price=True), 10)
        self.assertAlmostEqual(cache.change("XEC", .11, NOW), 10)

    def test_persistence_round_trip_and_corruption_validation(self):
        cache = TrendReferenceCache()
        cache.put("BTC", json.loads(json.dumps(data())))
        self.assertAlmostEqual(cache.change("BTC", 110, NOW), 10)
        for altered in ({"points": [[TARGET, float("nan")]]}, {"points": [[TARGET, -1]]},
                        {"points": [[TARGET, 1]]*100}, {"end": TARGET+100000},
                        {"source_symbol": "bad/path"}, {"multiplier": 0}):
            with self.assertRaises(ValueError):
                cache.put("BAD", {**data(), **altered})

    def test_failure_retains_valid_prices_and_defers_without_starving_other_symbols(self):
        cache = TrendReferenceCache()
        cache.put("BTC", data())
        cache.defer("BTC", NOW+600)
        self.assertFalse(cache.due("BTC", "OTHER", 1, NOW))
        self.assertTrue(cache.due("ETH", "ETHUSDT", 1, NOW))
        self.assertAlmostEqual(cache.change("BTC", 110, NOW), 10)
        self.assertTrue(cache.due("BTC", "OTHER", 1, NOW+601))

    def test_memory_and_retry_metadata_bounded(self):
        cache = TrendReferenceCache()
        with patch("trend_reference.MAX_SYMBOLS", 3):
            for i in range(8):
                cache.put(str(i), data())
                cache.defer(str(i), NOW)
        self.assertEqual(len(cache.entries), 3)
        self.assertEqual(len(cache.retries), 3)

    def test_missing_or_nonfinite_current_remains_unknown(self):
        cache = TrendReferenceCache()
        cache.put("BTC", data())
        for current in (None, 0, -1, float("nan"), float("inf")):
            self.assertIsNone(cache.change("BTC", current, NOW))
        self.assertIsNone(cache.change("MISSING", 110, NOW))


class TrendIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cache = TrendReferenceCache()
        self.cache.put("BTC/USDT", data())

    def test_spot_2d_uses_existing_history_and_30d_uses_public_cache(self):
        row = {"short_bid": 109, "short_ask": 111}
        history = MagicMock()
        history.query.filter.return_value.all.return_value = [
            SimpleNamespace(symbol="BTC/USDT", bucket_at=NOW-2*86400, price=50)]
        with patch.object(module, "FuturesPriceHistory", history), \
             patch.object(module, "LONG_TREND_REFERENCES", self.cache), patch("app.time.time", return_value=NOW), \
             patch.object(module, "get_json", side_effect=AssertionError("No network in page")):
            module.enrich_price_changes([{"symbol": "BTC/USDT", "rows": [row]}])
        self.assertAlmostEqual(row["change_2d"], 120)
        self.assertAlmostEqual(row["change_30d"], 10)
        self.assertIsNone(row["change_7d"])

    def test_dual_has_same_binance_reference_not_bybit_price(self):
        group = {"symbol": "BTC/USDT", "rows": [{"long_exchange": "Bybit", "long_bid": 999, "long_ask": 1000,
                "short_exchange": "Binance", "short_bid": 109, "short_ask": 111, "short_basis": .2}]}
        history = MagicMock()
        history.query.filter.return_value.all.return_value = [
            SimpleNamespace(symbol="BTC/USDT", bucket_at=NOW-2*86400, price=50)]
        with patch.object(module, "FuturesPriceHistory", history), patch.object(module, "SPOT_FUTURES_CACHE", {"snapshot": None}), \
             patch.object(module, "LONG_TREND_REFERENCES", self.cache), patch("app.time.time", return_value=NOW):
            module.enrich_dual_binance_reference([group])
        self.assertAlmostEqual(group["binance_change_2d"], 120)
        self.assertAlmostEqual(group["binance_change_30d"], 10)

    def test_dual_alias_restores_raw_units_before_comparing_shared_history(self):
        self.cache.put("XEC/USDT", {**data([[TARGET, .1]], 1000), "source_symbol": "1000XECUSDT"})
        group = {"symbol": "XEC/USDT", "rows": [{"long_exchange": "Bybit",
                 "short_exchange": "Binance", "short_bid": .109, "short_ask": .111,
                 "alert_stream_route": {"short_factor": .001}}]}
        history = MagicMock()
        history.query.filter.return_value.all.return_value = [
            SimpleNamespace(symbol="XEC/USDT", bucket_at=NOW-2*86400, price=100)]
        with patch.object(module, "FuturesPriceHistory", history), patch.object(module, "SPOT_FUTURES_CACHE", {"snapshot": None}), \
             patch.object(module, "LONG_TREND_REFERENCES", self.cache), patch("app.time.time", return_value=NOW):
            module.enrich_dual_binance_reference([group])
        self.assertAlmostEqual(group["binance_change_2d"], 10)
        self.assertAlmostEqual(group["binance_change_30d"], 10)

    def test_catalog_accepts_only_active_verified_binance_usdt_perpetual(self):
        catalog = {"symbols": [
            {"symbol": s, "quoteAsset": q, "status": state, "contractType": contract}
            for s, q, state, contract in [("BTCUSDT", "USDT", "TRADING", "PERPETUAL"),
              ("1000XECUSDT", "USDT", "TRADING", "PERPETUAL"), ("OTHERUSDT", "USDT", "TRADING", "PERPETUAL"),
              ("OLDUSDT", "USDT", "SETTLING", "PERPETUAL"), ("BTCUSDC", "USDC", "TRADING", "PERPETUAL")]]}
        alias = SimpleNamespace(canonical_symbol="XECUSDT", alias_symbol="1000XECUSDT",
                                multiplier=1000, exchange="Binance", market_type="contract")
        with patch.object(module, "SPOT_FUTURES_CACHE", {"snapshot": {"symbols": [{"symbol": s} for s in ["BTC/USDT", "XEC/USDT", "OLD/USDT"]]}}), \
             patch.object(module, "DUAL_FUTURES_CACHE", {"snapshot": None}), \
             patch.object(module, "MARKET_PAYLOAD_CACHE", {"https://fapi.binance.com/fapi/v1/exchangeInfo": {"payload": catalog}}), \
             patch.object(module, "symbol_alias_rows", return_value=[alias]), patch.object(module, "is_rwa_stock_pair", return_value=False):
            self.assertEqual(module.long_trend_targets(), {"BTC/USDT": ("BTCUSDT", 1), "XEC/USDT": ("1000XECUSDT", 1000)})

    def test_reference_table_is_separate_from_account_and_price_history(self):
        self.assertEqual(set(module.FuturesTrendReference.__table__.columns.keys()), {"symbol", "payload", "updated_at"})
        self.assertEqual(module.PRICE_HISTORY_RETENTION_SECONDS, 8*86400)
        self.assertEqual(module.TREND_WINDOWS["change_2d"], 2*86400)
        self.assertEqual(module.TREND_WINDOWS["change_30d"], 30*86400)

    def test_old_backfill_does_not_loop_trying_to_find_30d_in_eight_days(self):
        history = SimpleNamespace(symbol=module.FuturesPriceHistory.symbol,
                                  bucket_at=module.FuturesPriceHistory.bucket_at,
                                  id=module.FuturesPriceHistory.id, query=MagicMock())
        history.query.filter.return_value.all.return_value = [
            SimpleNamespace(symbol="BTC/USDT", bucket_at=NOW-seconds)
            for seconds in module.TREND_WINDOWS.values() if seconds < module.PRICE_HISTORY_RETENTION_SECONDS]
        session = MagicMock()
        session.query.return_value.filter.return_value.group_by.return_value.all.return_value = [("BTC/USDT", 2100)]
        with patch.object(module, "FuturesPriceHistory", history), patch.object(module.db, "session", session), \
             patch("app.time.time", return_value=NOW):
            result = module.price_history_integrity([{"symbol": "BTC/USDT"}])
        self.assertEqual(result["missing_symbols"], [])

    def test_persist_then_publish_and_do_not_replace_good_cache_on_fetch_error(self):
        session = MagicMock()
        session.get.return_value = None
        get = MagicMock(return_value=data())
        with patch.object(module.db, "session", session), patch.object(module, "fetch_reference", get), \
             patch.object(module, "LONG_TREND_REFERENCES", self.cache):
            module.sync_long_trend_reference("BTC/USDT", "BTCUSDT", 1, NOW)
            row = session.add.call_args.args[0]
            self.assertEqual(json.loads(row.payload), data())
            session.commit.assert_called_once()
            self.assertAlmostEqual(self.cache.change("BTC/USDT", 120, NOW), 20)
            get.side_effect = TimeoutError()
            with self.assertRaises(TimeoutError):
                module.sync_long_trend_reference("BTC/USDT", "BTCUSDT", 1, NOW+1)
            session.commit.assert_called_once()
            self.assertAlmostEqual(self.cache.change("BTC/USDT", 120, NOW), 20)


if __name__ == "__main__":
    unittest.main()

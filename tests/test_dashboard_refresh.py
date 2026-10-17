import os
import time
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "dashboard-refresh-test")
os.environ.setdefault("CHAT_ENCRYPTION_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")

import app as module


class StopWorker(BaseException):
    pass


class DashboardRefreshTests(unittest.TestCase):
    def setUp(self):
        self.cache = patch.object(module, "DASHBOARD_OPPORTUNITY_CACHE", {"ts": 0, "items": []})
        self.cache.start()
        self.addCleanup(self.cache.stop)
        self.analysis = patch.object(module, "MAJOR_MARKET_CACHE", {"ts": 0, "items": []})
        self.analysis.start()
        self.addCleanup(self.analysis.stop)

    def quotes(self, raw_symbol, price=100, exchanges=None, captured_at=1000):
        exchanges = module.EXCHANGES if exchanges is None else exchanges
        return (
            raw_symbol[:-4] + "/USDT",
            {exchange: price + index for index, exchange in enumerate(exchanges)},
            {exchange: captured_at for exchange in exchanges},
            {"change_24h": 2, "volume_24h": 100000},
        )

    def refresh(self, **kwargs):
        with patch.object(module, "fetch_dashboard_symbol_quotes", side_effect=lambda raw: self.quotes(raw, **kwargs)):
            return module.refresh_dashboard_opportunities()

    def test_quotes_advance_after_five_seconds_without_fifteen_second_cache(self):
        self.refresh(price=100)
        self.refresh(price=110, captured_at=1005)
        with patch("app.time.time", return_value=1006), patch("app.get_json", side_effect=AssertionError("HTTP reader contacted exchange")):
            items = module.live_dashboard_opportunities()
        self.assertEqual(len(items), 3)
        self.assertTrue(all(item["exchange_prices"]["Binance"] == 110 for item in items))
        self.assertTrue(all(item["updated_at_epoch"] == 1005 and not item["stale"] for item in items))

    def test_all_failed_sources_keep_old_timestamp_and_become_stale(self):
        self.refresh()
        with patch.object(module, "fetch_dashboard_symbol_quotes", side_effect=RuntimeError("upstream down")):
            module.refresh_dashboard_opportunities()
        with patch("app.time.time", return_value=1020):
            items = module.live_dashboard_opportunities()
        self.assertTrue(all(item["updated_at_epoch"] == 1000 and item["stale"] for item in items))

    def test_partial_exchange_failure_does_not_mix_old_and_new_prices(self):
        self.refresh()
        self.refresh(exchanges=["Binance", "OKX"], captured_at=1005)
        items = module.live_dashboard_opportunities()
        self.assertTrue(all(item["exchange_prices"]["Bybit"] is None and item["incomplete"] for item in items))

    def test_one_exchange_does_not_invent_an_arbitrage_spread(self):
        rows = self.refresh(exchanges=["Binance"])
        self.assertTrue(all(item["spread"] is None and item["estimated_profit"] is None for item in rows))

    def test_major_price_updates_independently_without_mutating_analysis(self):
        module.MAJOR_MARKET_CACHE["items"] = [{"symbol": "BTC/USDT", "price": 90, "windows": {"30m": {"price_change": 1}}}]
        self.refresh(price=110, captured_at=1005)
        with patch("app.time.time", return_value=1006):
            majors = module.live_dashboard_majors(module.live_dashboard_opportunities())
        self.assertEqual(majors[0]["price"], 110)
        self.assertEqual(majors[0]["quote_updated_at_epoch"], 1005)
        self.assertFalse(majors[0]["quote_stale"])
        self.assertEqual(module.MAJOR_MARKET_CACHE["items"][0]["price"], 90)
        self.assertEqual(majors[0]["windows"]["30m"]["price_change"], 1)

    def test_http_response_never_calls_slow_analysis_or_exchange_loaders(self):
        self.refresh(captured_at=time.time())
        with module.app.app_context(), \
                patch.object(module, "get_json", side_effect=AssertionError("network in HTTP")), \
                patch.object(module, "fetch_major_market_overview", side_effect=AssertionError("analysis in HTTP")), \
                patch.object(module, "load_momentum_scores", side_effect=AssertionError("scoring in HTTP")), \
                patch.object(module, "load_open_interest_market_cap_opportunities", side_effect=AssertionError("market caps in HTTP")):
            response = module.dashboard()
        data = response.get_json()
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertFalse(data["market_stale"])
        self.assertEqual(len(data["opportunities"]), 3)

    def test_cold_start_returns_waiting_state_without_network(self):
        with module.app.app_context(), patch.object(module, "get_json", side_effect=AssertionError("cold request must not block")):
            data = module.dashboard().get_json()
        self.assertIsNone(data["market_updated_at_epoch"])
        self.assertTrue(data["market_stale"])
        self.assertEqual(data["opportunities"], [])
        self.assertTrue(all(item.get("price") is None for item in data["majors"]))

    def test_background_worker_subtracts_work_duration_and_recovers(self):
        refresh = unittest.mock.Mock(__name__="test-refresh", side_effect=[RuntimeError("temporary failure"), None])
        with patch("app.time.monotonic", side_effect=[100, 102, 105, 106]), \
                patch("app.time.sleep", side_effect=[None, StopWorker()]) as sleep, \
                patch.object(module.app.logger, "warning"):
            with self.assertRaises(StopWorker):
                module.background_dashboard_cache_refresh(refresh, 5)
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3, 4])

    def test_live_quote_requests_are_bounded_and_keep_exchange_identity(self):
        def payload(url, timeout):
            self.assertEqual(timeout, 1.5)
            if "binance" in url:
                return {"lastPrice": "100", "priceChangePercent": "2", "quoteVolume": "1000"}
            if "okx" in url:
                return {"data": [{"last": "101"}]}
            return {"result": {"list": [{"lastPrice": "102"}]}}
        with patch.object(module, "get_json", side_effect=payload):
            symbol, prices, times, metrics = module.fetch_dashboard_symbol_quotes("BTCUSDT")
        self.assertEqual(symbol, "BTC/USDT")
        self.assertEqual(prices, {"Binance": 100, "OKX": 101, "Bybit": 102})
        self.assertEqual(set(times), set(prices))
        self.assertEqual(metrics["change_24h"], 2)

    def test_configured_slower_cadence_does_not_create_false_stale_warning(self):
        self.refresh(captured_at=1000)
        with patch.object(module, "DASHBOARD_QUOTE_REFRESH_SECONDS", 30), patch("app.time.time", return_value=1035):
            self.assertTrue(all(not item["stale"] for item in module.live_dashboard_opportunities()))

    def test_workers_register_independent_cache_lanes_only_once(self):
        with patch.object(module, "BACKGROUND_WORKERS_STARTED", False), patch("app.threading.Thread") as thread:
            module.start_background_workers()
            module.start_background_workers()
        lanes = [call.kwargs for call in thread.call_args_list if call.kwargs.get("target") is module.background_dashboard_cache_refresh]
        self.assertEqual(len(lanes), 4)
        self.assertEqual({lane["name"] for lane in lanes}, {"dashboard-quotes", "dashboard-major-analysis", "dashboard-momentum", "dashboard-oi-market-cap"})
        self.assertEqual(lanes[0]["args"][1:], (5, "spot_market_refresh", False))
        self.assertTrue(all(lane["args"][-1] for lane in lanes[1:]))


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from unittest.mock import patch, MagicMock

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "live-page-refresh-test")
os.environ.setdefault("CHAT_ENCRYPTION_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")

import app as module


class StopWorker(BaseException):
    pass


class LivePageRefreshTests(unittest.TestCase):
    def test_live_endpoints_are_not_browser_cached_even_when_login_expires(self):
        client = module.app.test_client()
        for path in ("dashboard", "spot-futures", "dual-futures", "alerts", "hedge-positions"):
            with self.subTest(path=path):
                response = client.get("/api/" + path)
                self.assertEqual(response.status_code, 401)
                self.assertIn("no-store", response.headers["Cache-Control"])

    def check_reference(self, groups, spot=None, fallback=None, expected=10):
        now = 2_000_000_000
        bucket = now // module.PRICE_HISTORY_BUCKET_SECONDS * module.PRICE_HISTORY_BUCKET_SECONDS
        points = {("BTC/USDT", bucket - seconds): 100 for seconds in module.TREND_WINDOWS.values()}
        latest, history = MagicMock(), MagicMock()
        latest.query.filter.return_value.all.return_value = fallback or []
        history.query.filter.return_value.all.return_value = []
        with patch.object(module, "LatestMarketSnapshot", latest), patch.object(module, "FuturesPriceHistory", history), \
                patch.object(module, "SPOT_FUTURES_CACHE", {"snapshot": spot}), \
                patch.object(module, "nearest_trend_points", return_value=points), patch("app.time.time", return_value=now):
            module.enrich_dual_binance_reference(groups)
        self.assertAlmostEqual(groups[0]["binance_change_5m"], expected)
        return latest

    def test_dual_binance_short_uses_live_leg_instead_of_15_second_database(self):
        groups = [{"symbol": "BTC/USDT", "rows": [{"short_exchange": "Binance", "short_bid": 109, "short_ask": 111, "short_basis": 0.1, "long_exchange": "Bybit"}]}]
        latest = self.check_reference(groups)
        latest.query.filter.assert_not_called()

    def test_dual_binance_long_also_uses_live_leg(self):
        groups = [{"symbol": "BTC/USDT", "rows": [{"long_exchange": "Binance", "long_bid": 109, "long_ask": 111, "long_basis": 0.1, "short_exchange": "Bybit"}]}]
        self.check_reference(groups)

    def test_bybit_okx_path_uses_live_binance_memory_reference(self):
        groups = [{"symbol": "BTC/USDT", "rows": [{"long_exchange": "Bybit", "short_exchange": "OKX"}]}]
        spot = {"symbols": [{"symbol": "BTC/USDT", "rows": [{"short_bid": 119, "short_ask": 121, "basis": 0.2}]}]}
        latest = self.check_reference(groups, spot=spot, expected=20)
        latest.query.filter.assert_not_called()

    def test_cold_start_retains_database_fallback_for_reference(self):
        groups = [{"symbol": "BTC/USDT", "rows": [{"long_exchange": "Bybit", "short_exchange": "OKX"}]}]
        row = type("Snapshot", (), {"symbol": "BTC/USDT", "short_bid": 109, "short_ask": 111, "basis": 0.1})()
        latest = self.check_reference(groups, fallback=[row])
        latest.query.filter.assert_called_once()

    def test_spot_rule_read_failure_does_not_kill_market_worker(self):
        with patch("app.runtime_interval", side_effect=[RuntimeError("database temporarily unavailable"), 5]) as interval, \
                patch("app.spot_futures_snapshot") as refresh, patch("app.time.monotonic", return_value=100), \
                patch("app.time.sleep", side_effect=[None, StopWorker()]):
            with self.assertRaises(StopWorker):
                module.background_spot_market_refresh()
        self.assertEqual(interval.call_count, 2)
        refresh.assert_called_once_with()

    def test_dual_rule_read_failure_does_not_kill_market_worker(self):
        with patch("app.runtime_interval", side_effect=[RuntimeError("database temporarily unavailable"), 5]) as interval, \
                patch("app.dual_futures_snapshot") as refresh, patch("app.time.monotonic", return_value=100), \
                patch("app.time.sleep", side_effect=[None, None, StopWorker()]):
            with self.assertRaises(StopWorker):
                module.background_dual_market_refresh()
        self.assertEqual(interval.call_count, 2)
        refresh.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

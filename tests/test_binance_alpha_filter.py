import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from app import (
    app,
    is_binance_alpha_symbol,
    load_binance_alpha_symbols,
    parse_binance_alpha_symbols,
    spot_futures,
)


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


class BinanceAlphaFilterTests(unittest.TestCase):
    def setUp(self):
        self.catalog_cache = dict(app_module.BINANCE_ALPHA_CATALOG_CACHE)
        app_module.BINANCE_ALPHA_CATALOG_CACHE.update({
            "symbols": frozenset(),
            "expires_at": 0.0,
            "updated_at": None,
        })

    def tearDown(self):
        app_module.BINANCE_ALPHA_CATALOG_CACHE.clear()
        app_module.BINANCE_ALPHA_CATALOG_CACHE.update(self.catalog_cache)

    def test_parser_keeps_visible_alpha_tokens_and_normalizes_case(self):
        symbols = parse_binance_alpha_symbols({"data": [
            {"symbol": "hajimi", "offline": False},
            {"symbol": "KOMA"},
            {"symbol": "OLD", "offline": True},
            {"offline": False},
        ]})

        self.assertEqual(symbols, frozenset({"HAJIMI", "KOMA"}))

    def test_matcher_understands_multiplier_contract_symbols(self):
        self.assertTrue(is_binance_alpha_symbol("HAJIMI/USDT", {"HAJIMI"}))
        self.assertTrue(is_binance_alpha_symbol("1000SATS/USDT", {"SATS"}))
        self.assertFalse(is_binance_alpha_symbol("BTC/USDT", {"HAJIMI"}))

    def test_catalog_uses_last_valid_copy_when_refresh_fails(self):
        payload = {"data": [{"symbol": "HAJIMI", "offline": False}]}
        with patch("app.get_json", return_value=payload):
            symbols, updated_at, stale = load_binance_alpha_symbols(force=True)
        self.assertEqual(symbols, frozenset({"HAJIMI"}))
        self.assertTrue(updated_at)
        self.assertFalse(stale)

        with patch("app.get_json", side_effect=OSError("temporary failure")):
            symbols, cached_at, stale = load_binance_alpha_symbols(force=True)
        self.assertEqual(symbols, frozenset({"HAJIMI"}))
        self.assertEqual(cached_at, updated_at)
        self.assertTrue(stale)

    def test_api_filters_before_sorting_and_pagination(self):
        snapshot = {
            "updated_at": "12:00:00",
            "next_refresh_in_seconds": 5,
            "errors": {},
            "symbols": [
                {"symbol": "BTC/USDT", "rows": [{"long_exchange": "Binance", "open_spread": 2.0}]},
                {"symbol": "HAJIMI/USDT", "rows": [{"long_exchange": "Gate", "open_spread": 1.0}]},
            ],
        }
        with app.test_request_context("/api/spot-futures?binance_alpha_only=1"), patch(
            "app.load_latest_market_snapshot", return_value=snapshot
        ), patch("app.enrich_funding_statistics"), patch("app.enrich_price_changes"), patch(
            "app.enrich_basis_openings"
        ), patch("app.enrich_transfer_networks"), patch("app.mark_announced_delistings"), patch(
            "app.load_binance_alpha_symbols", return_value=(frozenset({"HAJIMI"}), "2026-09-10 12:00:00", False)
        ), patch("app.SPOT_VIEW_CACHE", {"key": None, "symbols": None}):
            response = spot_futures()

        data = response.get_json()
        self.assertTrue(data["binance_alpha_only"])
        self.assertEqual(data["total_symbols"], 1)
        self.assertEqual([item["symbol"] for item in data["symbols"]], ["HAJIMI/USDT"])

    def test_frontend_adds_checkbox_and_sends_filter_parameter(self):
        self.assertIn('id="binanceAlphaOnly"', APP_JS)
        self.assertIn("function setBinanceAlphaFilter()", APP_JS)
        self.assertIn("&binance_alpha_only=", APP_JS)
        self.assertIn("当前条件下没有匹配的 Binance Alpha 币种", APP_JS)


if __name__ == "__main__":
    unittest.main()

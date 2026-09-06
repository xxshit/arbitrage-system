import unittest
from datetime import datetime
from pathlib import Path

from app import onchain_risk_profile, onchain_score_metrics, parse_onchain_trending_pools


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


def sample_payload(liquidity="250000", volume="600000", symbol="HAJIMI"):
    return {
        "data": [{
            "id": "bsc_0xpool",
            "attributes": {
                "address": "0xpool",
                "base_token_price_usd": "0.016",
                "reserve_in_usd": liquidity,
                "volume_usd": {"h1": "80000", "h24": volume},
                "price_change_percentage": {"m5": "4", "h1": "18", "h6": "45", "h24": "70"},
                "transactions": {"h1": {"buys": 180, "sells": 90}},
                "market_cap_usd": "16000000",
                "fdv_usd": "16000000",
                "pool_created_at": "2026-08-20T00:00:00Z",
            },
            "relationships": {
                "base_token": {"data": {"id": "bsc_0x82ec"}},
                "quote_token": {"data": {"id": "bsc_0xusdt"}},
                "dex": {"data": {"id": "pancakeswap_v3"}},
            },
        }],
        "included": [
            {"id": "bsc_0x82ec", "attributes": {"address": "0x82ec", "symbol": symbol, "name": "哈基米"}},
            {"id": "bsc_0xusdt", "attributes": {"address": "0xusdt", "symbol": "USDT", "name": "Tether"}},
        ],
    }


class OnchainOpportunityTests(unittest.TestCase):
    def test_score_rewards_liquidity_activity_buy_pressure_and_momentum(self):
        strong = onchain_score_metrics({
            "liquidity_usd": 5_000_000,
            "volume_24h_usd": 2_500_000,
            "buys_1h": 700,
            "sells_1h": 300,
            "price_change_5m": 12,
            "price_change_1h": 35,
            "price_change_6h": 80,
        })
        weak = onchain_score_metrics({
            "liquidity_usd": 50_000,
            "volume_24h_usd": 25_000,
            "buys_1h": 5,
            "sells_1h": 15,
            "price_change_5m": -3,
            "price_change_1h": -12,
            "price_change_6h": -25,
        })

        self.assertEqual(strong["score"], 100.0)
        self.assertGreater(strong["score"], weak["score"])
        self.assertEqual(strong["components"], {
            "liquidity": 25.0,
            "activity": 25.0,
            "order_flow": 15.0,
            "momentum": 35.0,
        })

    def test_parser_uses_chain_and_contract_identity(self):
        rows = parse_onchain_trending_pools(
            sample_payload(),
            "bsc",
            captured_at=datetime(2026, 9, 6, 0, 0),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token_symbol"], "HAJIMI")
        self.assertEqual(rows[0]["token_name"], "哈基米")
        self.assertEqual(rows[0]["token_address"], "0x82ec")
        self.assertEqual(rows[0]["pool_address"], "0xpool")
        self.assertEqual(rows[0]["chain_label"], "BSC")
        self.assertIn("geckoterminal.com/bsc/pools/0xpool", rows[0]["source_url"])

    def test_parser_filters_thin_or_inactive_pools(self):
        self.assertEqual(parse_onchain_trending_pools(sample_payload(liquidity="49999"), "bsc"), [])
        self.assertEqual(parse_onchain_trending_pools(sample_payload(volume="24999"), "bsc"), [])

    def test_quote_assets_are_not_presented_as_opportunities(self):
        self.assertEqual(parse_onchain_trending_pools(sample_payload(symbol="USDT"), "bsc"), [])

    def test_risk_profile_never_labels_a_pool_safe(self):
        level, tags = onchain_risk_profile({
            "liquidity_usd": 2_000_000,
            "price_change_5m": 1,
            "price_change_1h": 5,
            "buys_1h": 60,
            "sells_1h": 40,
            "market_cap_usd": 10_000_000,
            "pair_created_at": datetime(2026, 8, 1),
        }, now=datetime(2026, 9, 6))

        self.assertEqual(level, "注意")
        self.assertIn("仍需合约与持仓审计", tags)
        self.assertNotIn("安全", "".join(tags))

    def test_frontend_has_independent_onchain_index_and_boundary_copy(self):
        self.assertIn('data-view="onchain-opportunities"', INDEX_HTML)
        self.assertIn('id="onchainOpportunityList"', INDEX_HTML)
        self.assertIn("不含持仓量、资金费率或合约多空人数比确认", INDEX_HTML)
        self.assertIn("function loadOnchainOpportunities", APP_JS)
        self.assertIn("onchain_opportunity_refresh", APP_JS)
        self.assertIn(".onchain-opportunity-card", STYLE_CSS)
        self.assertIn("@media(max-width:760px)", STYLE_CSS)


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "oi-market-cap-test-secret"
os.environ["CHAT_ENCRYPTION_KEY"] = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

from app import (
    fetch_coinmarketcap_market_rows,
    match_market_cap_record,
    open_interest_market_candidates,
    rank_open_interest_market_cap,
)


class OpenInterestMarketCapTests(unittest.TestCase):
    def test_snapshot_aggregation_counts_each_exchange_once(self):
        spot = {
            "symbols": [{
                "symbol": "HEI/USDT",
                "rows": [
                    {"short_bid": 0.38, "short_ask": 0.381, "futures_open_interest": 10_000_000},
                    {"short_bid": 0.38, "short_ask": 0.381, "futures_open_interest": 10_000_000},
                ],
            }],
        }
        dual = {
            "symbols": [{
                "symbol": "HEI/USDT",
                "rows": [
                    {
                        "long_exchange": "Bybit", "long_bid": 0.379, "long_ask": 0.38,
                        "long_open_interest": 7_000_000,
                        "short_exchange": "Binance", "short_bid": 0.38, "short_ask": 0.381,
                        "short_open_interest": 10_000_000,
                    },
                    {
                        "long_exchange": "Bybit", "long_bid": 0.379, "long_ask": 0.38,
                        "long_open_interest": 7_000_000,
                        "short_exchange": "OKX", "short_bid": 0.38, "short_ask": 0.381,
                        "short_open_interest": 5_000_000,
                    },
                ],
            }],
        }
        with patch("app.load_latest_market_snapshot", return_value=spot), patch(
            "app.load_latest_dual_futures_snapshot", return_value=dual
        ):
            items = open_interest_market_candidates()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["exchange_oi"], {
            "Binance": 10_000_000,
            "Bybit": 7_000_000,
            "OKX": 5_000_000,
        })
        self.assertEqual(items[0]["total_open_interest"], 22_000_000)

    def test_symbol_collision_requires_one_price_matched_project(self):
        candidate = {"symbol": "PLAY/USDT", "price": 0.035}
        rows = [
            {"symbol": "play", "id": "playsout", "name": "PlaysOut", "current_price": 0.0349, "market_cap": 13_000_000},
            {"symbol": "play", "id": "play-2", "name": "Other Play", "current_price": 0.0001, "market_cap": 80_000},
        ]
        self.assertEqual(match_market_cap_record(candidate, rows)["id"], "playsout")

        rows[1]["current_price"] = 0.034
        self.assertIsNone(match_market_cap_record(candidate, rows))

    def test_ranking_recalculates_market_cap_at_contract_price(self):
        candidates = [
            {"symbol": "HEI/USDT", "price": 0.38, "total_open_interest": 30_000_000, "exchange_oi": {"Binance": 18_000_000, "Bybit": 12_000_000}},
            {"symbol": "AKE/USDT", "price": 0.0041, "total_open_interest": 43_000_000, "exchange_oi": {"Binance": 25_000_000, "Bybit": 18_000_000}},
        ]
        markets = [
            {"symbol": "hei", "id": "heima", "name": "Heima", "current_price": 0.379, "market_cap": 30_500_000, "market_cap_rank": 700, "circulating_supply": 80_000_000},
            {"symbol": "ake", "id": "akedo", "name": "Akedo", "current_price": 0.00409, "market_cap": 93_000_000, "market_cap_rank": 500, "circulating_supply": 22_000_000_000},
        ]

        ranked = rank_open_interest_market_cap(candidates, markets)

        self.assertEqual([item["symbol"] for item in ranked], ["HEI/USDT", "AKE/USDT"])
        self.assertEqual(ranked[0]["label"], "接近 1:1")
        self.assertAlmostEqual(ranked[0]["market_cap"], 0.38 * 80_000_000, places=2)
        self.assertAlmostEqual(ranked[0]["ratio"], 30_000_000 / (0.38 * 80_000_000), places=4)
        self.assertEqual(ranked[1]["label"], "接近观察")

    @patch("app.get_json")
    def test_coinmarketcap_rows_are_normalized_from_keyless_quotes(self, mocked_get_json):
        mocked_get_json.return_value = {"data": [{
            "id": 37414, "name": "Yooldo", "symbol": "ESPORTS", "cmc_rank": 682,
            "circulating_supply": 899_989_850, "total_supply": 899_999_020, "max_supply": 900_000_000,
            "quote": [{
                "symbol": "USD", "price": 0.019, "market_cap": 17_100_000,
                "fully_diluted_market_cap": 17_101_000, "last_updated": "2026-08-07T05:21:02Z",
            }],
        }]}

        rows = fetch_coinmarketcap_market_rows(["ESPORTS"])

        self.assertEqual(rows[0]["source"], "CoinMarketCap")
        self.assertEqual(rows[0]["id"], "cmc:37414")
        self.assertEqual(rows[0]["circulating_supply"], 899_989_850)

    def test_esports_uses_cmc_supply_and_warns_about_coingecko_disagreement(self):
        candidates = [{
            "symbol": "ESPORTS/USDT", "price": 0.0191, "total_open_interest": 13_560_000,
            "exchange_oi": {"Binance": 9_894_000, "Bybit": 3_666_000},
        }]
        cmc_rows = [{
            "symbol": "esports", "id": "cmc:37414", "source": "CoinMarketCap", "name": "Yooldo",
            "current_price": 0.019, "market_cap": 17_100_000, "market_cap_rank": 682,
            "circulating_supply": 899_989_850,
        }]
        coingecko_rows = [{
            "symbol": "esports", "id": "yooldo-games", "source": "CoinGecko", "name": "Yooldo Games",
            "current_price": 0.01901, "market_cap": 2_889_834, "market_cap_rank": 1915,
            "circulating_supply": 151_800_000,
        }]

        ranked = rank_open_interest_market_cap(candidates, cmc_rows, coingecko_rows)

        self.assertEqual(ranked[0]["market_source"], "CoinMarketCap")
        self.assertAlmostEqual(ranked[0]["market_cap"], 0.0191 * 899_989_850, places=2)
        self.assertAlmostEqual(ranked[0]["ratio"], 13_560_000 / (0.0191 * 899_989_850), places=4)
        self.assertTrue(ranked[0]["market_cap_warning"])
        self.assertEqual(ranked[0]["alternate_market_source"], "CoinGecko")

    def test_missing_cmc_coin_falls_back_to_coingecko_per_candidate(self):
        candidates = [{
            "symbol": "HEI/USDT", "price": 0.38, "total_open_interest": 30_000_000,
            "exchange_oi": {"Binance": 18_000_000, "Bybit": 12_000_000},
        }]
        coingecko_rows = [{
            "symbol": "hei", "id": "heima", "source": "CoinGecko", "name": "Heima",
            "current_price": 0.379, "market_cap": 30_500_000, "market_cap_rank": 700,
            "circulating_supply": 80_000_000,
        }]

        ranked = rank_open_interest_market_cap(candidates, [], coingecko_rows)

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["market_source"], "CoinGecko")
        self.assertAlmostEqual(ranked[0]["market_cap"], 0.38 * 80_000_000, places=2)
        self.assertFalse(ranked[0]["market_cap_warning"])

    def test_tokenized_equity_market_records_are_excluded(self):
        candidate = {"symbol": "DRAM/USDT", "price": 10}
        rows = [{
            "symbol": "dram", "id": "roundhill-memory-etf-backpack-securities",
            "name": "Roundhill Memory ETF - Backpack Securities",
            "current_price": 10, "market_cap": 1_000_000,
        }]
        self.assertIsNone(match_market_cap_record(candidate, rows))


if __name__ == "__main__":
    unittest.main()

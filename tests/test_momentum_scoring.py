import unittest
from unittest.mock import patch

from app import fetch_momentum_structure, momentum_cvd_ratio, score_momentum_structure


class MomentumScoringTests(unittest.TestCase):
    def test_full_four_factor_resonance_scores_one_hundred(self):
        result = score_momentum_structure({
            "price_change_30m": 5,
            "price_change_4h": 20,
            "price_change_24h": 60,
            "oi_change_30m": 3,
            "oi_change_4h": 12,
            "ratio_change_30m": -5,
            "ratio_change_4h": -15,
            "cvd_ratio_30m": 20,
            "cvd_ratio_4h": 20,
        })

        self.assertEqual(result["score"], 100.0)
        self.assertEqual(result["components"], {
            "price": 30.0,
            "open_interest": 25.0,
            "ratio": 20.0,
            "cvd": 25.0,
        })

    def test_price_only_rally_does_not_receive_structure_points(self):
        result = score_momentum_structure({
            "price_change_30m": 5,
            "price_change_4h": 20,
            "price_change_24h": 60,
            "oi_change_30m": -3,
            "oi_change_4h": -12,
            "ratio_change_30m": 5,
            "ratio_change_4h": 15,
            "cvd_ratio_30m": -20,
            "cvd_ratio_4h": -20,
        })

        self.assertEqual(result["score"], 30.0)
        self.assertEqual(result["components"]["open_interest"], 0.0)
        self.assertEqual(result["components"]["ratio"], 0.0)
        self.assertEqual(result["components"]["cvd"], 0.0)

    def test_incomplete_four_factor_data_is_not_scored(self):
        self.assertIsNone(score_momentum_structure({
            "price_change_30m": 2,
            "price_change_4h": 8,
            "price_change_24h": None,
        }))

    def test_cvd_ratio_uses_real_taker_buy_quote_volume(self):
        rows = [
            [0, 0, 0, 0, 0, 0, 0, 1000, 0, 0, 600],
            [0, 0, 0, 0, 0, 0, 0, 2000, 0, 0, 1300],
        ]

        self.assertAlmostEqual(momentum_cvd_ratio(rows), 26.6666667)

    @patch("app.time.time", return_value=2_000_000_000)
    @patch("app.get_json")
    def test_live_structure_uses_closed_30m_and_4h_windows(self, mocked_get_json, _mocked_time):
        start = 1_900_000_000_000
        klines = []
        for index in range(8):
            open_price = 100 + index
            close_price = open_price + 2
            klines.append([
                start + index * 1_800_000, open_price, close_price, open_price,
                close_price, 0, start + (index + 1) * 1_800_000 - 1,
                1000, 0, 0, 650,
            ])
        oi_rows = [{"sumOpenInterest": 100 + index * 2} for index in range(9)]
        ratio_rows = [{"longShortRatio": 1.0 - index * 0.03} for index in range(9)]
        mocked_get_json.side_effect = [klines, oi_rows, ratio_rows]

        result = fetch_momentum_structure({
            "symbol": "TEST/USDT",
            "price": 109,
            "volume_24h": 1_000_000,
            "change_24h": 25,
            "candidate_score": 50,
        })

        self.assertIsNotNone(result)
        self.assertGreater(result["price_change_30m"], 0)
        self.assertGreater(result["price_change_4h"], 0)
        self.assertGreater(result["oi_change_4h"], 0)
        self.assertLess(result["ratio_change_4h"], 0)
        self.assertGreater(result["cvd_ratio_4h"], 0)
        self.assertGreater(result["score"], 0)


if __name__ == "__main__":
    unittest.main()

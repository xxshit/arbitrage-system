import unittest
from unittest.mock import patch

from app import (
    SUDDEN_PUMP_CONFIRMATIONS,
    classify_sudden_pump_stage,
    fetch_sudden_pump_context,
    sudden_pump_confirmation_ready,
)


def kline(open_time, open_price, close_price, quote_volume=1000, taker_buy_quote=550):
    high = max(open_price, close_price)
    low = min(open_price, close_price)
    return [
        open_time, open_price, high, low, close_price, 0, open_time + 299_999,
        quote_volume, 0, 0, taker_buy_quote,
    ]


class SuddenPumpScanTests(unittest.TestCase):
    def setUp(self):
        SUDDEN_PUMP_CONFIRMATIONS.clear()

    def test_single_five_minute_bar_over_fifteen_percent_is_ignition(self):
        stage = classify_sudden_pump_stage(15.2, 18.0, 1)
        self.assertEqual(stage["stage_key"], "ignition")

    def test_two_hot_bars_accelerate_and_fifty_percent_is_extreme(self):
        by_bars = classify_sudden_pump_stage(21.0, 46.0, 2)
        by_total = classify_sudden_pump_stage(18.0, 51.0, 1)
        self.assertEqual(by_bars["stage_key"], "acceleration")
        self.assertEqual(by_total["stage_key"], "extreme_15m")

    def test_live_candle_requires_two_separate_confirmations(self):
        self.assertFalse(sudden_pump_confirmation_ready("CYS/USDT", 1000, False, 10))
        self.assertFalse(sudden_pump_confirmation_ready("CYS/USDT", 1000, False, 14))
        self.assertTrue(sudden_pump_confirmation_ready("CYS/USDT", 1000, False, 16))

    def test_closed_candle_is_immediately_confirmed(self):
        self.assertTrue(sudden_pump_confirmation_ready("CYS/USDT", 1000, True, 10))

    @patch("app.get_json")
    def test_cys_like_two_bar_acceleration_is_verified(self, mocked_get_json):
        start = 1_700_000_000_000
        prices = [100, 100, 100, 100, 100, 110, 134.2, 165.1, 166]
        rows = [
            kline(start + index * 300_000, prices[index], prices[index + 1], 1000 + index * 100, 600 + index * 70)
            for index in range(len(prices) - 1)
        ]
        rows.append(kline(start + 8 * 300_000, 166, 220, 5000, 3500))
        mocked_get_json.side_effect = [
            rows,
            [{"sumOpenInterest": value} for value in (100, 103, 107, 112)],
            [{"longShortRatio": value} for value in (1.0, 0.96, 0.91, 0.86)],
        ]
        item = fetch_sudden_pump_context("CYS/USDT", 15.0)
        self.assertIsNotNone(item)
        self.assertGreater(item["change_5m"], 15)
        self.assertGreaterEqual(item["hot_bar_count"], 2)
        self.assertGreater(item["change_15m"], 50)
        self.assertEqual(item["stage_key"], "extreme_15m")
        self.assertGreater(item["oi_change"], 0)
        self.assertLess(item["ratio_change"], 0)


if __name__ == "__main__":
    unittest.main()

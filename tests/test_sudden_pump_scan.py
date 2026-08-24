import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import (
    SUDDEN_PUMP_CONFIRMATIONS,
    classify_sudden_pump_stage,
    evaluate_push_signal_validation,
    fetch_sudden_pump_context,
    push_signal_validation_bounds,
    push_signal_validation_summary,
    sudden_pump_confirmation_ready,
)


def kline(open_time, open_price, close_price, quote_volume=1000, taker_buy_quote=550):
    high = max(open_price, close_price)
    low = min(open_price, close_price)
    return [
        open_time, open_price, high, low, close_price, 0, open_time + 299_999,
        quote_volume, 0, 0, taker_buy_quote,
    ]


def path_kline(open_epoch, high, low, close=100):
    return [
        open_epoch * 1000, 100, high, low, close, 0, open_epoch * 1000 + 299_999,
        1000, 0, 0, 550,
    ]


def validation_row(**overrides):
    values = {
        "trigger_epoch": 1001,
        "validation_minutes": 10,
        "trigger_price": 100.0,
        "direction": "up",
        "target_move_pct": 5.0,
        "failure_move_pct": 5.0,
        "status": "pending",
        "first_hit": None,
        "first_hit_at": None,
        "max_favorable_pct": 0.0,
        "max_adverse_pct": 0.0,
        "peak_price": None,
        "trough_price": None,
        "observed_candle_count": 0,
        "resolved_at": None,
        "result_note": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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

    def test_push_validation_starts_with_first_full_post_push_candle(self):
        self.assertEqual(push_signal_validation_bounds(1001, 10), (1200, 1800))
        self.assertEqual(push_signal_validation_bounds(1200, 10), (1500, 2100))

    def test_push_validation_records_first_target_and_path_metrics(self):
        row = validation_row()
        candles = [
            path_kline(900, 150, 50),  # 推送前极值必须忽略
            path_kline(1200, 106, 99, 105),
            path_kline(1500, 108, 98, 107),
        ]
        status = evaluate_push_signal_validation(row, candles, now_epoch=1801)
        self.assertEqual(status, "win")
        self.assertEqual(row.first_hit, "target")
        self.assertAlmostEqual(row.max_favorable_pct, 8.0)
        self.assertAlmostEqual(row.max_adverse_pct, -2.0)
        self.assertEqual(row.observed_candle_count, 2)

    def test_push_validation_records_first_failure(self):
        row = validation_row()
        status = evaluate_push_signal_validation(
            row,
            [path_kline(1200, 101, 94), path_kline(1500, 108, 98)],
            now_epoch=1801,
        )
        self.assertEqual(status, "loss")
        self.assertEqual(row.first_hit, "failure")

    def test_neutral_and_same_candle_ambiguous_do_not_become_wins(self):
        neutral = validation_row()
        ambiguous = validation_row()
        self.assertEqual(evaluate_push_signal_validation(
            neutral, [path_kline(1200, 103, 98), path_kline(1500, 104, 97)], now_epoch=1801
        ), "neutral")
        self.assertEqual(evaluate_push_signal_validation(
            ambiguous, [path_kline(1200, 106, 94), path_kline(1500, 102, 99)], now_epoch=1801
        ), "insufficient_data")
        self.assertEqual(ambiguous.first_hit, "ambiguous")

    def test_missing_candles_wait_for_grace_then_become_insufficient(self):
        row = validation_row()
        only_one = [path_kline(1200, 106, 99)]
        self.assertEqual(evaluate_push_signal_validation(row, only_one, now_epoch=1801), "pending")
        self.assertEqual(evaluate_push_signal_validation(row, only_one, now_epoch=2401), "insufficient_data")

    def test_category_win_rate_excludes_neutral_pending_and_insufficient(self):
        rows = [
            SimpleNamespace(category="sudden_pump", category_label="5MIN急涨提醒", subtype="ignition", subtype_label="5MIN突然点火", status=status)
            for status in ("win", "win", "loss", "neutral", "pending", "insufficient_data")
        ]
        summary = push_signal_validation_summary(rows)[0]
        self.assertEqual(summary["decided"], 3)
        self.assertAlmostEqual(summary["win_rate"], 200 / 3)
        self.assertEqual(summary["neutral"], 1)
        self.assertEqual(summary["insufficient_data"], 1)


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app import (
    AKE_DIRECTION_CANDIDATES,
    ake_orderbook_wall_direction,
    ake_structure_direction,
    app,
    classify_early_trend_stage,
    thought_lark_ake_structure_message,
    thought_push_direction,
    thought_push_has_new_information,
    thought_signal_key,
    thought_horizon_outlook,
    stabilize_thought_horizon,
)


class ThoughtRepricingTests(unittest.TestCase):
    def setUp(self):
        self.analysis = {
            "symbol": "AKE/USDT",
            "last": 0.004305,
            "support": 0.00402,
            "resistance": 0.0044618,
            "funding_rate": 0.0342,
            "basis": 0.2675,
            "oi_value": 36_000_000,
            "futures_volume": 10_000_000,
            "spot_volume": 5_000_000,
            "open_spread": 0.2,
            "close_spread": 0.1,
            "source": "live",
            "validation": {},
            "orderbook_wall": {
                "wall_low": 0.0020,
                "wall_high": 0.0023,
                "wall_qty": 3_300_000,
                "wall_notional": 7_000,
                "reference_buckets": [{"qty": 3_300_000}],
            },
        }

    def test_historical_wall_does_not_drive_live_direction(self):
        self.assertIsNone(ake_orderbook_wall_direction(self.analysis))

    def test_basis_noise_around_zero_does_not_flip_bull_structure(self):
        self.analysis["validation"] = {
            key: {"price_change": 2.0, "oi_change": 2.0, "ratio_change": -1.0, "cvd": 100}
            for key in ("30m", "1h", "2h")
        }
        for basis in (0.0041, -0.0988, 0.0039):
            self.analysis["basis"] = basis
            self.assertEqual(ake_structure_direction(self.analysis), "ake_above_wall_bull_continue")

    def test_one_weak_window_does_not_confirm_main_long_unwind(self):
        self.analysis["basis"] = 0.02
        self.analysis["validation"] = {
            "30m": {"price_change": -1.0, "oi_change": -2.0, "ratio_change": 1.0, "cvd": -100},
            "1h": {"price_change": 2.0, "oi_change": 2.0, "ratio_change": -1.0, "cvd": 100},
            "2h": {"price_change": 3.0, "oi_change": 3.0, "ratio_change": -2.0, "cvd": 100},
        }
        self.assertEqual(ake_structure_direction(self.analysis), "ake_above_wall_bull_continue")

    def test_uncertain_new_range_is_silent(self):
        self.analysis["funding_rate"] = 0.0
        self.analysis["basis"] = -0.05
        self.analysis["validation"] = {
            key: {"price_change": 0.0, "oi_change": 0.0, "ratio_change": 0.0, "cvd": 0.0}
            for key in ("30m", "1h", "2h")
        }
        self.assertEqual(ake_structure_direction(self.analysis), "ake_above_wall_new_range")
        self.assertIsNone(thought_push_direction(self.analysis))

    def test_ake_opposite_direction_needs_three_observations_and_ten_minutes(self):
        AKE_DIRECTION_CANDIDATES.clear()
        previous = SimpleNamespace(direction="ake_above_wall_bull_continue")
        metrics = {"symbol": "AKE/USDT", "direction": "ake_main_long_unwind_watch"}
        with patch("app.time.monotonic", side_effect=(0, 300, 601)):
            self.assertFalse(thought_push_has_new_information(previous, metrics))
            self.assertFalse(thought_push_has_new_information(previous, metrics))
            self.assertTrue(thought_push_has_new_information(previous, metrics))

    def test_early_stage_does_not_require_positive_cvd(self):
        start = 1_700_000_000_000
        closed = []
        price = 100.0
        for index in range(25):
            close = price * (1.002 if index < 20 else 1.01)
            volume = 100.0
            taker_buy = 40.0 if index >= 20 else 50.0
            closed.append([start + index * 1_800_000, price, close * 1.002, price * 0.998, close, 0, 0, volume, 0, 0, taker_buy])
            price = close
        oi_rows = [{"sumOpenInterest": 100 + index} for index in range(5)]
        ratio_rows = [{"longShortRatio": 1.0 - index * 0.01} for index in range(5)]
        signal = classify_early_trend_stage(closed, oi_rows, ratio_rows)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["stage_key"], "prelaunch")
        self.assertLess(signal["cvd_change_5"], 0)

    def test_signal_key_uses_current_structure(self):
        key = thought_signal_key(self.analysis, "ake_above_wall_bull_continue")
        self.assertEqual(key, "ake_above_wall_bull_continue-current-range-upper")

    def test_message_reprices_key_levels(self):
        with app.app_context():
            message = thought_lark_ake_structure_message(
                self.analysis,
                "ake_above_wall_bull_continue",
            )
        self.assertIn("0.00402000", message)
        self.assertIn("0.00439487", message)
        self.assertTrue(message.startswith("短线方向："))
        self.assertNotIn("0.0022 上沿", message)
        self.assertNotIn("0.0022-0.0024", message)
        self.assertNotIn("0.0026/0.0028", message)

    def test_small_dynamic_zone_move_does_not_repeat_push(self):
        previous = SimpleNamespace(
            direction="ake_above_wall_bull_continue",
            signal_key="ake_above_wall_bull_continue-current-range-lower",
            last_price=0.004300,
            funding_rate=0.0342,
            basis=0.2675,
            oi_value=36_000_000,
            wall_qty=0,
            cvd_30m=1,
            cvd_1h=1,
            cvd_2h=1,
            pushed_at=datetime.now(),
        )
        metrics = {
            "symbol": "AKE/USDT",
            "direction": "ake_above_wall_bull_continue",
            "signal_key": "ake_above_wall_bull_continue-current-range-upper",
            "last_price": 0.004305,
            "funding_rate": 0.0342,
            "basis": 0.2675,
            "oi_value": 36_000_000,
            "wall_qty": 0,
            "cvd_30m": 1,
            "cvd_1h": 1,
            "cvd_2h": 1,
        }
        self.assertFalse(thought_push_has_new_information(previous, metrics))

    @staticmethod
    def horizon_analysis(sign):
        return {
            "source": "live",
            "validation": {
                key: {
                    "price_change": 2 * sign,
                    "cvd": 100 * sign,
                    "oi_change": 2,
                    "ratio_change": -2 * sign,
                }
                for key in ("1h", "2h", "4h")
            },
        }

    def test_medium_outlook_does_not_flip_within_ten_minutes(self):
        AKE_DIRECTION_CANDIDATES.clear()
        started = datetime(2026, 8, 2, 14, 20)
        previous = self.horizon_analysis(1)
        stabilize_thought_horizon(previous, {}, started)
        for minutes in (5, 10):
            current = self.horizon_analysis(-1)
            stabilize_thought_horizon(current, previous, started + timedelta(minutes=minutes))
            self.assertEqual(current["_horizon_outlook"]["medium"]["bias"], "偏多")
            self.assertEqual(current["_horizon_outlook"]["medium"]["candidate_bias"], "偏空")
            previous = current

    def test_medium_opposite_direction_requires_thirty_minutes(self):
        started = datetime(2026, 8, 2, 14, 20)
        previous = self.horizon_analysis(1)
        stabilize_thought_horizon(previous, {}, started)
        for minutes in (5, 15, 25, 36):
            current = self.horizon_analysis(-1)
            stabilize_thought_horizon(current, previous, started + timedelta(minutes=minutes))
            previous = current
        self.assertEqual(previous["_horizon_outlook"]["medium"]["bias"], "偏空")

    def test_price_oi_and_account_ratio_rising_is_not_clean_medium_bull(self):
        crowded_long = {
            "validation": {
                key: {"price_change": 3, "cvd": 100, "oi_change": 2, "ratio_change": 2}
                for key in ("1h", "2h", "4h")
            }
        }
        self.assertEqual(thought_horizon_outlook(crowded_long)["medium"]["bias"], "震荡/分歧")


if __name__ == "__main__":
    unittest.main()

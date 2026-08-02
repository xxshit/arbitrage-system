import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from app import (
    ake_orderbook_wall_direction,
    app,
    thought_lark_ake_structure_message,
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

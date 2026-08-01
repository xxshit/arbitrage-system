import unittest
from datetime import datetime
from types import SimpleNamespace

from app import (
    ake_orderbook_wall_direction,
    app,
    thought_lark_ake_structure_message,
    thought_push_has_new_information,
    thought_signal_key,
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


if __name__ == "__main__":
    unittest.main()

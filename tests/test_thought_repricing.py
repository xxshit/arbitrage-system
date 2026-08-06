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
    estimated_position_quantity_change,
    hei_risk_direction,
    position_quantity_change,
    send_early_trend_stage_push,
    thought_lark_ake_structure_message,
    thought_lark_hei_message,
    thought_primary_position_evidence,
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

    @staticmethod
    def hei_analysis(price_5m=0.2, price_15m=0.4, volume_ratio=1.0, cvd=10, basis=0.0):
        return {
            "symbol": "HEI/USDT",
            "source": "live",
            "last": 0.35,
            "support": 0.32,
            "resistance": 0.38,
            "basis": basis,
            "funding_rate": -0.03,
            "validation": {
                "30m": {"price_change": -1.0, "oi_change": 2.0, "ratio_change": 1.0, "cvd": -100, "volume_ratio": 1.2},
            },
            "micro_validation": {
                "5m": {"price_change": price_5m, "volume_ratio": volume_ratio, "cvd": cvd, "bucket_at": 1_700_000_000_000},
                "15m": {"price_change": price_15m, "volume_ratio": 1.1, "cvd": cvd},
            },
        }

    def test_hei_fast_watch_detects_5m_selloff(self):
        analysis = self.hei_analysis(price_5m=-3.2)
        self.assertEqual(hei_risk_direction(analysis), "hei_5m_selloff")
        self.assertEqual(thought_push_direction(analysis), "hei_5m_selloff")

    def test_hei_fast_watch_detects_volume_and_basis_risk(self):
        self.assertEqual(
            hei_risk_direction(self.hei_analysis(volume_ratio=2.6, cvd=-100)),
            "hei_sell_volume",
        )
        self.assertEqual(
            hei_risk_direction(self.hei_analysis(basis=-0.31)),
            "hei_basis_discount",
        )
        self.assertIsNone(hei_risk_direction(self.hei_analysis()))

    def test_hei_bar_dedupes_same_bucket_but_allows_next_bucket(self):
        analysis = self.hei_analysis(price_5m=-4.0)
        current_key = thought_signal_key(analysis, "hei_5m_selloff")
        previous = SimpleNamespace(direction="hei_5m_selloff", signal_key=current_key)
        metrics = {"symbol": "HEI/USDT", "direction": "hei_5m_selloff", "signal_key": current_key}
        self.assertFalse(thought_push_has_new_information(previous, metrics))
        metrics["signal_key"] = "hei_5m_selloff-1700000300000"
        self.assertTrue(thought_push_has_new_information(previous, metrics))

    def test_hei_message_keeps_chain_transfer_as_unverified_hypothesis(self):
        with app.app_context():
            message = thought_lark_hei_message(self.hei_analysis(price_5m=-4.0), "hei_5m_selloff")
        self.assertIn("用户假设", message)
        self.assertIn("尚未独立核验地址归属与转账目的", message)

    def test_notional_oi_is_adjusted_for_price_repricing(self):
        self.assertAlmostEqual(estimated_position_quantity_change(100, 100), 0.0)
        self.assertAlmostEqual(estimated_position_quantity_change(-50, -50), 0.0)
        self.assertAlmostEqual(estimated_position_quantity_change(0, 25), 25.0)
        self.assertAlmostEqual(position_quantity_change({
            "price_change": 100,
            "oi_change": 100,
            "position_quantity_change": 12.5,
        }), 12.5)

    def test_position_evidence_uses_period_values_and_cvd_as_confirmation(self):
        self.analysis["validation"] = {
            "30m": {"price_change": -2.0, "oi_change": -8.0, "ratio_change": 3.0, "cvd": -100_000},
            "1h": {"price_change": -4.0, "oi_change": -12.0, "ratio_change": 5.0, "cvd": -300_000},
            "2h": {"price_change": -5.0, "oi_change": -15.0, "ratio_change": 7.0, "cvd": -500_000},
        }
        evidence = thought_primary_position_evidence(self.analysis)
        self.assertIn("近2H价格 -5.00%", evidence["summary"])
        self.assertIn("名义持仓 -15.00%", evidence["summary"])
        self.assertIn("估算实际合约数量 -10.53%", evidence["summary"])
        self.assertIn("多空人数比同期上升 +7.00%", evidence["summary"])
        self.assertIn("主动卖出与偏空主结构同向", evidence["summary"])

    def test_ake_unwind_message_no_longer_uses_fixed_cvd_slogan(self):
        self.analysis["validation"] = {
            "30m": {"price_change": -2.0, "oi_change": -8.0, "ratio_change": 3.0, "cvd": 100_000},
            "1h": {"price_change": -4.0, "oi_change": -12.0, "ratio_change": 5.0, "cvd": 300_000},
            "2h": {"price_change": -5.0, "oi_change": -15.0, "ratio_change": 7.0, "cvd": 500_000},
        }
        with app.app_context():
            message = thought_lark_ake_structure_message(self.analysis, "ake_main_long_unwind_watch")
        self.assertIn("近2H价格 -5.00%", message)
        self.assertIn("估算实际合约数量 -10.53%", message)
        self.assertIn("主动买入与当前偏弱结构背离", message)
        self.assertNotIn("CVD上涨不能单独看多，持仓和人数比已经给出反证", message)

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

    def test_loose_prelaunch_candidate_never_sends_lark(self):
        with patch("app.urlopen") as mocked_urlopen:
            self.assertFalse(send_early_trend_stage_push([{
                "symbol": "TEST/USDT", "signal_type": "prelaunch",
                "stage_key": "prelaunch", "stage_number": 0,
            }]))
        mocked_urlopen.assert_not_called()

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

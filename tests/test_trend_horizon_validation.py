import unittest
from types import SimpleNamespace

from app import (
    TREND_HORIZON_META,
    ake_horizons_needing_validation,
    build_ake_horizon_definitions,
    current_ake_horizon_rows,
    horizon_move_pct,
    trend_horizon_outcome,
    trend_horizon_review_text,
    trend_horizon_statistics,
)


class TrendHorizonValidationTests(unittest.TestCase):
    def test_long_and_short_favorable_moves_use_opposite_signs(self):
        self.assertAlmostEqual(horizon_move_pct("long", 110, 100), 10.0)
        self.assertAlmostEqual(horizon_move_pct("short", 90, 100), 10.0)
        self.assertAlmostEqual(horizon_move_pct("short", 110, 100), -10.0)

    def test_three_horizons_have_distinct_review_windows(self):
        self.assertEqual(TREND_HORIZON_META["short"]["window"], "30MIN-3H")
        self.assertEqual(TREND_HORIZON_META["medium"]["window"], "4H-24H")
        self.assertEqual(TREND_HORIZON_META["long"]["window"], "1D-3D")
        self.assertLess(TREND_HORIZON_META["short"]["expire_minutes"], TREND_HORIZON_META["medium"]["expire_minutes"])
        self.assertLess(TREND_HORIZON_META["medium"]["expire_minutes"], TREND_HORIZON_META["long"]["expire_minutes"])

    def test_each_horizon_renews_without_waiting_for_long_plan(self):
        active = [SimpleNamespace(horizon="long")]
        self.assertEqual(ake_horizons_needing_validation(active), ["short", "medium"])

    def test_current_payload_can_combine_active_horizons_from_different_batches(self):
        rows = [
            SimpleNamespace(horizon="short", status="active", batch_key="new-short"),
            SimpleNamespace(horizon="medium", status="active", batch_key="new-medium"),
            SimpleNamespace(horizon="long", status="active", batch_key="old-long"),
            SimpleNamespace(horizon="short", status="completed", batch_key="old-batch"),
        ]
        selected = current_ake_horizon_rows(rows)
        self.assertEqual([row.horizon for row in selected], ["short", "medium", "long"])
        self.assertEqual([row.batch_key for row in selected], ["new-short", "new-medium", "old-long"])

    def test_review_keeps_stop_then_recovery_as_a_learning_case(self):
        plan = SimpleNamespace(
            first_hit="hard_stop",
            max_favorable_pct=3.2,
            max_adverse_pct=-4.1,
            path_state="stop_then_recovery",
        )
        review = trend_horizon_review_text(plan, final=True)
        self.assertIn("止损后价格重新回到有利方向", review)
        self.assertIn("止损是否过紧", review)

    def test_review_marks_near_target_reversal(self):
        plan = SimpleNamespace(
            first_hit=None,
            max_favorable_pct=4.8,
            max_adverse_pct=-1.6,
            path_state="near_target_then_reversal",
        )
        review = trend_horizon_review_text(plan)
        self.assertIn("曾接近目标但未到达便反转", review)

    def test_completed_direction_without_target_uses_conservative_outcome(self):
        correct = SimpleNamespace(
            first_hit=None, status="completed", direction="long", anchor_price=100, latest_price=102
        )
        unresolved = SimpleNamespace(
            first_hit=None, status="completed", direction="long", anchor_price=100, latest_price=100.4
        )
        self.assertEqual(trend_horizon_outcome(correct), "correct")
        self.assertEqual(trend_horizon_outcome(unresolved), "unresolved")

    def test_accuracy_excludes_unresolved_and_ambiguous_samples(self):
        rows = [
            SimpleNamespace(first_hit="take_profit", status="completed", max_favorable_pct=4, max_adverse_pct=-1),
            SimpleNamespace(first_hit="hard_stop", status="completed", max_favorable_pct=1, max_adverse_pct=-3),
            SimpleNamespace(first_hit="ambiguous_same_candle", status="completed", max_favorable_pct=2, max_adverse_pct=-2),
        ]
        stats = trend_horizon_statistics(rows)
        self.assertEqual(stats["correct"], 1)
        self.assertEqual(stats["wrong"], 1)
        self.assertEqual(stats["inconclusive"], 1)
        self.assertEqual(stats["accuracy"], 50.0)

    def test_dynamic_definitions_keep_horizons_independent_and_apply_calibration(self):
        metrics = {
            "price_30m": 1.2, "price_1h": 1.8, "price_4h": -2.0,
            "price_24h": 4.0, "price_3d": 7.0,
            "cvd_1h": 10, "cvd_4h": -10, "cvd_24h": 20, "cvd_3d": 30,
            "oi_1h": 1.0, "oi_4h": 1.0, "oi_24h": 2.0, "oi_3d": 3.0,
            "ratio_1h": -1.0, "ratio_4h": 1.0, "ratio_24h": -2.0, "ratio_3d": -3.0,
            "high_30m": 103, "low_30m": 98,
        }
        baseline = build_ake_horizon_definitions(100, 2, 99, 101, metrics, {})
        calibrated = build_ake_horizon_definitions(
            100,
            2,
            99,
            101,
            metrics,
            {"short": {"near_target_reversal_count": 1, "stop_recovery_count": 1}},
        )
        self.assertEqual(baseline["short"]["direction"], "long")
        self.assertEqual(baseline["medium"]["direction"], "short")
        self.assertEqual(baseline["long"]["direction"], "long")
        self.assertLess(calibrated["short"]["tp"], baseline["short"]["tp"])
        self.assertLess(calibrated["short"]["hard"], baseline["short"]["hard"])


if __name__ == "__main__":
    unittest.main()

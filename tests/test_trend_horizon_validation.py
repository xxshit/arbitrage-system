import unittest
from types import SimpleNamespace

from app import TREND_HORIZON_META, horizon_move_pct, trend_horizon_review_text


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


if __name__ == "__main__":
    unittest.main()

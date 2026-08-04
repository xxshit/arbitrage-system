import os
import unittest
from datetime import datetime


os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "dual-funding-test-secret"

from app import SHANGHAI_TZ, calculate_dual_funding_differences


def settled_at(year, month, day, hour):
    return int(datetime(year, month, day, hour, tzinfo=SHANGHAI_TZ).timestamp() * 1000)


class DualFundingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 4, 12, tzinfo=SHANGHAI_TZ)

    def test_uses_actual_settlement_events_and_hedge_signs(self):
        long_records = [
            {"funding_time": settled_at(2026, 8, 3, 0), "funding_rate": -0.010},
            {"funding_time": settled_at(2026, 8, 4, 0), "funding_rate": 0.010},
            {"funding_time": settled_at(2026, 8, 4, 8), "funding_rate": 0.020},
        ]
        short_records = [
            {"funding_time": settled_at(2026, 8, 3, 8), "funding_rate": 0.008},
            {"funding_time": settled_at(2026, 8, 4, 0), "funding_rate": 0.005},
            {"funding_time": settled_at(2026, 8, 4, 4), "funding_rate": -0.003},
            {"funding_time": settled_at(2026, 8, 4, 8), "funding_rate": 0.006},
        ]

        result = calculate_dual_funding_differences(long_records, short_records, self.now)

        # 08:00 双方同时结算：空端 +0.006%，多端支付 0.020%。
        self.assertAlmostEqual(result["previous"], -0.014)
        # 24H 使用北京时间今日 00:00 至当前的全部真实结算事件。
        self.assertAlmostEqual(result["day_1"], -0.022)
        # 3D 再包含昨日的两个结算事件，而不是按固定周期补齐虚拟记录。
        self.assertAlmostEqual(result["day_3"], -0.004)

    def test_previous_can_be_a_single_side_settlement(self):
        long_records = [
            {"funding_time": settled_at(2026, 8, 4, 8), "funding_rate": 0.010},
        ]
        short_records = [
            {"funding_time": settled_at(2026, 8, 4, 8), "funding_rate": 0.005},
            {"funding_time": settled_at(2026, 8, 4, 10), "funding_rate": 0.004},
        ]

        result = calculate_dual_funding_differences(long_records, short_records, self.now)

        self.assertAlmostEqual(result["previous"], 0.004)

    def test_does_not_present_a_partial_leg_as_net_funding(self):
        result = calculate_dual_funding_differences(
            [],
            [{"funding_time": settled_at(2026, 8, 4, 8), "funding_rate": 0.010}],
            self.now,
        )

        self.assertTrue(all(value is None for value in result.values()))


if __name__ == "__main__":
    unittest.main()

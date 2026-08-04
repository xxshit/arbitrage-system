import os
import unittest
from datetime import datetime


os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "symbol-detail-funding-test-secret"

from app import (
    SHANGHAI_TZ,
    compose_detail_funding_events,
    detail_funding_history_is_continuous,
    parse_detail_funding_window,
)


def settled_at(year, month, day, hour):
    return int(datetime(year, month, day, hour, tzinfo=SHANGHAI_TZ).timestamp() * 1000)


class SymbolDetailFundingTests(unittest.TestCase):
    def test_selected_calendar_dates_become_inclusive_shanghai_window(self):
        start, end, start_label, end_label = parse_detail_funding_window("2026-08-01", "2026-08-02")

        self.assertEqual(start, settled_at(2026, 8, 1, 0))
        self.assertEqual(end, settled_at(2026, 8, 3, 0))
        self.assertEqual(start_label, "2026-08-01")
        self.assertEqual(end_label, "2026-08-02")

    def test_invalid_reversed_date_range_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "开始日期不能晚于结束日期"):
            parse_detail_funding_window("2026-08-03", "2026-08-02")

    def test_exchange_pair_is_merged_by_real_settlement_minute(self):
        long_records = [
            {"funding_time": settled_at(2026, 8, 1, 8), "funding_rate": 0.010},
            {"funding_time": settled_at(2026, 8, 1, 16), "funding_rate": -0.020},
        ]
        short_records = [
            {"funding_time": settled_at(2026, 8, 1, 8), "funding_rate": 0.006},
            {"funding_time": settled_at(2026, 8, 1, 12), "funding_rate": -0.003},
        ]

        events = compose_detail_funding_events(long_records, short_records)

        self.assertEqual([item["time"] for item in events], [
            "2026-08-01 08:00", "2026-08-01 12:00", "2026-08-01 16:00",
        ])
        self.assertAlmostEqual(events[0]["rate"], -0.004)
        self.assertAlmostEqual(events[1]["rate"], -0.003)
        self.assertAlmostEqual(events[2]["rate"], 0.020)
        self.assertEqual(events[1]["long_rate"], None)
        self.assertAlmostEqual(events[1]["short_rate"], -0.003)

    def test_single_exchange_keeps_original_funding_sign(self):
        events = compose_detail_funding_events([
            {"funding_time": settled_at(2026, 8, 2, 0), "funding_rate": -0.015},
        ])

        self.assertAlmostEqual(events[0]["rate"], -0.015)
        self.assertIsNone(events[0]["long_rate"])
        self.assertAlmostEqual(events[0]["short_rate"], -0.015)

    def test_missing_recent_settlements_marks_history_incomplete(self):
        start, end, _start_label, _end_label = parse_detail_funding_window("2026-08-01", "2026-08-04")
        records = [
            {"funding_time": settled_at(2026, 8, 1, hour), "funding_rate": 0.005}
            for hour in (0, 8, 16)
        ]

        self.assertFalse(detail_funding_history_is_continuous(
            records, start, end, datetime(2026, 8, 4, 18, tzinfo=SHANGHAI_TZ),
        ))

    def test_regular_eight_hour_settlements_cover_selected_window(self):
        start, end, _start_label, _end_label = parse_detail_funding_window("2026-08-03", "2026-08-03")
        records = [
            {"funding_time": settled_at(2026, 8, 3, hour), "funding_rate": 0.005}
            for hour in (0, 8, 16)
        ]

        self.assertTrue(detail_funding_history_is_continuous(
            records, start, end, datetime(2026, 8, 4, 12, tzinfo=SHANGHAI_TZ),
        ))


if __name__ == "__main__":
    unittest.main()

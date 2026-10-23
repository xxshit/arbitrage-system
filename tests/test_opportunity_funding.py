import math
import unittest
from opportunity_funding import funding_quality, funding_window

NOW = 1800000000


class FundingQualityTests(unittest.TestCase):
    def events(self, hours=4, rate=.005, days=8):
        return [(NOW-i*hours*3600, rate) for i in range(int(days*24/hours)+1)]

    def test_exact_rolling_windows_and_baseline(self):
        result = funding_quality(self.events(), NOW, 4)
        for key, count in (("24h",6),("3d",18),("7d",42)):
            row = result["windows"][key]
            self.assertEqual(row["count"], count)
            self.assertEqual(row["classified"], count)
            self.assertEqual(row["above"], 0)
            self.assertTrue(row["complete"])
            self.assertAlmostEqual(row["total"], count*.005)

    def test_negative_zero_and_unknown_are_not_low_floor_or_positive(self):
        events = self.events()
        events[0] = (NOW, -.02)
        events[1] = (NOW-14400, 0)
        events[2] = (NOW-28800, .05)
        row = funding_quality(events,NOW,4)["windows"]["24h"]
        self.assertEqual((row["negative"],row["zero"],row["above"]),(1,1,1))
        self.assertEqual(row["recent_positive_streak"],0)
        self.assertAlmostEqual(row["total"],.045)

    def test_missing_period_is_not_invented_as_longer_settlement(self):
        events = [(t,r) for t,r in self.events() if t != NOW-14400]
        row = funding_quality(events,NOW,4)["windows"]["24h"]
        self.assertEqual(row["count"],5)
        self.assertEqual(row["unknown_count"],1)
        self.assertFalse(row["complete"])
        self.assertEqual(row["classified"],4)

    def test_period_switch_uses_local_cadence_and_keeps_uncertainty(self):
        events = [(NOW-4*3600-i*4*3600,.005) for i in range(10)]
        events += [(NOW-i*3600,.00125) for i in range(4)]
        row = funding_quality(events,NOW,1)["windows"]["24h"]
        self.assertEqual(row["above"],0)
        self.assertTrue(row["complete"])
        # First lengthening may instead be a missing period: conservatively unknown.
        events = [(NOW-4*3600-i*3600,.00125) for i in range(30)] + [(NOW,.005)]
        row = funding_quality(events,NOW,4)["windows"]["24h"]
        self.assertEqual(row["unknown_count"],1)
        self.assertFalse(row["complete"])

    def test_missing_early_history_or_overdue_tail_is_not_complete(self):
        row = funding_quality(self.events(days=2),NOW,4)["windows"]["7d"]
        self.assertFalse(row["complete"])
        row = funding_quality(self.events()[2:],NOW,4)["windows"]["24h"]
        self.assertFalse(row["complete"])

    def test_duplicates_future_invalid_values_and_boundary(self):
        events = self.events()+[(NOW, .005),(NOW+1,99),(NOW-1,math.nan),(True,1)]
        row = funding_window(events,NOW-86400,NOW,4)
        self.assertEqual(row["count"],6)
        self.assertAlmostEqual(row["total"],.03)
        self.assertIsNone(funding_window([],NOW-86400,NOW)["total"])
        self.assertIsNone(funding_window([],NOW-86400,NOW)["above_ratio"])

    def test_outcome_does_not_use_post_deadline_settlements(self):
        before = funding_window(self.events(),NOW-86400,NOW-3600)
        after = funding_window(self.events()+[(NOW+3600,999)],NOW-86400,NOW-3600)
        self.assertEqual(before,after)

    def test_newly_shorter_current_interval_flags_overdue_history(self):
        row = funding_quality(self.events()[1:],NOW,1)["windows"]["24h"]
        self.assertFalse(row["complete"])

    def test_small_exchange_timestamp_jitter_is_not_a_fake_gap(self):
        events=[(t+(i%2)*.001,r) for i,(t,r) in enumerate(self.events())]
        row=funding_quality(events,NOW,4)["windows"]["24h"]
        self.assertEqual(row["unknown_count"],0)

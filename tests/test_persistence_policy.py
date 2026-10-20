import copy
import unittest

from anomaly_streams import AlertStreamHub
from persistence_policy import funding_write_due


class PulseCoalescingTests(unittest.TestCase):
    def setUp(self):
        self.hub = AlertStreamHub()
        self.pulse = dict(id="demo", version=1, state="watch", notice_version=1,
                          peak=.6, current=.6, ranges={"open_spread": {"min": 0, "max": .6}})

    def put(self, **changes):
        self.pulse = {**self.pulse, "version": self.pulse["version"]+1, **changes}
        self.hub._updates([copy.deepcopy(self.pulse)])

    def commit(self, at):
        batch = self.hub.batch(now=at)
        self.hub.acknowledge(batch, now=at)
        return batch

    def test_quiet_tail_writes_at_most_every_15_seconds(self):
        self.put()
        self.assertEqual(len(self.commit(1000)), 1)
        writes = 0
        for tick in range(1, 301):
            self.put(current=.60 + (tick % 10)/10000)
            writes += len(self.commit(1000+tick/10))
        self.assertEqual(writes, 2)

    def test_new_extreme_and_fast_retrace_then_restart_bypass_heartbeat(self):
        self.put()
        self.commit(1000)
        self.put(peak=1.1, current=1.1)
        self.assertEqual(self.commit(1000.1)[0]["peak"], 1.1)
        self.put(state="retraced", current=.1, notice_version=2)
        self.assertEqual(self.commit(1000.2)[0]["state"], "retraced")
        self.put(id="restart", state="watch", peak=.7, current=.7)
        self.assertEqual(self.commit(1000.3)[0]["id"], "restart")

    def test_small_extrema_survive_aggregation_and_final_flush(self):
        self.put()
        self.commit(1000)
        self.put(peak=.65, current=.65, ranges={"open_spread": {"min": 0, "max": .65}})
        self.assertEqual(self.hub.batch(now=1001), [])
        self.put(current=.61, ended_at=1002, state="settled")
        saved = self.commit(1002)[0]
        self.assertEqual((saved["peak"], saved["current"], saved["ranges"]["open_spread"]["max"]), (.65, .61, .65))

    def test_failed_commit_does_not_advance_checkpoint(self):
        self.put()
        first = self.hub.batch(now=1000)
        self.assertEqual(first, self.hub.batch(now=1000.1))
        self.assertEqual(self.hub.persisted_checkpoints, {})

    def test_update_during_commit_is_not_acknowledged_as_saved(self):
        self.put()
        first = self.hub.batch(now=1000)
        self.put(peak=.9)
        self.hub.acknowledge(first, now=1000)
        self.assertEqual(self.hub.batch(now=1000.1)[0]["peak"], .9)

    def test_committed_watermarks_are_bounded(self):
        self.hub.acknowledge([dict(id=str(i), version=1) for i in range(20005)], now=1000)
        self.assertEqual(len(self.hub.persisted_checkpoints), 20000)


class FundingCheckpointTests(unittest.TestCase):
    def test_volatile_rates_defer_but_event_and_boundary_are_immediate(self):
        old = dict(observed_at=1000, rate_at=1000, info_at=1000, rate=.01, next_at=2000, side=None)
        current = {**old, "observed_at": 1059, "rate_at": 1059, "rate": .02}
        self.assertFalse(funding_write_due(old, current, []))
        self.assertTrue(funding_write_due(old, {**current, "observed_at": 1060}, []))
        self.assertTrue(funding_write_due(old, current, [{}]))
        self.assertTrue(funding_write_due(old, {**current, "pending": [{"at": 1001}]}, []))
        self.assertTrue(funding_write_due(old, {**current, "side": "positive"}, []))

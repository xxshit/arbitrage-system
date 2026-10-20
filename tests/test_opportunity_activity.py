import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from opportunity_activity import ActivityLedger

NOW = 1792540800
PATH = ("spot_futures", "TEST/USDT", "Gate", "Binance")


def quote(at, value):
    return dict(long={"bid":100,"ask":100},short={"bid":100+value,"ask":100+value},
                long_at=at,short_at=at)


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = ActivityLedger(self.temp.name)
        self.ledger.restore()

    def tearDown(self):
        self.temp.cleanup()

    def observe(self, values, start=NOW, path=PATH):
        for i, value in enumerate(values):
            at=start+i*5
            self.ledger.observe({path:quote(at,value)},at)
        return start+(len(values)-1)*5

    def test_peak_retrace_and_restart_count_independent_episodes_not_ticks(self):
        at=self.observe([0,1.1,1.5,1.2,.6,.4,1.2,-1.1,-1.4,-.4])
        self.ledger.persist(at)
        summary=self.ledger.snapshot()[PATH[1:3]]
        self.assertEqual(summary["count"],3)
        self.assertEqual(summary["returned"],3)
        self.assertEqual(summary["windows"]["24h"]["positive"],2)
        self.assertEqual(summary["windows"]["24h"]["negative"],1)
        self.assertAlmostEqual(summary["windows"]["24h"]["recent"][-1]["peak"],1.5)

    def test_restart_retains_active_episode_and_peak(self):
        at=self.observe([0,1.1,1.5])
        self.ledger.persist(at)
        restarted=ActivityLedger(self.temp.name);restarted.restore()
        restarted.observe({PATH:quote(at+5,1.2)},at+5)
        restarted.persist(at+5)
        self.assertEqual(restarted.snapshot()[PATH[1:3]]["count"],1)
        self.assertAlmostEqual(restarted.snapshot()[PATH[1:3]]["peak"],1.5)

    def test_gap_is_not_convergence_and_first_outside_is_censored(self):
        at=self.observe([0,1.1])
        self.observe([.1],at+30)
        self.ledger.persist(at+30)
        self.assertEqual(self.ledger.snapshot()[PATH[1:3]]["returned"],0)
        self.observe([1.2,.1],at+60)
        self.ledger.persist(at+65)
        summary=self.ledger.snapshot()[PATH[1:3]]
        self.assertEqual(summary["count"],2)
        self.assertEqual(summary["returned"],0)

    def test_five_day_old_events_in_7d_not_1d_and_20d_survive_curves(self):
        self.observe([0,1.2,.1],NOW-20*86400)
        self.observe([0,-1.1,.1],NOW-5*86400)
        self.ledger.persist(NOW)
        summary=self.ledger.snapshot()[PATH[1:3]]
        self.assertEqual(summary["count"],2)
        self.assertEqual(summary["windows"]["24h"]["negative"],0)
        self.assertEqual(summary["windows"]["7d"]["negative"],1)
        self.assertEqual(summary["windows"]["15d"]["positive"],0)

    def test_only_fresh_exact_spot_path_is_counted(self):
        self.observe([0,1.1])
        other=("spot_futures","TEST/USDT","Bitget","Binance")
        self.observe([0,.3],path=other)
        self.ledger.observe({("futures_futures",*PATH[1:]):quote(NOW,2)},NOW)
        self.ledger.observe({PATH:quote(NOW+10,2)},NOW+20)
        self.ledger.persist(NOW+20)
        self.assertEqual(self.ledger.snapshot()[PATH[1:3]]["count"],1)
        self.assertEqual(self.ledger.snapshot()[other[1:3]]["count"],0)

    def test_disk_guard_keeps_pending_and_does_not_fake_persistence(self):
        self.observe([0,1.1])
        with patch("opportunity_activity.shutil.disk_usage",return_value=SimpleNamespace(free=0)):
            with self.assertRaises(OSError):
                self.ledger.persist(NOW+5)
        self.assertTrue(self.ledger.pending)
        self.ledger.persist(NOW+5)
        self.assertFalse(self.ledger.pending)

    def test_new_ledger_does_not_invent_old_counts(self):
        self.assertEqual(self.ledger.snapshot(),{})
        self.observe([.5])
        self.ledger.persist(NOW)
        self.assertEqual(self.ledger.snapshot()[PATH[1:3]]["since"],NOW)
        self.assertEqual(self.ledger.snapshot()[PATH[1:3]]["count"],0)

    def test_boundary_values_and_checkpoint(self):
        at=self.observe([0,1,.5,1,-1,-.5])
        self.ledger.persist(at)
        self.assertEqual(self.ledger.snapshot()[PATH[1:3]]["count"],3)
        self.ledger.checkpoint()
        restored=ActivityLedger(self.ledger.directory/"checkpoints")
        restored.restore()
        self.assertEqual(restored.states,self.ledger.states)

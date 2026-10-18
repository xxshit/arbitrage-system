import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from anomaly_engine import PulseDetector, alert_round_ids, motion_threshold, pulse_is_actionable


PATH = ("spot_futures", "QUALITY/USDT", "Gate", "Binance")


def context(value):
    return dict(open_spread=value, close_spread=value+.02, mid_spread=value+.01,
                long_mid=100, short_mid=100+value, basis=value, funding_rate=None)


def historical_pulse(baseline=0, trigger=.2, peak=.3, current=.1, metric="spread", identifier="old"):
    return dict(id=identifier, episode=identifier, ordinal=1, metric=metric, path=PATH,
                baseline=baseline, baseline_at=1000, baseline_context=context(baseline),
                trigger=trigger, triggered_at=1001, trigger_context=context(trigger),
                peak=peak, peak_at=1002, peak_context=context(peak),
                current=current, current_at=1003, current_context=context(current),
                state="retraced", direction=1, version=3, notice_version=2, noticed_at=1003,
                retrace_at=1003, ranges={"open_spread": {"min": min(trigger, peak, current), "max": max(trigger, peak, current)}, "basis": {"min": min(trigger, peak, current), "max": max(trigger, peak, current)}})


class AlertQualityTests(unittest.TestCase):
    def test_speed_lanes_and_anchor_expiry_are_explicit(self):
        for seconds, expected in ((15, .5), (15.01, .75), (30, .75), (30.01, 1), (120, 1), (300, 1), (300.01, None)):
            self.assertEqual(motion_threshold("spread", -.1, -.6, seconds), expected)
        self.assertIsNone(motion_threshold("spread", -.3, .3, 1))
        self.assertEqual(motion_threshold("spread", .1, .5, 299), .4)
        self.assertIsNone(motion_threshold("spread", .1, .5, 301))

    def test_repeated_user_examples_are_not_new_alerts(self):
        for values in ((-.3191, -.0639), (-.3551, -.142), (-.094, -.3445), (-.6, -.31), (-.3, .3)):
            engine, output = PulseDetector(), []
            for tick in range(180):
                value = values[tick % 2]
                output += engine.observe(PATH, "spread", value, 1000+tick, context(value))
            self.assertEqual(output, [], values)

    def test_near_zero_to_positive_half_percent_is_still_immediate(self):
        engine = PulseDetector()
        engine.observe(PATH, "spread", .08, 1000, context(.08))
        self.assertFalse(engine.observe(PATH, "spread", .49, 1000.01, context(.49)))
        pulse = engine.observe(PATH, "spread", .5, 1000.02, context(.5))[0]
        self.assertEqual((pulse["triggered_at"], pulse["threshold"], pulse["state"]), (1000.02, .4, "watch"))
        self.assertTrue(pulse_is_actionable(pulse))

    def test_small_rebounds_update_same_record_and_real_restart_is_kept(self):
        engine, snapshots = PulseDetector(), {}
        for tick, value in enumerate((0, 1, .1, .35, .1, .65)):
            for pulse in engine.observe(PATH, "spread", value, 1000+tick, context(value)):
                snapshots[pulse["id"]] = pulse
        self.assertEqual(len(snapshots), 2)
        first, restart = snapshots.values()
        self.assertEqual(first["peak"], 1)
        self.assertEqual((restart["ordinal"], restart["baseline"], restart["peak"]), (2, .1, .65))
        self.assertEqual(first["episode"], restart["episode"])

    def test_one_minute_pause_does_not_reset_same_episode(self):
        engine = PulseDetector()
        engine.observe(PATH, "spread", 0, 1000, context(0))
        first = engine.observe(PATH, "spread", 1, 1001, context(1))[0]
        for tick in range(2, 61):
            engine.observe(PATH, "spread", .1, 1000+tick, context(.1))
        restart = engine.observe(PATH, "spread", .65, 1061, context(.65))[-1]
        self.assertEqual((restart["episode"], restart["ordinal"]), (first["episode"], 2))

    def test_current_rules_filter_old_noise_but_do_not_erase_a_real_spike(self):
        self.assertFalse(pulse_is_actionable(historical_pulse()))
        self.assertFalse(pulse_is_actionable(historical_pulse(baseline=.3, trigger=-.3, peak=-.35)))
        self.assertFalse(pulse_is_actionable(historical_pulse(baseline=-.1, trigger=.15, peak=.3, metric="basis")))
        spike = historical_pulse(trigger=.21, peak=1.01, current=.01)
        self.assertTrue(pulse_is_actionable(spike))
        spike["peak_at"] = 1020  # .75-point 30s lane still qualifies
        self.assertTrue(pulse_is_actionable(spike))
        early = historical_pulse(baseline=.08, trigger=.5, peak=.6, current=.1)
        early["peak_at"] = 1240
        self.assertTrue(pulse_is_actionable(early))  # original valid trigger remains valid

    def test_old_width_only_or_missing_quote_evidence_cannot_be_promoted(self):
        pulse = historical_pulse(trigger=1, peak=1)
        pulse["trigger_context"] = pulse["peak_context"] = {**context(1), "close_spread": 0, "mid_spread": 0}
        self.assertFalse(pulse_is_actionable(pulse))

    def test_historical_restarts_require_the_same_rearm_size_as_live_detection(self):
        pulse = historical_pulse(baseline=.1, trigger=.5, peak=.55)
        self.assertTrue(pulse_is_actionable(pulse))
        pulse["ordinal"] = 2
        self.assertFalse(pulse_is_actionable(pulse))
        pulse.update(peak=.65, peak_context=context(.65))
        self.assertTrue(pulse_is_actionable(pulse))

    def test_missing_trigger_does_not_discard_a_well_evidenced_peak(self):
        pulse = historical_pulse(peak=1.02)
        pulse.pop("trigger_context")
        self.assertTrue(pulse_is_actionable(pulse))
        pulse.pop("baseline_context")
        self.assertFalse(pulse_is_actionable(pulse))

    def test_rounds_merge_venue_copies_and_restarts_not_later_independent_onsets(self):
        start = datetime(2026, 10, 19)
        events, pulses = [], {}
        for identifier, seconds, episode, venue, symbol in (
            (1, 0, "a", "Gate", "ONE/USDT"),
            (2, 20, "b", "Bitget", "ONE/USDT"),
            (3, 40, "c", "Binance", "ONE/USDT"),
            (4, 100, "a", "Gate", "ONE/USDT"),
            (5, 1, "d", "Gate", "TWO/USDT"),
        ):
            events.append(SimpleNamespace(id=identifier, created_at=start+timedelta(seconds=seconds), symbol=symbol,
                                          strategy="spot_futures", long_exchange=venue, short_exchange="Binance"))
            pulses[identifier] = dict(id=str(identifier), episode=episode, metric="spread")
        groups = alert_round_ids(events, pulses)
        self.assertEqual(groups[1], groups[2])
        self.assertEqual(groups[1], groups[4])
        self.assertNotEqual(groups[1], groups[3])
        self.assertNotEqual(groups[1], groups[5])
        self.assertEqual(len(set(groups.values())), 3)


if __name__ == "__main__":
    unittest.main()

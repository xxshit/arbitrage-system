import json
import math
import unittest

from alert_reminders import WINDOW, admit_pulse, current_notice, reminder_count, reminder_state_id
from tests.test_alert_quality import historical_pulse, context


def motion(start, peak, at=1000, identifier="first", metric="spread", path=None):
    pulse = historical_pulse(baseline=start, trigger=peak, peak=peak, current=peak, metric=metric, identifier=identifier)
    pulse.update(direction=1 if peak > start else -1, state="expanded", baseline_at=at,
                 triggered_at=at+1, peak_at=at+1, current_at=at+1, noticed_at=at+1)
    if path:
        pulse["path"] = path
    if metric == "price":
        for key, value in (("baseline_context", start), ("trigger_context", peak), ("peak_context", peak)):
            pulse[key] = {"long_mid": math.exp(value / 100), "short_mid": math.exp(value / 100)}
    return pulse


class ReminderTests(unittest.TestCase):
    def save(self, state, pulse, previous=None, **kwargs):
        state, metadata = admit_pulse(state, pulse, previous, **kwargs)
        return state, {**pulse, "reminder": metadata}

    def test_user_range_oscillates_100_times_but_only_first_and_real_break_remind(self):
        state, first = self.save({}, motion(.5, 1))
        notices = list(first["reminder"]["notices"])
        for index in range(100):
            pulse = motion(1, .5, 1010+index*10, f"down-{index}") if index % 2 == 0 else motion(.5, 1, 1010+index*10, f"up-{index}")
            state, saved = self.save(state, pulse)
            notices.extend(saved["reminder"]["notices"])
        self.assertEqual(len(notices), 1)
        state, last = self.save(state, motion(.5, 1.2, 3000, "break"))
        self.assertEqual(last["reminder"]["notices"][0]["kind"], "breakout")
        state, repeat = self.save(state, motion(.5, 1.2, 4000, "repeat"))
        self.assertEqual(repeat["reminder"]["notices"], [])

    def test_minor_observed_peaks_do_not_ratchet_the_notification_reference(self):
        state, original = self.save({}, motion(0, 1))
        for i, value in enumerate((1.01, 1.05, 1.1, 1.199999)):
            state, saved = self.save(state, motion(.5, value, 1020+i*10, str(i)))
            self.assertFalse(saved["reminder"]["notices"])
        state, saved = self.save(state, motion(.5, 1.2, 1100, "exact"))
        self.assertEqual(reminder_count(saved, 1101), 1)
        self.assertEqual(state["points"][-1]["value"], 1.2)

    def test_larger_old_peak_uses_twenty_percent_and_signed_lower_boundary(self):
        state, _ = self.save({}, motion(0, 2))
        state, small = self.save(state, motion(.5, 2.39, 1020, "small"))
        self.assertFalse(small["reminder"]["notices"])
        state, large = self.save(state, motion(.5, 2.4, 1030, "large"))
        self.assertTrue(large["reminder"]["notices"])
        state, negative = self.save(state, motion(0, -2, 1040, "negative"))
        self.assertEqual(negative["reminder"]["notices"][0]["direction"], -1)
        state, small = self.save(state, motion(0, -2.39, 1050, "down-small"))
        self.assertFalse(small["reminder"]["notices"])
        state, large = self.save(state, motion(0, -2.4, 1060, "down-large"))
        self.assertTrue(large["reminder"]["notices"])

    def test_first_downward_wave_covers_the_return_to_its_start(self):
        state, _ = self.save({}, motion(1, .5))
        state, saved = self.save(state, motion(.5, 1, 1020, "return"))
        self.assertFalse(saved["reminder"]["notices"])

    def test_spike_retrace_restart_keeps_evidence_without_phase_tickets(self):
        state, first = self.save({}, motion(0, 1))
        for phase in ("retraced", "settled", "data_gap"):
            state, saved = self.save(state, {**first, "state": phase, "current": .1, "current_at": 1010, "version": 5}, first)
            self.assertEqual(saved["reminder"], first["reminder"])
        state, restart = self.save(state, motion(.1, .65, 1020, "restart"))
        self.assertFalse(restart["reminder"]["notices"])
        state, renewed = self.save(state, motion(.1, 1.2, 1030, "renewed"))
        self.assertTrue(renewed["reminder"]["notices"])

    def test_same_pulse_can_break_again_without_new_episode(self):
        state, first = self.save({}, motion(0, 1))
        next_pulse = {**first, "peak": 1.2, "peak_at": 1002, "peak_context": context(1.2), "version": 4}
        state, second = self.save(state, next_pulse, first)
        self.assertEqual(reminder_count(second, 1003), 2)
        state2, duplicate = self.save(state, next_pulse, second)
        self.assertEqual(state2, state)
        self.assertEqual(duplicate["reminder"], second["reminder"])

    def test_path_strategy_coin_metric_and_leg_order_have_distinct_persistent_keys(self):
        base = motion(0, 1)
        variants = [base, {**base, "metric": "basis"}]
        for path in (("futures_futures", "QUALITY/USDT", "Gate", "Binance"),
                     ("spot_futures", "QUALITY/USDT", "Bitget", "Binance"),
                     ("spot_futures", "OTHER/USDT", "Gate", "Binance"),
                     ("spot_futures", "QUALITY/USDT", "Binance", "Gate")):
            variants.append({**base, "path": path})
        self.assertEqual(len({reminder_state_id(p) for p in variants}), 6)
        state, _ = self.save({}, base)
        with self.assertRaises(ValueError):
            self.save(state, variants[1])

    def test_json_restart_and_late_stronger_peak_do_not_rewind_or_erase_memory(self):
        state, first = self.save({}, motion(0, 1))
        state = json.loads(json.dumps(state))
        state, repeated = self.save(state, motion(0, 1, 1020, "after-restart"))
        self.assertFalse(repeated["reminder"]["notices"])
        state, old = self.save(state, motion(0, 3, 900, "late"))
        self.assertTrue(old["reminder"]["notices"])
        self.assertEqual(state["last_peak_at"], 1021)
        state, repeat = self.save(state, motion(0, 2, 1100, "repeat"))
        self.assertFalse(repeat["reminder"]["notices"])

    def test_window_expiry_requires_a_new_qualified_motion_not_a_static_or_old_pulse(self):
        state, first = self.save({}, motion(0, 1))
        state, boundary = self.save(state, motion(.5, 1, 1000+WINDOW, "at-boundary"))
        self.assertFalse(boundary["reminder"]["notices"])
        static = {**first, "current_at": 1001+WINDOW, "version": 4}
        state, saved = self.save(state, static, first)
        self.assertEqual(len(saved["reminder"]["notices"]), 1)
        self.assertIsNone(current_notice(saved, 1001+WINDOW))
        tiny = {**first, "peak": 1.0001, "peak_at": 1002+WINDOW, "version": 5}
        state, saved = self.save(state, tiny, first)
        self.assertEqual(len(saved["reminder"]["notices"]), 1)
        state, fresh = self.save(state, motion(.5, 1, 1100+WINDOW, "fresh"))
        self.assertEqual(len(fresh["reminder"]["notices"]), 1)

    def test_early_half_percent_spike_survives_retracement_before_first_persistence(self):
        pulse = motion(.08, .5)
        pulse.update(state="retraced", current=.1, current_at=1004)
        _, saved = self.save({}, pulse)
        self.assertIsNotNone(current_notice(saved, 1005))
        self.assertEqual(saved["peak"], .5)

    def test_noise_isolation_and_seeded_history_cannot_make_live_notifications(self):
        _, small = self.save({}, motion(0, .2))
        _, isolated = self.save({}, {**motion(0, 10), "state": "isolated"})
        self.assertFalse(small["reminder"]["notices"])
        self.assertFalse(isolated["reminder"]["notices"])
        state, seeded = self.save({}, motion(0, 1), seeded=True)
        self.assertIsNone(current_notice(seeded, 1002))
        self.assertEqual(reminder_count(seeded, 1002), 1)
        _, repeated = self.save(state, motion(0, 1, 1005, "new"))
        self.assertFalse(repeated["reminder"]["notices"])

    def test_price_threshold_is_independent_of_absolute_token_price(self):
        for offset in (-1000, 0, 1000):
            state, _ = self.save({}, motion(offset, offset+3, metric="price"))
            state, small = self.save(state, motion(offset, offset+3.59, 1020, "small", "price"))
            self.assertFalse(small["reminder"]["notices"])
            state, exact = self.save(state, motion(offset, offset+3.6, 1030, "exact", "price"))
            self.assertTrue(exact["reminder"]["notices"])

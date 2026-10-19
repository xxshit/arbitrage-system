import json
import unittest

from funding_alerts import advance, binance_observations, event_is_active, status


def observation(at=1000, **kwargs):
    return dict(dict(exchange="Binance", contract="DEMOUSDT", symbol="DEMO/USDT", rate=.3,
                     cap=1, floor=-1, interval=4, next_at=1200, rate_at=at, info_at=at, observed_at=at), **kwargs)


class FundingRuleTests(unittest.TestCase):
    def test_initial_hourly_is_state_not_a_fabricated_transition(self):
        state, events = advance({}, observation(interval=1))
        self.assertEqual(events, [])
        self.assertEqual(status(state, 1001), "hourly")

    def test_initial_cap_is_visible_but_silent(self):
        state, events = advance({}, observation(rate=1))
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["notice"])
        self.assertFalse(event_is_active(events[0], state, 1001))
        self.assertEqual(status(state, 1001), "cap_touch")

    def test_positive_and_negative_touch_do_not_infer_one_hour(self):
        for value, side in ((1, "positive"), (-1, "negative")):
            with self.subTest(value=value):
                state, _ = advance({}, observation())
                state, events = advance(state, observation(1010, rate=value))
                self.assertEqual([(e["kind"], e["side"], e["notice"]) for e in events], [("cap_touch", side, True)])
                self.assertEqual(state["interval"], 4)
                self.assertTrue(event_is_active(events[0], state, 1015))

    def test_touch_then_recede_then_settlement_below_limit_does_not_change_interval(self):
        state, _ = advance({}, observation(1100))
        state, touch = advance(state, observation(1110, rate=1))
        state, _ = advance(state, observation(1195, rate=.8))
        self.assertFalse(event_is_active(touch[0], state, 1195))
        state, events = advance(state, observation(1205, rate=.2, next_at=15600), [{"at": 1200, "rate": .8}])
        self.assertEqual([e["kind"] for e in events], ["settled_below_cap"])
        self.assertEqual(status(state, 1205), "normal")
        self.assertEqual(state["interval"], 4)

    def test_actual_cap_can_wait_for_delayed_official_interval(self):
        state, _ = advance({}, observation(1195, rate=1))
        state, events = advance(state, observation(1205, rate=.1, next_at=15600), [{"at": 1200.005, "rate": 1}])
        self.assertEqual(events[0]["kind"], "settled_cap")
        self.assertFalse(events[0]["notice"])
        self.assertEqual(status(state, 1205), "settled_cap")
        for at in range(1210, 2100, 30):
            state, _ = advance(state, observation(at, next_at=15600))
        state, events = advance(state, observation(2100, interval=1, next_at=4800))
        self.assertEqual([e["kind"] for e in events], ["interval_shortened"])
        self.assertTrue(events[0]["notice"])
        self.assertEqual((events[0]["from_interval"], events[0]["to_interval"]), (4, 1))
        self.assertTrue(event_is_active(events[0], state, 2100))

    def test_pending_is_not_actual_settlement_proof(self):
        state, _ = advance({}, observation(1195, rate=1))
        state, events = advance(state, observation(1205, next_at=15600))
        self.assertFalse(events)
        self.assertEqual(status(state, 1205), "settling")
        state, _ = advance(state, observation(1210, next_at=15600), [{"at": 1190, "rate": 1}])
        self.assertEqual(status(state, 1210), "settled_cap")

    def test_future_and_wrong_cycle_settlements_are_not_used(self):
        state, _ = advance({}, observation(1195, rate=1))
        for stamp in (1000, 1300):
            new, events = advance(state, observation(1205, next_at=15600), [{"at": stamp, "rate": 1}])
            self.assertFalse(events)
            self.assertEqual(status(new, 1205), "settling")

    def test_cap_comparison_uses_pre_settlement_limits_not_new_limits(self):
        state, _ = advance({}, observation(1195, rate=1))
        state, events = advance(state, observation(1205, cap=2, next_at=15600), [{"at": 1200, "rate": 1}])
        self.assertEqual(events[0]["kind"], "settled_cap")
        self.assertEqual(events[0]["cap"], 1)

    def test_unknown_cap_does_not_fabricate_touch_but_interval_changes_still_work(self):
        state, _ = advance({}, observation(cap=None, floor=None))
        state, events = advance(state, observation(1010, rate=2, cap=None, floor=None))
        self.assertFalse(events)
        state, events = advance(state, observation(1020, rate=2, cap=None, floor=None, interval=1))
        self.assertEqual(events[0]["kind"], "interval_shortened")

    def test_eight_hour_to_one_hour_and_reversion_are_distinct(self):
        state, _ = advance({}, observation(interval=8))
        state, events = advance(state, observation(1010, interval=1))
        self.assertEqual(events[0]["from_interval"], 8)
        state, events = advance(state, observation(1020, interval=4))
        self.assertEqual(events[0]["kind"], "interval_changed")
        self.assertFalse(events[0]["notice"])

    def test_repeat_cycles_record_but_do_not_repeat_same_cap_reminder_in_24h(self):
        state, _ = advance({}, observation())
        state, first = advance(state, observation(1001, rate=1))
        self.assertTrue(first[0]["notice"])
        for n in range(1, 20):
            state, events = advance(state, observation(1001+n, rate=1, next_at=1200+n))
            self.assertEqual(len(events), 1)
            self.assertFalse(events[0]["notice"])

    def test_no_repeat_after_retracement_or_serialized_restart(self):
        state, _ = advance({}, observation())
        state, first = advance(state, observation(1010, rate=1))
        for n in range(100):
            state, events = advance(json.loads(json.dumps(state)), observation(1011+n, rate=1 if n % 2 else .5))
            self.assertFalse(events)
        self.assertTrue(first[0]["notice"])

    def test_opposite_cap_and_changed_limit_have_independent_reminders(self):
        state, _ = advance({}, observation())
        state, _ = advance(state, observation(1010, rate=1))
        state, events = advance(state, observation(1020, rate=-1))
        self.assertTrue(events[0]["notice"])
        state, events = advance(state, observation(1030, rate=2, cap=2))
        self.assertTrue(events[0]["notice"])

    def test_stale_and_settlement_crossing_do_not_remain_active(self):
        state, _ = advance({}, observation())
        state, events = advance(state, observation(1195, rate=1))
        self.assertFalse(event_is_active(events[0], state, 1200))
        self.assertEqual(status(state, 1200), "settling")
        self.assertEqual(status(state, 1300), "stale")

    def test_long_gap_interval_change_is_recorded_without_a_new_sound(self):
        state, _ = advance({}, observation())
        state, events = advance(state, observation(2000, interval=1, next_at=4800))
        self.assertTrue(events[0]["after_gap"])
        self.assertFalse(events[0]["notice"])

    def test_duplicate_and_out_of_order_observations_are_idempotent(self):
        state, _ = advance({}, observation())
        state, _ = advance(state, observation(1010, interval=1))
        for obs in (observation(1010, interval=1), observation(1005), observation(1020, info_at=1000)):
            new, events = advance(state, obs)
            self.assertEqual(state, new)
            self.assertFalse(events)
        newer, events = advance(state, observation(1020, rate=1, interval=1, info_at=1009.99999))
        self.assertEqual(newer["rate"], 1)
        self.assertEqual(events[0]["kind"], "cap_touch")

    def test_24h_expiry_rearms_only_on_another_cycle_touch(self):
        state, _ = advance({}, observation())
        state, _ = advance(state, observation(1010, rate=1))
        state, _ = advance(state, observation(1010+86400-5, rate=.5, next_at=90000))
        state, events = advance(state, observation(1010+86400, rate=1, next_at=90000))
        self.assertTrue(events[0]["notice"])


class FundingInputTests(unittest.TestCase):
    def inputs(self, **updates):
        values = dict(instruments=[dict(symbol="DEMOUSDT", status="TRADING", contractType="PERPETUAL", quoteAsset="USDT")],
                      rates=[dict(symbol="DEMOUSDT", lastFundingRate="0.01", nextFundingTime=1200000, time=1000000)],
                      info=[dict(symbol="DEMOUSDT", adjustedFundingRateCap="0.01", adjustedFundingRateFloor="-0.01", fundingIntervalHours=4)], rate_at=1000, info_at=1000, now=1000)
        values.update(updates)
        return binance_observations(**values)

    def test_decimal_api_values_become_percentage_points_once(self):
        item = self.inputs()[0]
        self.assertEqual((item["rate"], item["cap"], item["floor"], item["interval"]), (1, 1, -1, 4))

    def test_absent_override_does_not_invent_cap(self):
        item = self.inputs(info=[])[0]
        self.assertIsNone(item["cap"])
        self.assertIsNone(item["floor"])
        self.assertEqual(item["interval"], 8)

    def test_stale_future_or_missing_source_data_are_rejected(self):
        for updates in (dict(rate_at=800), dict(info_at=800), dict(rate_at=1001), dict(info_at=None), dict(info={"code": -1}), dict(rates={})):
            self.assertFalse(self.inputs(**updates))

    def test_bad_metadata_cannot_manufacture_a_mass_default_change(self):
        for info in ([{}], [{"symbol": "DEMOUSDT", "fundingIntervalHours": "NaN"}], [{"symbol": "DEMOUSDT", "fundingIntervalHours": 0}]):
            self.assertFalse(self.inputs(info=info))

    def test_old_exchange_tick_and_countdown_are_not_current_cycle_proof(self):
        for extra in (dict(time=1), dict(time=None), dict(nextFundingTime=999999), dict(lastFundingRate="NaN")):
            tick = dict(symbol="DEMOUSDT", lastFundingRate="0.01", nextFundingTime=1200000, time=1000000)
            tick.update(extra)
            self.assertFalse(self.inputs(rates=[tick]))

    def test_delisted_and_delivery_contracts_are_not_observed(self):
        self.assertFalse(self.inputs(instruments=[]))
        for field, value in (("status", "SETTLING"), ("contractType", "CURRENT_QUARTER"), ("quoteAsset", "USDC")):
            item = dict(symbol="DEMOUSDT", status="TRADING", contractType="PERPETUAL", quoteAsset="USDT")
            item[field] = value
            self.assertFalse(self.inputs(instruments=[item]))

import math
import unittest
from copy import deepcopy

from opportunity_pool import baseline, classify, funding_profile, quote_metrics

NOW = 1792540800


def quotes(spread=.5, funding=.005, interval=4, **kwargs):
    return dict(long=dict(bid=99.99, ask=100, bid_qty=100, ask_qty=100),
                short=dict(bid=100+spread, ask=100.01+spread, bid_qty=100, ask_qty=100,
                           funding=funding, interval=interval, funding_at=NOW),
                long_at=NOW, short_at=NOW, metadata_at=NOW,
                transfer_state="normal", **kwargs)


def history(**kwargs):
    return dict(dict(computed_at=NOW, ready=True, close_ready=True, range=.05,
                     max_abs=.53, low=.48, high=.53, median=.50,
                     exit_q75=.55, first_at=NOW-86400, last_at=NOW), **kwargs)


def funding(rate=.005, interval=4):
    return funding_profile([(NOW-i*interval*3600, rate) for i in range(12)], NOW, interval)


class OpportunityTests(unittest.TestCase):
    def test_scenario_is_not_available_above_known_first_level_capacity(self):
        self.assertTrue(self.candidate()["scenario"]["calculable"])
        self.assertFalse(self.candidate(notional=5001)["scenario"]["calculable"])
        live = quotes(); live["long"]["bid_qty"] = None
        self.assertFalse(self.candidate(live=live)["scenario"]["calculable"])
        live = quotes(); live["short"]["funding_at"] = NOW-601
        self.assertFalse(self.candidate(live=live)["scenario"]["calculable"])
        self.assertFalse(self.candidate(hist=history(close_ready=False))["scenario"]["calculable"])

    def test_adverse_funding_is_explicit_and_not_positive_income(self):
        scenario=self.candidate(live=quotes(funding=-.02))["scenario"]
        self.assertGreater(scenario["adverse_funding"],0)
        self.assertEqual(self.candidate()["scenario"]["adverse_funding"],0)

    def candidate(self, live=None, profile=None, hist=None, row=None, **kwargs):
        return classify(row or dict(open_spread=.5, close_spread=.55, funding_rate=.005,
                                    funding_interval_hours=4, change_24h=-2),
                        profile if profile is not None else funding(),
                        hist if hist is not None else history(),
                        live if live is not None else quotes(), NOW, **kwargs)

    def test_baseline_is_cadence_normalized_not_a_guaranteed_floor(self):
        self.assertAlmostEqual(baseline(4), .005)
        self.assertAlmostEqual(baseline(8), .01)
        self.assertAlmostEqual(baseline(1), .00125)
        for value in (None, 0, -1, True, math.nan, 48):
            self.assertIsNone(baseline(value))

    def test_static_half_percent_low_funding_is_retained_with_low_score(self):
        item = self.candidate()
        self.assertEqual(item["status"], "watch")
        self.assertLess(item["score"], 30)
        self.assertNotIn("grade", item)
        self.assertLess(item["scenario"]["net"], 0)
        self.assertTrue(any("常态溢价" in reason for reason in item["reasons"]))

    def test_new_funding_improvement_survives_all_previous_low_insurance(self):
        item = self.candidate(live=quotes(.05, funding=.02))
        self.assertEqual(item["status"], "watch")
        self.assertGreater(item["score"], self.candidate(live=quotes(.05, funding=.006))["score"])
        self.assertEqual(item["funding"]["above"], 0)
        self.assertIn("资费", item["drivers"])

    def test_slow_falling_positive_funding_is_not_rejected(self):
        item = self.candidate(live=quotes(.05, funding=.03), profile=funding(.03))
        self.assertEqual(item["trend"], "回撤正资费观察")
        self.assertIn("资费", item["drivers"])

    def test_rising_trend_is_supporting_evidence_not_a_gate(self):
        down = self.candidate(row=dict(change_24h=-3), live=quotes(.1, funding=.03))
        up = self.candidate(row=dict(change_24h=3), live=quotes(.1, funding=.03))
        self.assertEqual(down["score"], up["score"])
        self.assertEqual(up["trend"], "价格走强")

    def test_flat_market_with_no_funding_driver_is_not_a_candidate(self):
        self.assertIsNone(self.candidate(live=quotes(.05)))

    def test_negative_spread_activity_is_watch_never_abs_profit(self):
        item = self.candidate(live=quotes(-1.2), hist=history(range=1.5, max_abs=2, low=-2, high=-.5))
        self.assertEqual(item["status"], "watch")
        self.assertLess(item["scenario"]["spread_net"], 0)
        self.assertGreater(item["score"], 20)

    def test_current_opportunity_requires_exit_and_four_fees(self):
        item = self.candidate(live=quotes(1.5), hist=history(range=1.4, max_abs=1.5, exit_q75=.1))
        self.assertEqual(item["status"], "opportunity")
        self.assertGreater(item["score"], self.candidate()["score"])
        self.assertGreater(item["scenario"]["fee_budget"], .6)
        self.assertAlmostEqual(item["scenario"]["spread_net"],
                               1.5-.11-item["scenario"]["fee_budget"]-.1)

    def test_spread_only_opportunity_does_not_require_positive_funding_history(self):
        item = self.candidate(live=quotes(1.5, funding=.02), profile=funding_profile([], NOW, 4),
                              hist=history(exit_q75=.1))
        self.assertEqual(item["status"], "opportunity")
        self.assertIsNone(item["scenario"]["funding_income"])
        self.assertTrue(item["notes"])

    def test_historical_negative_exit_discount_is_not_credited(self):
        item = self.candidate(live=quotes(1.5), hist=history(exit_q75=-2))
        self.assertEqual(item["scenario"]["exit_spread"], 0)
        self.assertLess(item["scenario"]["net"], 1.5)

    def test_large_current_funding_is_capped_at_settled_history(self):
        item = self.candidate(live=quotes(.5, funding=2))
        self.assertAlmostEqual(item["scenario"]["funding_income"], .03*3*.5)
        self.assertEqual(item["status"], "watch")

    def test_funding_can_be_the_main_driver_of_current_space(self):
        item = self.candidate(live=quotes(.5, funding=.5), profile=funding(.5))
        self.assertLess(item["scenario"]["spread_net"], 0)
        self.assertEqual(item["status"], "opportunity")
        self.assertGreater(item["score"], 70)

    def test_incomplete_funding_never_credits_forecast_income(self):
        item = self.candidate(live=quotes(.5, funding=.5),
                              profile=funding_profile([(NOW, .5)], NOW, 4))
        self.assertIsNone(item["scenario"]["funding_net"])
        self.assertEqual(item["status"], "watch")

    def test_negative_funding_is_cost_and_holding_time_matters(self):
        one = self.candidate(live=quotes(1.5, funding=-.1), hold_hours=24)
        week = self.candidate(live=quotes(1.5, funding=-.1), hold_hours=168)
        self.assertLess(week["scenario"]["net"], one["scenario"]["net"])
        self.assertEqual(week["status"], "watch")

    def test_one_venue_divergence_does_not_exclude_the_other_venue(self):
        bad = self.candidate(row=dict(price_divergence={"state": "confirmed"}))
        good = self.candidate(row=dict(price_divergence=None))
        self.assertIsNone(bad)
        self.assertIsNotNone(good)

    def test_listing_risk_is_watch_not_a_current_opportunity(self):
        item = self.candidate(live=quotes(2), row=dict(listing_review=True))
        self.assertEqual(item["status"], "watch")
        self.assertIn("下架", " ".join(item["risks"]))

    def test_unknown_or_closed_transfers_cannot_be_current(self):
        for value in ("unknown", "restricted", None):
            live=quotes(2)
            live["transfer_state"]=value
            item=self.candidate(live=live)
            self.assertEqual(item["status"], "watch")
            self.assertIn("充提", " ".join(item["risks"]))
            self.assertLess(item["score"], self.candidate(live=quotes(2))["score"])

    def test_bad_or_stale_quotes_are_never_live(self):
        for leg, field, value in (
            ("long","bid",101), ("long","ask",0), ("short","bid",math.nan),
            ("short","ask",True),
        ):
            live=quotes(2);live[leg][field]=value
            self.assertIsNone(quote_metrics(live, NOW))
        for field, value in (("long_at",NOW-5),("short_at",NOW+3),("short_at",NOW-3)):
            live=quotes(2);live[field]=value
            self.assertIsNone(quote_metrics(live, NOW))

    def test_capacity_is_half_the_minimum_same_unit_quantity(self):
        live=quotes(2);live["short"]["ask_qty"]=15
        item=self.candidate(live=live)
        self.assertEqual(item["capacity_usdt"],750)
        self.assertEqual(item["status"],"watch")
        self.assertEqual(self.candidate(live=live,notional=500)["status"],"opportunity")

    def test_unknown_quantity_and_stale_metadata_are_fail_closed(self):
        for changes in ("qty","meta","fund"):
            live=quotes(2)
            if changes=="qty":live["short"]["bid_qty"]=None
            if changes=="meta":live["metadata_at"]=NOW-61
            if changes=="fund":live["short"]["funding_at"]=NOW-601
            self.assertEqual(self.candidate(live=live)["status"],"watch")

    def test_close_history_missing_stale_or_not_ready_cannot_be_current(self):
        for hist in ({},history(close_ready=False),history(computed_at=NOW-361),history(ready=False)):
            self.assertEqual(self.candidate(live=quotes(2),hist=hist)["status"],"watch")

    def test_settlement_signed_total_dedup_and_unknown_cadence(self):
        profile=funding_profile([(NOW-3600,.1),(NOW-3600,.1),(NOW,-.2)],NOW,1)
        self.assertAlmostEqual(profile["total"],-.1)
        self.assertEqual(profile["count"],2)
        profile=funding_profile([(NOW-3*3600,.01),(NOW,.02)],NOW,4)
        self.assertEqual(profile["unknown_count"],2)
        self.assertFalse(profile["complete"])

    def test_missing_or_future_settlements_do_not_claim_complete(self):
        events=[(NOW-i*4*3600,.005) for i in range(12)]
        self.assertTrue(funding_profile(events,NOW,4)["complete"])
        events.pop(3)
        self.assertFalse(funding_profile(events,NOW,4)["complete"])
        profile=funding_profile([(NOW+1,99),(NOW,math.nan)],NOW,4)
        self.assertIsNone(profile["total"])

    def test_cadence_switch_is_inferred_per_settlement_not_current_only(self):
        events=[(NOW-8*3600,.01),(NOW-4*3600,.005),(NOW,.005)]
        result=funding_profile(events,NOW,1)
        self.assertEqual(result["above"],0)
        self.assertFalse(result["complete"])

    def test_inputs_are_never_mutated(self):
        args=[dict(change_24h=3),funding(),history(),quotes()]
        before=deepcopy(args)
        classify(*args,NOW)
        self.assertEqual(args,before)

    def test_five_day_old_opening_survives_flat_recent_day_and_low_current_gap(self):
        older = dict(range=2.2,max_abs=1.3,low=-.9,high=1.3,peak_at=NOW-5*86400,
                     first_at=NOW-6*86400,last_at=NOW,bins=5000)
        hist = history(windows={"24h":dict(range=.05,max_abs=.1),"15d":older})
        item = self.candidate(live=quotes(.05),hist=hist)
        self.assertEqual(item["status"],"watch")
        self.assertGreater(item["score"],30)
        self.assertIn("价差",item["drivers"])

    def test_episode_summary_survives_raw_retention_and_fades_gradually(self):
        activity = dict(count=3,returned=2,peak=1.5,peak_at=NOW-20*86400,last_at=NOW-20*86400)
        item = self.candidate(live=quotes(.05),hist={},activity=activity)
        self.assertIsNotNone(item)
        self.assertEqual(item["status"],"watch")
        older = self.candidate(live=quotes(.05),hist={},activity={**activity,"last_at":NOW-80*86400})
        self.assertLess(older["score"],item["score"])

    def test_scores_are_continuous_evidence_not_three_grade_mappings(self):
        scores = {self.candidate(live=quotes(.5, funding=rate),profile=funding(rate))["score"]
                  for rate in (.006,.01,.02,.03,.05,.1,.3,.5)}
        self.assertGreater(len(scores),5)
        self.assertTrue(all(0 <= score <= 100 for score in scores))


if __name__=="__main__":
    unittest.main()

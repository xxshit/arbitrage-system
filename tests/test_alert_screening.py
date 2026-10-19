import unittest
from copy import deepcopy

from alert_screening import NoiseWindow, okx_contract_unit, pulse_has_watch_peak, screen_pulse
from anomaly_engine import PulseDetector
from anomaly_streams import AlertStreamHub, Route


PATH = ("spot_futures", "DEMO/USDT", "Gate", "Binance")


def reference():
    profile = dict(open=0, exit=.04, floor=.4, noise=.01, width=.04)
    return dict(ready=True, samples=20, from_at=800, to_at=900, forward=dict(profile), reverse=dict(profile))


def pulse(**overrides):
    return dict(dict(path=PATH, metric="spread", direction=1, state="watch", baseline=0, current_at=1000,
                     peak=1.5, peak_close=1.54, screen_reference=reference()), **overrides)


def quotes(spread=1.5, quantity=100, **overrides):
    return dict(dict(long=dict(bid=99.99, ask=100, bid_qty=quantity, ask_qty=quantity, funding=.01, interval=8, funding_at=overrides.get("metadata_at", 1000)),
                     short=dict(bid=100+spread, ask=100.01+spread, bid_qty=quantity, ask_qty=quantity, funding=.01, interval=8, funding_at=overrides.get("metadata_at", 1000)),
                     long_at=1000, short_at=1000, metadata_at=1000, transfer_state="normal"), **overrides)


class ScreeningTests(unittest.TestCase):
    def test_half_percent_is_watch_but_not_a_net_cost_candidate(self):
        result = screen_pulse(pulse(peak=.5), quotes(.5), 1000)
        self.assertEqual(result["tier"], "watch")
        self.assertLess(result["net_edge"], .30)

    def test_four_fees_adverse_funding_and_exit_assumption_are_explicit(self):
        live = quotes()
        live["short"].update(funding=-.2, interval=4)
        result = screen_pulse(pulse(), live, 1000)
        self.assertEqual(result["tier"], "watch")
        self.assertGreater(result["fee_budget"], .6)
        self.assertAlmostEqual(result["exit_assumption"], .044)
        self.assertAlmostEqual(result["funding_budget"], .6*1.0151*1.1)
        self.assertAlmostEqual(result["net_edge"], result["entry_spread"]-result["exit_assumption"]-result["fee_budget"]-.1-result["funding_budget"])

    def test_large_positive_edge_can_be_candidate_with_no_profits_from_positive_funding(self):
        live = quotes()
        live["short"]["funding"] = 99
        result = screen_pulse(pulse(), live, 1000)
        self.assertEqual(result["tier"], "candidate")
        self.assertEqual(result["funding_budget"], 0)
        self.assertGreaterEqual(result["net_edge"], .3)

    def test_negative_gap_and_ensolike_narrowing_are_record_only(self):
        for spread in (-.54, -.28, .08):
            self.assertEqual(screen_pulse(pulse(), quotes(spread), 1000)["tier"], "record")
        self.assertEqual(screen_pulse(pulse(direction=-1), quotes(-1.5), 1000)["tier"], "record")

    def test_joint_price_and_basis_motion_do_not_manufacture_hedge_entries(self):
        for metric in ("basis", "price"):
            self.assertEqual(screen_pulse(pulse(metric=metric), quotes(), 1000)["tier"], "record")

    def test_noise_and_static_premium_cannot_be_promoted(self):
        event = pulse()
        event["screen_reference"]["forward"]["floor"] = 2
        self.assertEqual(screen_pulse(event, quotes(), 1000)["tier"], "record")
        event["baseline"] = 1.4
        event["screen_reference"]["forward"].update(open=1.4, floor=.4)
        self.assertEqual(screen_pulse(event, quotes(), 1000)["tier"], "record")

    def test_slow_near_zero_ramp_is_not_absorbed_by_moving_median(self):
        event = pulse(baseline=.08, peak=.5)
        event["screen_reference"]["forward"]["open"] = .3
        self.assertEqual(screen_pulse(event, quotes(.5), 1000)["tier"], "watch")
        self.assertTrue(pulse_has_watch_peak(event))
        event["screen_reference"]["forward"]["floor"] = 1
        self.assertEqual(screen_pulse(event, quotes(.5), 1000)["tier"], "record")

    def test_cold_start_cannot_guess_noise_or_exit_convergence(self):
        for ref in ({}, {"ready": False}, {"ready": True}):
            result = screen_pulse(pulse(screen_reference=ref), quotes(), 1000)
            self.assertEqual(result["tier"], "record")
            self.assertIn(result["reason"], ("基线积累中", "历史未记录筛选基线"))
        event = pulse(); event["screen_reference"]["to_at"] = 300
        self.assertEqual(screen_pulse(event, quotes(), 1000)["reason"], "本轮参考基线已过期")

    def test_missing_closed_or_stale_transfer_never_passes_as_normal(self):
        for status in ("unknown", "restricted", "stale", None):
            result = screen_pulse(pulse(), quotes(transfer_state=status), 1000)
            self.assertEqual(result["tier"], "watch")
            self.assertIn("充提", result["reason"])

    def test_stale_or_misaligned_books_remove_current_status(self):
        for changes in (dict(long_at=995), dict(short_at=1003), dict(short_at=997), dict(long_at=float("nan"))):
            self.assertEqual(screen_pulse(pulse(), quotes(**changes), 1000)["live"], False)
        self.assertEqual(screen_pulse(pulse(), None, 1000)["tier"], "record")

    def test_funding_missing_or_metadata_stale_blocks_candidate(self):
        for field, value in (("funding", None), ("funding", float("nan")), ("interval", 0), ("interval", None), ("funding_at", None), ("funding_at", 300)):
            live = quotes(); live["short"][field] = value
            self.assertEqual(screen_pulse(pulse(), live, 1000)["tier"], "watch")
        self.assertEqual(screen_pulse(pulse(), quotes(metadata_at=900), 1000)["tier"], "watch")

    def test_depth_must_cover_equal_base_quantity_and_both_exit_sides(self):
        live = quotes(quantity=20)
        self.assertEqual(screen_pulse(pulse(), live, 1000)["tier"], "candidate")
        # 19.9 base units at the dearer leg may exceed 2,000 USDT, but cannot
        # cover a 10-unit hedge at our 50% capacity allowance.
        live["short"]["bid_qty"] = 19.9
        self.assertEqual(screen_pulse(pulse(), live, 1000)["tier"], "watch")
        live = quotes(); live["short"]["ask_qty"] = 1
        self.assertEqual(screen_pulse(pulse(), live, 1000)["tier"], "watch")

    def test_amount_changes_estimate_only_and_missing_quantity_fails_closed(self):
        live = quotes(quantity=20); original = deepcopy(live)
        self.assertEqual(screen_pulse(pulse(), live, 1000, 2000)["tier"], "watch")
        self.assertEqual(screen_pulse(pulse(), live, 1000, 500)["tier"], "candidate")
        self.assertEqual(live, original)
        live["long"]["ask_qty"] = None
        self.assertEqual(screen_pulse(pulse(), live, 1000)["reason"], "数量单位或盘口深度未知")

    def test_futures_reversal_recomputes_ask_bid_fees_and_funding_not_absolute_gap(self):
        event = pulse(path=("futures_futures", "DEMO/USDT", "Bybit", "Binance"), direction=-1)
        live = quotes(-2, transfer_state="unknown")
        live["long"]["funding"] = -.2
        live["short"]["funding"] = .1
        result = screen_pulse(event, live, 1000)
        self.assertEqual((result["long_exchange"], result["short_exchange"]), ("Binance", "Bybit"))
        self.assertAlmostEqual(result["entry_spread"], 100*(99.99/98.01-1))
        self.assertNotEqual(result["entry_spread"], 2)
        self.assertGreater(result["funding_budget"], .3)

    def test_candidate_disappears_as_soon_as_live_gap_is_gone_not_after_120_seconds(self):
        event = pulse(state="retraced")
        self.assertEqual(screen_pulse(event, quotes(.1), 1000)["tier"], "record")
        self.assertTrue(pulse_has_watch_peak(event))
        for state in ("data_gap", "isolated", "settled"):
            self.assertEqual(screen_pulse(pulse(state=state), quotes(), 1000)["live"], False)
        self.assertFalse(screen_pulse(pulse(current_at=980), quotes(), 1000)["live"])

    def test_reverse_peak_uses_original_closing_side(self):
        event = pulse(path=("futures_futures", "DEMO/USDT", "Bybit", "Binance"), direction=-1, peak=-1, peak_close=-.1)
        self.assertFalse(pulse_has_watch_peak(event))
        event["peak_close"] = -.8
        self.assertTrue(pulse_has_watch_peak(event))

    def test_okx_contract_size_is_not_a_coin_count(self):
        data = dict(instId="BTC-USDT-SWAP", ctType="linear", ctVal="0.01", ctValCcy="BTC", settleCcy="USDT")
        self.assertEqual(okx_contract_unit(data), .01)
        for changes in (dict(ctVal=""), dict(ctVal="NaN"), dict(ctValCcy="USD"), dict(ctType="inverse"), dict(settleCcy="BTC")):
            self.assertIsNone(okx_contract_unit({**data, **changes}))

    def test_profile_warmup_excludes_trigger_and_adapts_to_ordinary_oscillation(self):
        quiet, noisy = NoiseWindow(), NoiseWindow()
        for i in range(19):
            q = quiet.observe(1000+i*5, dict(open_spread=0, close_spread=.04))
            value = .6 if i % 2 else 0
            n = noisy.observe(1000+i*5, dict(open_spread=value, close_spread=value+.04))
            if i < 14:
                self.assertFalse(q.get("ready"))
        self.assertTrue(q["ready"])
        self.assertEqual(q["forward"]["floor"], .4)
        self.assertAlmostEqual(n["forward"]["floor"], 1.8)
        triggered = quiet.observe(1095, dict(open_spread=5, close_spread=5.04))
        self.assertEqual(triggered["forward"], q["forward"])
        self.assertFalse(quiet.observe(1110, dict(open_spread=5, close_spread=5.04))["ready"])

    def test_engine_freezes_reference_across_retrace_and_restart_below_old_high(self):
        engine, updates = PulseDetector(), {}
        ref = reference()
        for at, value in enumerate((0, 1.5, .1, .8)):
            ctx = dict(open_spread=value, close_spread=value+.04, mid_spread=value+.02, screen_reference=ref)
            for event in engine.observe(PATH, "spread", value, 1000+at, ctx):
                updates[event["id"]] = event
            ref = {"ready": False}
        self.assertEqual(len(updates), 2)
        for event in updates.values():
            # The first event triggered with cold reference, so later changes
            # cannot falsely upgrade its pre-event evidence.
            self.assertFalse(event["screen_reference"]["ready"])
            self.assertNotIn("screen_reference", event["current_context"])

    def test_trigger_noise_cannot_raise_its_own_frozen_threshold(self):
        engine, updates = PulseDetector(), {}
        ref = reference()
        for at, value in enumerate((0, 1.5, .1, .8)):
            ctx = dict(open_spread=value, close_spread=value+.04, mid_spread=value+.02, screen_reference=ref)
            for event in engine.observe(PATH, "spread", value, 1000+at, ctx):
                updates[event["id"]] = event
            if at == 1:
                ref["forward"]["floor"] = 50
        self.assertEqual(len(updates), 2)
        self.assertTrue(all(event["screen_reference"]["forward"]["floor"] == .4 for event in updates.values()))

    def test_profile_storage_is_bounded_during_long_monitoring(self):
        window = NoiseWindow()
        for tick in range(3600):
            window.observe(1000+tick, dict(open_spread=tick%5*.01, close_spread=.1+tick%5*.01))
        self.assertLessEqual(len(window.samples), 61)

    def test_hub_refreshes_quantity_without_resetting_episode_or_metadata(self):
        hub = AlertStreamHub()
        route = Route(PATH, ("gate", "DEMO_USDT"), ("bn_future", "DEMOUSDT"), metadata_at=1000)
        hub.register("spot_futures", [route], 1000)
        def book(symbol, bid, ask, at=1000, qty=100):
            return dict(symbol=symbol, bid=bid, ask=ask, bid_qty=qty, ask_qty=qty, at=at)
        hub.ingest("gate", book("DEMO_USDT", 99.99, 100), 1000)
        hub.ingest("bn_future", book("DEMOUSDT", 101.5, 101.51), 1000)
        hub.ingest("gate", book("DEMO_USDT", 99.99, 100, 1000.1, 1), 1000.1)
        live = hub.screening_quotes([PATH], 1000.1)[PATH]
        self.assertEqual(live["long"]["ask_qty"], 1)
        profile = hub.noise[PATH]
        hub.register("spot_futures", [Route(PATH, route.long_key, route.short_key, metadata_at=1001)], 1001)
        self.assertIs(hub.noise[PATH], profile)
        hub.invalidate([route.long_key], 1001)
        self.assertEqual(hub.screening_quotes([PATH], 1001), {})
        self.assertNotIn(PATH, hub.noise)

    def test_hub_converts_alias_and_okx_contract_quantities(self):
        hub = AlertStreamHub(); path = ("futures_futures", "X/USDT", "Bybit", "OKX")
        hub.register("futures_futures", [Route(path, ("bybit", "1000XUSDT"), ("okx", "X-USDT-SWAP"), long_factor=.001, short_qty_factor=10)], 1000)
        for feed, symbol, price, qty in (("bybit", "1000XUSDT", 1000, 2), ("okx", "X-USDT-SWAP", 1.02, 200)):
            hub.ingest(feed, dict(symbol=symbol, bid=price, ask=price*1.0001, bid_qty=qty, ask_qty=qty, at=1000), 1000)
        quote = hub.screening_quotes([path], 1000)[path]
        self.assertEqual(quote["long"]["bid_qty"], 2000)
        self.assertEqual(quote["short"]["bid_qty"], 2000)
        self.assertEqual(quote["long"]["bid"], 1)


if __name__ == "__main__":
    unittest.main()

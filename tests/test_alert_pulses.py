import json
import math
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "pulse-test")
os.environ.setdefault("CHAT_ENCRYPTION_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")

from anomaly_engine import PulseDetector
from anomaly_streams import AlertStreamHub, Route, parse_frame, subscription
import app as module

PATH = ("spot_futures", "DEMO/USDT", "Gate", "Binance")


def context(value, **kwargs):
    return dict(dict(open_spread=value, close_spread=value + .02, mid_spread=value + .01,
                     long_mid=100, short_mid=100 + value, basis=0.01, funding_rate=.01), **kwargs)


class PulseDetectorTests(unittest.TestCase):
    def setUp(self):
        self.engine = PulseDetector()

    def sample(self, second, value, metric="spread", **overrides):
        return self.engine.observe(PATH, metric, value, 1000 + second, context(value, **overrides))

    def test_first_valid_crossing_does_not_wait_for_three_observations(self):
        self.sample(0, .03)
        output = self.sample(.1, .25)
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["triggered_at"], 1000.1)
        self.assertEqual(output[0]["state"], "watch")

    def test_ten_second_spike_then_five_second_retrace_keeps_peak(self):
        self.sample(0, .03)
        self.sample(5, .30)
        self.sample(10, 1.03)
        pulse = self.sample(15, .1)[0]
        self.assertEqual(pulse["state"], "retraced")
        self.assertEqual((pulse["baseline"], pulse["peak"], pulse["current"]), (.03, 1.03, .1))
        self.assertEqual((pulse["peak_at"], pulse["retrace_at"]), (1010, 1015))
        self.assertEqual(pulse["ranges"]["open_spread"]["max"], 1.03)

    def test_subsecond_peak_and_retrace_are_preserved(self):
        self.sample(0, 0)
        first = self.sample(.01, 1)[0]
        last = self.sample(.02, .1)[0]
        self.assertEqual(first["id"], last["id"])
        self.assertEqual((last["peak"], last["state"]), (1, "retraced"))

    def test_restart_below_old_peak_is_new_pulse_not_deduplicated(self):
        self.sample(0, 0)
        first = self.sample(1, 1)[0]
        self.sample(2, .1)
        second = self.sample(3, .4)[-1]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["episode"], second["episode"])
        self.assertEqual((second["ordinal"], second["baseline"]), (2, .1))
        self.assertLess(second["peak"], first["peak"])

    def test_multiple_restarts_each_get_identity(self):
        self.sample(0, 0)
        ids = []
        for i in range(1, 9):
            ids.append(self.sample(i * 2 - 1, .6)[-1]["id"])
            self.sample(i * 2, .05)
        self.assertEqual(len(set(ids)), 8)
        self.assertEqual(self.engine.tracks[(PATH, "spread")].ordinal, 8)

    def test_positive_to_negative_equal_absolute_values_trigger(self):
        self.sample(0, .3)
        pulse = self.sample(1, -.3)[0]
        self.assertEqual(pulse["direction"], -1)
        self.assertAlmostEqual(pulse["peak"] - pulse["baseline"], -.6)

    def test_direct_reversal_records_opposite_leg_without_an_extra_tick(self):
        self.sample(0, 0)
        self.sample(1, .6)
        updates = self.sample(2, -.6)
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[-1]["direction"], -1)

    def test_negative_spike_then_restart(self):
        self.sample(0, 0)
        self.sample(1, -.8)
        self.sample(2, -.1)
        pulse = self.sample(3, -.4)[-1]
        self.assertEqual((pulse["ordinal"], pulse["direction"]), (2, -1))

    def test_gradual_ramp_uses_120_second_window(self):
        outputs = []
        for second in range(101):
            outputs += self.sample(second, second * .005)
        self.assertTrue(outputs)
        self.assertAlmostEqual(outputs[0]["baseline"], 0)
        self.assertEqual(outputs[0]["threshold"], .5)

    def test_frozen_baseline_survives_continuing_ramp(self):
        self.sample(0, 0)
        pulse = None
        for second in range(1, 181):
            updates = self.sample(second, second * .025)
            if updates:
                pulse = updates[-1]
        self.assertEqual(pulse["baseline"], 0)
        self.assertEqual(pulse["ordinal"], 1)

    def test_pre_alert_anchor_catches_ramp_slower_than_all_rolling_windows(self):
        updates = []
        for second in range(126):
            updates += self.sample(second, second * .004)
        self.assertTrue(updates)
        self.assertEqual(updates[0]["baseline"], 0)
        self.assertAlmostEqual(updates[0]["trigger"], .5)

    def test_recovered_subthreshold_wiggle_does_not_create_a_pulse(self):
        for second, value in enumerate([0, .12, .03, -.04, 0]):
            self.assertFalse(self.sample(second, value))
        track = self.engine.tracks[(PATH, "spread")]
        self.assertFalse(track.pulse)

    def test_flat_negative_quote_is_not_new_opportunity(self):
        self.assertFalse(any(self.sample(second, -.32) for second in range(300)))

    def test_no_absolute_one_percent_gate(self):
        self.sample(0, .01)
        self.assertTrue(self.sample(2, .22))

    def test_width_only_change_is_not_price_displacement(self):
        self.sample(0, 0)
        self.assertEqual(self.sample(1, -.5, close_spread=.5, mid_spread=0), [])

    def test_basis_does_not_require_funding_or_one_percent(self):
        self.sample(0, -.1, metric="basis", funding_rate=None)
        self.assertTrue(self.sample(1, .11, metric="basis", funding_rate=None))

    def test_both_prices_moving_with_flat_spread_is_detected(self):
        self.engine.observe(PATH, "price", 100 * math.log(100), 1000, context(0))
        output = self.engine.observe(PATH, "price", 100 * math.log(102), 1001, context(0, long_mid=102, short_mid=102))
        self.assertEqual(output[0]["metric"], "price")

    def test_one_leg_price_is_not_claimed_as_both_moving(self):
        self.engine.observe(PATH, "price", 100 * math.log(100), 1000, context(0))
        self.assertEqual(self.engine.observe(PATH, "price", 50 * (math.log(100) + math.log(104)), 1001, context(4, short_mid=104)), [])

    def test_gap_does_not_create_jump_from_stale_baseline(self):
        self.sample(0, 0)
        self.assertEqual(self.sample(20, 1), [])

    def test_disconnect_retains_pulse_with_gap_state(self):
        self.sample(0, 0)
        first = self.sample(1, .5)[0]
        ended = self.engine.reset(PATH, 1002)
        self.assertEqual((ended[0]["id"], ended[0]["state"]), (first["id"], "data_gap"))
        self.assertEqual(self.sample(3, 1), [])

    def test_out_of_order_and_nan_cannot_move_peak(self):
        self.sample(2, 0)
        for at, value in ((1, 8), (2, 8), (3, float("nan")), (3, float("inf"))):
            self.assertEqual(self.sample(at, value), [])

    def test_small_retracement_does_not_chatter_or_rearm(self):
        self.sample(0, 0)
        first = self.sample(1, .5)[0]
        for second, value in enumerate([.48, .51, .46, .5], 2):
            output = self.sample(second, value)
            self.assertTrue(all(item["id"] == first["id"] for item in output))

    def test_notice_version_changes_on_state_not_every_tick(self):
        self.sample(0, 0)
        self.assertEqual(self.sample(1, .21)[0]["notice_version"], 1)
        self.assertEqual(self.sample(2, .25)[0]["notice_version"], 1)
        self.assertEqual(self.sample(3, .51)[0]["notice_version"], 2)
        self.assertEqual(self.sample(4, .1)[0]["notice_version"], 3)


class StreamHubTests(unittest.TestCase):
    def setUp(self):
        self.hub = AlertStreamHub()
        self.route = Route(PATH, ("gate", "DEMO_USDT"), ("bn_future", "DEMOUSDT"), mark_key=("bn_mark", "DEMOUSDT"))
        self.hub.register("spot_futures", [self.route], 1000)

    def book(self, feed, at, mid=100, **extra):
        symbol = "DEMO_USDT" if feed == "gate" else "DEMOUSDT"
        self.hub.ingest(feed, dict(dict(symbol=symbol, at=at, bid=mid-.001, ask=mid+.001, bid_qty=1, ask_qty=1), **extra), at)

    def test_end_to_end_peak_retrace_and_restart_queue(self):
        for at, mid in ((1000, 100), (1001, 101), (1002, 100.1), (1003, 100.4)):
            self.book("gate", at)
            self.book("bn_future", at+.001, mid)
        batch = [item for item in self.hub.batch() if item["metric"] == "spread"]
        self.assertEqual(len(batch), 2)
        self.assertEqual(batch[0]["state"], "retraced")
        self.assertAlmostEqual(batch[0]["peak"], .99799, places=4)
        self.assertEqual(batch[1]["ordinal"], 2)

    def test_stale_or_skewed_other_leg_cannot_trigger(self):
        self.book("gate", 1000)
        self.book("bn_future", 1000)
        self.book("bn_future", 1003, 101)
        self.assertEqual(self.hub.batch(), [])

    def test_quantity_only_frames_cache_freshness_without_recomputing_metrics(self):
        self.book("gate", 1000)
        self.book("bn_future", 1000)
        self.book("bn_future", 1000.1, bid_qty=2)
        self.assertEqual(self.hub.frame_counts["bn_future"]["unchanged_skipped"], 1)
        self.assertEqual(self.hub.books[self.route.short_key]["at"], 1000.1)
        self.assertFalse(self.hub.batch())

    def test_price_change_waiting_for_alignment_is_not_skipped_on_same_price_refresh(self):
        self.book("gate", 1000)
        self.book("bn_future", 1000)
        self.book("bn_future", 1002.1, 101)  # not yet aligned
        self.assertFalse(self.hub.batch())
        self.assertIn(PATH, self.hub.dirty)
        self.book("gate", 1002.11)  # same price, but now the spike is observable
        first = self.hub.batch()[0]
        self.assertGreater(first["peak"], .9)
        self.book("bn_future", 1002.12, 100.1)
        self.assertEqual(self.hub.batch()[0]["state"], "retraced")

    def test_zero_nan_crossed_and_empty_quantity_rejected(self):
        for values in ({"bid": 0}, {"ask": float("nan")}, {"ask": 80}, {"bid_qty": 0}):
            self.book("gate", 1000, **values)
        self.assertEqual(self.hub.rejected, 4)
        self.assertFalse(self.hub.books)

    def test_timestamp_age_and_order_rejected(self):
        self.book("gate", 1001, sequence=2)
        self.book("gate", 1002, sequence=1)
        self.hub.ingest("gate", dict(symbol="DEMO_USDT", at=900, bid=1, ask=1, bid_qty=1, ask_qty=1), 1002)
        self.assertEqual(self.hub.rejected, 2)

    def test_no_binance_mark_does_not_fabricate_zero_basis(self):
        for at, mid in ((1000, 100), (1001, 101)):
            self.book("gate", at)
            self.book("bn_future", at+.001, mid)
        self.assertIsNone(self.hub.batch()[0]["trigger_context"]["basis"])

    def test_alias_multiplier_normalizes_prices(self):
        self.hub.register("spot_futures", [Route(PATH, self.route.long_key, self.route.short_key, long_factor=1000)], 1000)
        self.book("gate", 1000, .1)
        self.book("bn_future", 1000, 100)
        self.assertEqual(self.hub.batch(), [])

    def test_removing_one_path_does_not_remove_other_venue(self):
        other = Route(("spot_futures", "DEMO/USDT", "Bitget", "Binance"), ("bitget", "DEMOUSDT"), self.route.short_key)
        self.hub.register("spot_futures", [self.route, other], 1000)
        self.hub.register("spot_futures", [other], 1001)
        self.assertEqual(set(self.hub.routes), {other.path})

    def test_acknowledging_old_batch_does_not_erase_new_peak(self):
        for at, mid in ((1000, 100), (1001, 101)):
            self.book("gate", at)
            self.book("bn_future", at+.001, mid)
        batch = self.hub.batch()
        self.book("bn_future", 1002, 102)
        self.hub.acknowledge(batch)
        self.assertTrue(self.hub.batch())
        self.assertGreater(self.hub.batch()[0]["peak"], 1.9)

    def test_failed_writer_batch_is_not_removed_by_read(self):
        self.book("gate", 1000)
        self.book("bn_future", 1000)
        self.book("bn_future", 1001, 101)
        self.assertEqual(self.hub.batch(), self.hub.batch())

    def test_hot_pending_pulses_cannot_starve_later_batches(self):
        self.hub.pending = {str(i): {"id": str(i), "version": 1} for i in range(1200)}
        first = self.hub.batch()
        for item in first:
            self.hub.pending[item["id"]]["version"] += 1
        self.hub.acknowledge(first)
        second = self.hub.batch()
        self.assertFalse({item["id"] for item in first} & {item["id"] for item in second})
        self.assertEqual(len(self.hub.pending), 1200)

    def test_queue_limit_is_visible_and_preserves_captured_pulses(self):
        self.hub.pending = {str(i): {"id": str(i), "version": 1} for i in range(20000)}
        self.book("gate", 1000)
        self.assertTrue(self.hub.status(1000)["overloaded"])
        self.assertEqual(len(self.hub.pending), 20000)
        self.hub.acknowledge(self.hub.batch())
        self.book("gate", 1001)
        self.assertFalse(self.hub.overloaded)

    def test_full_universe_is_subscribed_and_unchanged_shards_not_restarted(self):
        routes = [Route(("spot_futures", f"COIN{i}/USDT", "Bitget", "Binance"),
                        ("bitget", f"COIN{i}USDT"), ("bn_future", f"COIN{i}USDT")) for i in range(121)]
        self.hub.register("spot_futures", routes, 1000)
        with patch("anomaly_streams.threading.Thread") as thread:
            self.hub.reconcile()
            self.assertEqual(thread.call_count, 6)  # 4 x 40 Bitget; 2 x 100 Binance
            workers = dict(self.hub.workers)
            self.hub.reconcile()
            self.assertEqual(thread.call_count, 6)
            self.assertEqual(workers, self.hub.workers)
        self.assertEqual(sum(len(worker.symbols) for worker in self.hub.workers.values()), 242)

    def test_direct_new_divergence_is_filtered_before_rest_cycle(self):
        self.hub.ingest("bn_mark", dict(symbol="DEMOUSDT", mark=100, index=100, funding=0, at=1000), 1000)
        self.book("gate", 1000)
        self.book("bn_future", 1000)
        self.book("gate", 1001, 150)
        self.assertFalse(self.hub.batch())


class PublicParserTests(unittest.TestCase):
    def test_compressed_transport_preserves_order_and_rejects_invalid_utf8(self):
        import threading
        from websockets.sync.client import connect
        from websockets.sync.server import serve
        from websockets.exceptions import ConnectionClosedError

        def handler(socket):
            for value in (100, 101, 100.1, 100.4):
                socket.send(json.dumps(dict(s="BTCUSDT", b=value, a=value+.01, B=2, A=2)))
            socket.send(b"\xff", text=True)
            socket.recv(timeout=2)

        def safe_handler(socket):
            try:
                handler(socket)
            except ConnectionClosedError:
                pass  # client correctly closes the invalid-UTF8 connection

        with serve(safe_handler, "127.0.0.1", 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with connect(f"ws://127.0.0.1:{server.socket.getsockname()[1]}", proxy=None) as socket:
                    self.assertIn("permessage-deflate", socket.response.headers["Sec-WebSocket-Extensions"])
                    values = [parse_frame("bn_spot", socket.recv(timeout=2), 1000)[0]["bid"] for _ in range(4)]
                    self.assertEqual(values, [100, 101, 100.1, 100.4])
                    with self.assertRaises(ConnectionClosedError):
                        socket.recv(timeout=2)
            finally:
                server.shutdown()
                thread.join(timeout=2)

    def test_only_public_subscriptions_no_auth_or_orders(self):
        for feed in ("bn_spot", "bn_future", "bn_mark", "gate", "bitget", "bybit", "okx"):
            self.assertNotRegex(json.dumps(subscription(feed, ["BTCUSDT"])).lower(), r"apikey|secret|login|order\.create|withdraw")

    def test_binance_spot_has_receipt_not_fake_exchange_timestamp(self):
        item = parse_frame("bn_spot", {"s": "BTCUSDT", "u": 5, "b": "100", "a": "101", "B": "1", "A": "2"}, 1000)[0]
        self.assertEqual((item["at"], item["timestamp_source"]), (1000, "receipt"))

    def test_binance_futures_bounded_full_snapshot_subscription(self):
        from anomaly_streams import ENDPOINTS
        self.assertEqual(ENDPOINTS["bn_future"], "wss://fstream.binance.com/public/ws")
        self.assertEqual(subscription("bn_future", ["BTCUSDT"])["params"], ["btcusdt@depth5@500ms"])
        self.assertEqual(subscription("bn_spot", ["BTCUSDT"])["params"], ["btcusdt@bookTicker"])
        self.assertEqual(subscription("bn_mark", ["BTCUSDT"])["params"], ["btcusdt@markPrice@1s"])

    def test_binance_futures_partial_depth_uses_best_full_levels(self):
        data = dict(e="depthUpdate", E=1000000, s="BTCUSDT", u=8,
                    b=[["100", "2"], ["99", "3"]], a=[["101", "4"], ["102", "5"]])
        item = parse_frame("bn_future", data, 1000)[0]
        self.assertEqual((item["bid"], item["ask"], item["bid_qty"], item["ask_qty"]), (100, 101, 2, 4))
        self.assertEqual((item["at"], item["sequence"], item["timestamp_source"]), (1000, 8, "exchange"))
        self.assertEqual(parse_frame("bn_future", {**data, "b": []}, 1000), [])
        self.assertEqual(parse_frame("bn_future", {**data, "a": []}, 1000), [])
        # Futures depth arrays must never be accepted by the spot bookTicker parser.
        self.assertEqual(parse_frame("bn_spot", data, 1000), [])

    def test_gate_exact_timestamp_and_volume(self):
        data = {"event": "update", "channel": "spot.book_ticker", "result": {"s": "BTC_USDT", "u": 2, "t": 1000000, "b": "100", "a": "101", "B": "2", "A": "1"}}
        self.assertEqual(parse_frame("gate", data, 1000)[0]["at"], 1000)

    def test_bybit_only_full_level_one_snapshots(self):
        data = dict(topic="orderbook.1.BTCUSDT", type="snapshot", ts=1000000, data={"s": "BTCUSDT", "b": [["100", "1"]], "a": [["101", "1"]], "u": 1})
        self.assertTrue(parse_frame("bybit", data, 1000)[0]["reset"])
        self.assertEqual(parse_frame("bybit", {**data, "type": "delta"}, 1000), [])

    def test_bitget_v3_snapshot(self):
        data = dict(arg={"topic": "books1", "symbol": "BTCUSDT"}, action="snapshot", data=[{"ts": "1000000", "seq": 8, "b": [["100", "1"]], "a": [["101", "1"]]}])
        self.assertEqual(parse_frame("bitget", data, 1000)[0]["sequence"], 8)

    def test_okx_full_bbo(self):
        data = dict(arg={"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"}, action="snapshot", data=[{"ts": "1000000", "seqId": 8, "bids": [["100", "1", "0", "1"]], "asks": [["101", "1", "0", "1"]]}])
        self.assertEqual(parse_frame("okx", data, 1000)[0]["bid"], 100)
        data.pop("action")  # live bbo-tbt frames omit action; they are full snapshots
        self.assertEqual(parse_frame("okx", data, 1000)[0]["bid"], 100)

    def test_empty_book_and_subscription_error(self):
        self.assertEqual(parse_frame("bybit", dict(topic="orderbook.1.BTCUSDT", type="snapshot", data={"s": "BTCUSDT", "b": [], "a": []}), 1000), [])
        with self.assertRaises(ValueError):
            parse_frame("okx", {"event": "error", "code": "60012"}, 1000)


class PulseStorageTests(unittest.TestCase):
    def setUp(self):
        self.ctx = module.app.app_context()
        self.ctx.push()
        module.db.drop_all()
        module.db.create_all()

    def tearDown(self):
        module.db.session.remove()
        module.db.drop_all()
        self.ctx.pop()

    def test_duplicate_retry_keeps_one_event_and_highest_version(self):
        engine = PulseDetector()
        now = datetime.now(timezone.utc).timestamp() - 3
        engine.observe(PATH, "spread", 0, now, context(0))
        first = engine.observe(PATH, "spread", .3, now+1, context(.3))
        last = engine.observe(PATH, "spread", .8, now+2, context(.8))
        module.persist_alert_pulses(first)
        module.persist_alert_pulses(last)
        module.persist_alert_pulses(first)
        self.assertEqual(module.AlertEvent.query.count(), 1)
        self.assertEqual(json.loads(module.AlertPulse.query.one().evidence_json)["peak"], .8)
        self.assertEqual(module.AlertEvent.query.one().open_spread, .3)

    def test_restart_under_old_peak_persists_both_events(self):
        engine = PulseDetector()
        now = datetime.now(timezone.utc).timestamp() - 4
        updates = {}
        for i, value in enumerate((0, 1, .1, .4)):
            for pulse in engine.observe(PATH, "spread", value, now+i, context(value)):
                updates[pulse["id"]] = pulse
        module.persist_alert_pulses(list(updates.values()))
        self.assertEqual(module.AlertEvent.query.count(), 2)
        self.assertEqual(module.AlertPulse.query.count(), 2)

    def test_new_pulse_does_not_use_legacy_thirty_minute_gate_or_lark(self):
        engine = PulseDetector()
        now = datetime.now(timezone.utc).timestamp()-2
        engine.observe(PATH, "spread", 0, now, context(0))
        with patch.object(module, "create_alert", side_effect=AssertionError("legacy gate")), patch.object(module, "urlopen", side_effect=AssertionError("no network")):
            module.persist_alert_pulses(engine.observe(PATH, "spread", .2, now+1, context(.2)))
        self.assertEqual(module.LarkPushState.query.count(), 0)


if __name__ == "__main__":
    unittest.main()

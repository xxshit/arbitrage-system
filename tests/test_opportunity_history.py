import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

os.environ["DATABASE_URL"]="sqlite://"
os.environ.setdefault("SECRET_KEY","opportunity-history-test")
import app as module
from opportunity_history import BookBuffer, ProfileCache
from opportunity_pool import funding_profile
from spread_history import HistoryBuffer, HistoryStore, observations, decode_chunk

NOW=1792540800
PATH=("spot_futures","TEST/USDT","Gate","Binance")


def quote(at=NOW):
    return dict(long=dict(bid=99.9,ask=100,bid_qty=50,ask_qty=60),
                short=dict(bid=101.5,ask=101.6,bid_qty=70,ask_qty=80,funding=.005,interval=4,funding_at=at),
                long_at=at,short_at=at,metadata_at=at,transfer_state="normal")


class OpportunityHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=HistoryStore(self.temp.name)
        with module.app.app_context():
            module.db.create_all()

    def tearDown(self):
        self.temp.cleanup()
        with module.app.app_context():
            module.db.session.remove()

    def test_book_retains_first_actual_pair_once_per_five_minutes_across_restart(self):
        buffer=BookBuffer()
        buffer.observe({PATH:quote(NOW)},NOW)
        changed=quote(NOW+5);changed["long"]["ask"]=100.1
        buffer.observe({PATH:changed},NOW+5)
        self.assertEqual(buffer.persist(self.store),1)
        restarted=BookBuffer();restarted.observe({PATH:changed},NOW+5);restarted.persist(self.store)
        samples=list(self.store.chunks("TEST/USDT","Gate","book",NOW,NOW+300))
        data=decode_chunk(samples[0])
        self.assertEqual(len(data),1)
        self.assertEqual(next(iter(data.values()))[:9],[NOW,99.9,100,101.5,101.6,50,60,70,80])

    def test_stale_unsynchronized_and_other_strategies_not_collected(self):
        buffer=BookBuffer()
        buffer.observe({PATH:quote(NOW-5)},NOW)
        buffer.observe({("futures_futures",*PATH[1:]):quote()},NOW)
        self.assertFalse(buffer.pending)
        live=quote();live["short_at"]=NOW-3
        buffer.observe({PATH:live},NOW)
        self.assertFalse(buffer.pending)

    def test_unknown_quantity_is_null_never_nan_or_zero(self):
        live=quote();live["long"]["bid_qty"]=float("nan");live["short"]["ask_qty"]=0
        buffer=BookBuffer();buffer.observe({PATH:live},NOW);buffer.persist(self.store)
        result=decode_chunk(next(self.store.chunks("TEST/USDT","Gate","book",NOW,NOW+1)))
        sample=next(iter(result.values()))
        self.assertIsNone(sample[5]);self.assertIsNone(sample[8])

    def test_low_disk_retains_pending_and_size_is_bounded(self):
        buffer=BookBuffer(limit=2)
        for i in range(3):
            at=NOW+i*300
            buffer.observe({PATH:quote(at)},at)
        self.assertEqual(len(buffer.pending),2);self.assertEqual(buffer.dropped,1)
        with patch("spread_history.shutil.disk_usage",return_value=SimpleNamespace(free=0)):
            with self.assertRaises(OSError):buffer.persist(self.store)
        self.assertEqual(len(buffer.pending),2)
        self.assertEqual(buffer.persist(self.store),2)

    def test_close_collection_uses_exact_venue_and_valid_time_no_backfill(self):
        rows=[{"symbol":"TEST/USDT","rows":[dict(long_exchange="Gate",open_spread=.5,close_spread=.6,basis=.1,basis_source_at=NOW)]}]
        clocks={name:NOW for name in ("futures_books","gate_spot","funding")}
        result=list(observations(rows,clocks,{},NOW))
        self.assertIn((("TEST/USDT","Gate","close"),NOW,.6),result)
        self.assertFalse(any(key[-1]=="close" for key,_,_ in observations(rows,clocks,{"gate_spot":"old"},NOW)))
        rows[0]["rows"][0]["close_spread"]=True
        self.assertFalse(any(key[-1]=="close" for key,_,_ in observations(rows,clocks,{},NOW)))

    def fill(self,minutes=90,close=True):
        buffer=HistoryBuffer()
        for i in range(minutes):
            at=NOW-minutes*60+i*60
            buffer.observe([(("TEST/USDT","Gate","open"),at,.5),
                            (("TEST/USDT","Gate","open"),at+5,1.5 if i==20 else .51)])
            if close:buffer.observe([(("TEST/USDT","Gate","close"),at,.55)])
        self.store.persist(buffer,NOW)

    def test_summary_captures_extreme_without_overweighting_it_as_normal(self):
        self.fill()
        cache=ProfileCache();cache.refresh(self.store,NOW)
        data,at=cache.snapshot()
        profile=data[("TEST/USDT","Gate")]
        self.assertTrue(profile["ready"]);self.assertTrue(profile["close_ready"])
        self.assertAlmostEqual(profile["median"],.51)
        self.assertEqual(profile["high"],1.5)
        self.assertEqual(profile["exit_q75"],.55)
        self.assertEqual(at,NOW)

    def test_old_open_history_does_not_create_close_evidence(self):
        self.fill(close=False)
        cache=ProfileCache();cache.refresh(self.store,NOW)
        profile=cache.snapshot()[0][("TEST/USDT","Gate")]
        self.assertTrue(profile["ready"]);self.assertFalse(profile["close_ready"])
        self.assertIsNone(profile["exit_q75"])

    def test_short_or_stale_history_not_ready_and_no_cross_path_mix(self):
        self.fill(minutes=10)
        cache=ProfileCache();cache.refresh(self.store,NOW)
        self.assertFalse(cache.snapshot()[0][("TEST/USDT","Gate")]["ready"])
        self.assertNotIn(("TEST/USDT","Binance"),cache.snapshot()[0])
        self.fill(minutes=90)
        cache.refresh(self.store,NOW+3600)
        self.assertFalse(cache.snapshot()[0][("TEST/USDT","Gate")]["ready"])

    def test_book_api_auth_bounded_response_validation_and_no_upstream(self):
        buffer=BookBuffer()
        for i in range(300):
            at=NOW-i*300;buffer.observe({PATH:quote(at)},at)
        # Separate chronological observations to avoid intentionally rejecting old timestamps.
        for i in reversed(range(300)):
            at=NOW-i*300
            another=BookBuffer();another.observe({PATH:quote(at)},at);another.persist(self.store)
        with patch.object(module,"SPREAD_HISTORY_STORE",self.store),patch.object(module.time,"time",return_value=NOW),patch.object(module,"get_json",side_effect=AssertionError("No network")):
            with module.app.test_request_context("/api/arbitrage-thinking/book-history?symbol=TEST/USDT&exchange=Gate&period=15d"):
                data=module.opportunity_book_history().get_json()
                self.assertEqual(len(data["samples"]),288)
                self.assertEqual(data["total_samples"],300);self.assertTrue(data["truncated"])
            for args in ("symbol=TEST/USDT&exchange=Bybit","symbol=../USDT&exchange=Gate","symbol=TEST/USDT&exchange=Gate&period=99d"):
                with module.app.test_request_context("/api/arbitrage-thinking/book-history?"+args):
                    self.assertEqual(module.opportunity_book_history()[1],400)
        response=module.app.test_client().get("/api/arbitrage-thinking/book-history")
        self.assertEqual(response.status_code,401)
        self.assertEqual(response.headers["Cache-Control"],"private, no-store")

    def test_spot_request_does_not_load_dual_and_validates_settings(self):
        with patch.object(module,"load_latest_market_snapshot",return_value={}),patch.object(module,"spot_thinking_candidates",return_value=[]),patch.object(module,"load_latest_dual_futures_snapshot",side_effect=AssertionError("No dual request")),patch.object(module,"mark_delisting_items"):
            with module.app.test_request_context("/api/arbitrage-thinking/simple?strategy=spot"):
                self.assertEqual(module.simple_arbitrage_thinking().get_json()["dual_simple"],[])
            for args in ("notional=nan","notional=0","hold_hours=2","strategy=funding"):
                with module.app.test_request_context("/api/arbitrage-thinking/simple?"+args):
                    self.assertEqual(module.simple_arbitrage_thinking()[1],400)

    def test_candidate_assembly_clones_rows_and_isolates_routes(self):
        row=dict(long_exchange="Gate",short_exchange="Binance",open_spread=.5,close_spread=.6,
                 long_ask=100,long_bid=99.9,short_bid=101.5,short_ask=101.6,funding_interval_hours=4,funding_rate=.005)
        snapshot={"symbols":[{"symbol":"TEST/USDT","rows":[row,{**row,"long_exchange":"Bitget"}]},
                             {"symbol":"EQUITY/USDT","rows":[row]}]}
        before=deepcopy(snapshot)
        def enrich(groups):
            groups[0]["rows"][0]["price_divergence"]={"state":"confirmed"}
        stats={"TESTUSDT":{"previous":.004,"day_1":.03,"day_3":.09,"day_7":-.21,"day_30":1.193}}
        with module.app.app_context(),patch.object(module,"is_rwa_stock_pair",side_effect=lambda s:s=="EQUITY/USDT"),patch.object(module,"enrich_transfer_networks",side_effect=enrich),patch.object(module,"enrich_price_changes"),patch.object(module,"funding_statistics",return_value=stats) as totals,patch.object(module,"opportunity_funding_events",return_value={}),patch.object(module,"symbol_alias_rows",return_value=[]),patch.object(module.ALERT_STREAM_HUB,"screening_quotes",return_value={}):
            output=module.spot_thinking_candidates(snapshot,1000,72)
            totals.assert_called_once_with(["TESTUSDT"])
        self.assertEqual(snapshot,before)
        self.assertEqual([item["long_exchange"] for item in output],["Bitget"])
        self.assertEqual({field:output[0][field] for field in (
            "funding_previous","funding_24h","funding_3d","funding_7d","funding_30d")},
            dict(funding_previous=.004,funding_24h=.03,funding_3d=.09,funding_7d=-.21,funding_30d=1.193))
        # Display totals must not replace the independent profile used by grading.
        self.assertIsNone(output[0]["funding"]["total"])

    def test_empty_funding_statistics_remain_unknown_not_zero_or_current_rate_times_periods(self):
        snapshot={"symbols":[{"symbol":"NOHISTORY/USDT","rows":[{
            "long_exchange":"Gate","open_spread":.5,"funding_rate":.005,"funding_interval_hours":4}]}]}
        with module.app.app_context(),patch.object(module,"is_rwa_stock_pair",return_value=False),patch.object(module,"enrich_transfer_networks"),patch.object(module,"enrich_price_changes"),patch.object(module,"funding_statistics",return_value={}),patch.object(module,"opportunity_funding_events",return_value={}),patch.object(module,"symbol_alias_rows",return_value=[]),patch.object(module.ALERT_STREAM_HUB,"screening_quotes",return_value={}):
            item=module.spot_thinking_candidates(snapshot,1000,72)[0]
        for field in ("funding_previous","funding_24h","funding_3d","funding_7d","funding_30d"):
            self.assertIsNone(item[field])


if __name__=="__main__":
    unittest.main()

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opportunity_pool import POLICY, quote_metrics
from opportunity_validation import ValidationLedger, HORIZONS, close_scenario

NOW = 1800000000
PATH = ("spot_futures","DEMO/USDT","Gate","Binance")
SOURCE = dict(long_key=["gate","DEMO_USDT"],short_key=["bn_future","DEMOUSDT"],long_factor=1,short_factor=1)


def live(at, spot=100, future=101, quantity=100):
    return dict(long=dict(bid=spot,ask=spot+.01,bid_qty=quantity,ask_qty=quantity),
                short=dict(bid=future-.01,ask=future,bid_qty=quantity,ask_qty=quantity),
                long_at=at,short_at=at,metadata_at=at,transfer_state="normal")


def candidate(now=NOW, score=60, venue="Gate"):
    return dict(symbol="DEMO/USDT",long_exchange=venue,short_exchange="Binance",score=score,score_version=2,
                short_interval=4,scenario={},score_parts={"spread":50},risks=[],drivers=["价差"],status="watch",
                _validation_quote=quote_metrics(live(now,99.99,102.01),now),
                _validation_source=deepcopy(SOURCE),_validation_raw="DEMOUSDT")


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.ledger=ValidationLedger(self.temp.name)
        self.ledger.restore()

    def tearDown(self):
        self.temp.cleanup()

    def enroll(self):
        self.assertEqual(self.ledger.enroll([candidate()],NOW),1)
        return next(iter(self.ledger.active.values()))

    def tick_hour(self, gap=False):
        for second in range(5,3601,5):
            if gap and 600 <= second <= 1200:
                continue
            self.ledger.observe({PATH:live(NOW+second,100,100.1)},NOW+second,{PATH:SOURCE})

    def test_entry_is_frozen_and_daily_refresh_never_reenrolls(self):
        record=self.enroll()
        frozen=deepcopy(record["entry"])
        self.assertEqual(self.ledger.enroll([candidate(NOW+10,99)],NOW+10),0)
        self.ledger.persist(NOW)
        restarted=ValidationLedger(self.temp.name);restarted.restore()
        stored=next(iter(restarted.active.values()))
        self.assertEqual(stored["entry"],frozen)
        self.assertEqual(stored["score"],60)
        self.assertEqual(restarted.enroll([candidate(NOW+20,99)],NOW+20),0)

    def test_equal_base_pnl_is_not_difference_of_two_spreads(self):
        entry=self.enroll()["entry"]
        value=close_scenario(entry,quote_metrics(live(NOW,200,201),NOW))
        self.assertAlmostEqual(value["gross"],1)
        self.assertAlmostEqual(value["fees"],.9933)
        self.assertAlmostEqual(value["net"],-.0933)

    def test_frozen_cost_policy_survives_later_policy_change(self):
        record=self.enroll();entry=deepcopy(record["entry"])
        with patch.dict(POLICY,spot_fee=99):
            self.assertEqual(record["entry"],entry)
            self.assertLess(close_scenario(entry,quote_metrics(live(NOW,100,100.1),NOW))["fees"],1)

    def test_nonfinite_pnl_cannot_poison_durable_json(self):
        entry=self.enroll()["entry"]
        entry.update(spot_ask=1e-100,notional=1e-100)
        self.assertIsNone(close_scenario(entry,dict(long=dict(bid=1e308,bid_qty=100),
                         short=dict(ask=1,ask_qty=100),at=NOW)))

    def test_ordinary_ticks_after_an_outcome_still_batch_for_five_minutes(self):
        self.enroll();self.ledger.persist(NOW);self.tick_hour()
        self.assertEqual(self.ledger.persist(NOW+3600),1)
        self.ledger.observe({PATH:live(NOW+3605)},NOW+3605,{PATH:SOURCE})
        self.assertEqual(self.ledger.persist(NOW+3605),0)
        self.assertEqual(self.ledger.persist(NOW+3900),1)

    def test_model_versions_and_venues_never_mix_and_sql_entry_stays_frozen(self):
        second=candidate(venue="Bitget");second["score_version"]=3
        self.ledger.enroll([candidate(),second],NOW);self.ledger.persist(NOW)
        conn=self.ledger.connect()
        before=conn.execute("SELECT id,entry,score,model,entered FROM cohorts ORDER BY id").fetchall();conn.close()
        self.tick_hour();self.ledger.persist(NOW+3600)
        conn=self.ledger.connect()
        after=conn.execute("SELECT id,entry,score,model,entered FROM cohorts ORDER BY id").fetchall();conn.close()
        self.assertEqual(before,after)
        self.assertEqual(self.ledger.read(NOW+3600,horizon=3600,model=2)["observed"],1)
        self.assertEqual(self.ledger.read(NOW+3600,horizon=3600,model=3)["observed"],0)
        self.assertEqual(self.ledger.read(NOW+3600,symbol="DEMO/USDT",exchange="Bitget",model=2)["total"],0)

    def test_hour_result_is_real_future_data_and_immutable_afterwards(self):
        record=self.enroll()
        self.assertFalse(record["progress"]["results"])
        self.tick_hour()
        result=deepcopy(record["progress"]["results"]["3600"])
        self.assertEqual(result["status"],"observed")
        self.assertGreater(result["net"],0)
        self.assertAlmostEqual(result["coverage"],1)
        self.ledger.observe({PATH:live(NOW+3610,10,200)},NOW+3610,{PATH:SOURCE})
        self.assertEqual(result,record["progress"]["results"]["3600"])
        self.ledger.persist(NOW+3610)
        data=self.ledger.read(NOW+3610,horizon=3600)
        self.assertEqual(data["observed"],1)
        self.assertEqual(data["bands"][2]["positive"],1)
        self.assertEqual(data["bands"][2]["total"],1)

    def test_gap_is_not_labeled_a_valid_or_losing_sample(self):
        record=self.enroll();self.tick_hour(gap=True)
        result=record["progress"]["results"]["3600"]
        self.assertEqual(result["status"],"insufficient")
        self.assertIsNone(result["net"])
        self.assertGreater(result["max_gap"],300)

    def test_missing_deadline_expires_and_cannot_be_backfilled_with_later_price(self):
        record=self.enroll()
        self.ledger.observe({},NOW+3721)
        before=deepcopy(record["progress"]["results"]["3600"])
        self.assertEqual(before["status"],"insufficient")
        self.ledger.observe({PATH:live(NOW+3722)},NOW+3722,{PATH:SOURCE})
        self.assertEqual(before,record["progress"]["results"]["3600"])

    def test_insufficient_exit_size_and_stale_quotes_do_not_count(self):
        record=self.enroll()
        self.ledger.observe({PATH:live(NOW+5,quantity=1)},NOW+5,{PATH:SOURCE})
        self.ledger.observe({PATH:live(NOW+5)},NOW+20,{PATH:SOURCE})
        self.assertEqual(record["progress"]["windows"]["3600"]["samples"],0)
        self.assertIsNone(close_scenario(record["entry"],quote_metrics(live(NOW,quantity=1),NOW)))

    def test_unknown_entry_size_future_time_or_no_identity_skips_admission(self):
        item=candidate();item["_validation_quote"]["capacity"]=None
        self.assertEqual(self.ledger.enroll([item],NOW),0)
        self.assertEqual(self.ledger.enroll([candidate(NOW+1)],NOW),0)
        item=candidate();item["_validation_source"]=None
        self.assertEqual(self.ledger.enroll([item],NOW),0)

    def test_changed_mapping_invalidates_only_that_path(self):
        self.ledger.enroll([candidate(),candidate(venue="Bitget")],NOW)
        self.ledger.observe({PATH:live(NOW+5)},NOW+5,{PATH:{**SOURCE,"long_factor":1000}})
        gate=self.ledger.active[("spot","DEMO/USDT","Gate","Binance")]
        bitget=self.ledger.active[("spot","DEMO/USDT","Bitget","Binance")]
        self.assertEqual(len(gate["progress"]["results"]),4)
        self.assertFalse(bitget["progress"]["results"])
        self.assertTrue(all(r["reason"]=="配对发生变更" for r in gate["progress"]["results"].values()))

    def test_funding_is_separate_keeps_negatives_and_can_wait_for_late_collection(self):
        record=self.enroll();self.tick_hour()
        before=record["progress"]["results"]["3600"]["net"]
        self.assertFalse(self.ledger.funding_requests(NOW+3600))
        self.assertEqual(len(self.ledger.funding_requests(NOW+3720)),1)
        self.ledger.set_funding(record["id"],3600,dict(total=-.02,count=1,complete=False),NOW+3720)
        self.assertFalse(record["progress"]["results"]["3600"]["funding_final"])
        self.ledger.set_funding(record["id"],3600,dict(total=-.01,count=2,complete=True),NOW+3740)
        self.assertEqual(record["progress"]["results"]["3600"]["net"],before)
        self.assertEqual(record["progress"]["results"]["3600"]["funding"]["total"],-.01)
        self.ledger.set_funding(record["id"],3600,dict(total=99,complete=True),NOW+3800)
        self.assertEqual(record["progress"]["results"]["3600"]["funding"]["total"],-.01)

    def test_partial_checkpoint_and_low_disk_preserve_existing_records(self):
        record=self.enroll();self.ledger.persist(NOW)
        self.ledger.observe({PATH:live(NOW+5)},NOW+5,{PATH:SOURCE})
        self.assertEqual(self.ledger.persist(NOW+5),0)
        with patch("opportunity_validation.shutil.disk_usage") as usage:
            usage.return_value.free=1
            with self.assertRaises(OSError):self.ledger.persist(NOW+301)
        self.assertGreater(record["revision"],record["saved_revision"])
        self.ledger.persist(NOW+302)
        self.ledger.checkpoint()
        self.assertTrue((Path(self.temp.name)/"checkpoints/opportunity-validation.sqlite3").exists())

    def test_read_is_read_only_and_new_database_empty_is_honest(self):
        with tempfile.TemporaryDirectory() as temp:
            empty=ValidationLedger(Path(temp)/"missing")
            self.assertEqual(empty.read(NOW)["total"],0)
            self.assertFalse((Path(temp)/"missing").exists())
        self.enroll();self.ledger.persist(NOW)
        data=self.ledger.read(NOW+30,horizon=3600)
        self.assertEqual(data["pending"],1)
        self.assertEqual(data["observed"],0)
        self.assertEqual(self.ledger.read(NOW,symbol="DEMO/USDT",exchange="Bitget")["total"],0)

    def test_hard_cap_stops_new_records_without_deleting_old_evidence(self):
        self.ledger.max_records=1;self.enroll();self.ledger.persist(NOW)
        self.assertEqual(self.ledger.enroll([candidate(venue="Bitget")],NOW),0)
        self.assertTrue(self.ledger.capacity_limited)
        self.assertEqual(self.ledger.read(NOW)["total"],1)

    def test_storage_reserve_stops_admission_but_still_finishes_existing_outcomes(self):
        self.enroll();self.ledger.persist(NOW)
        self.ledger.storage_bytes=64*1024**2
        self.assertEqual(self.ledger.enroll([candidate(venue="Bitget")],NOW),0)
        self.tick_hour();self.assertEqual(self.ledger.persist(NOW+3600),1)
        self.assertEqual(self.ledger.read(NOW+3600,horizon=3600)["observed"],1)

    def test_finished_path_can_start_new_round_without_rewriting_previous(self):
        record=self.enroll();self.ledger.observe({},NOW+7*86400+121)
        for h in HORIZONS:
            self.ledger.set_funding(record["id"],h,dict(total=None,complete=False),NOW+8*86400)
        self.ledger.persist(NOW+8*86400)
        self.assertFalse(self.ledger.active)
        self.assertEqual(self.ledger.enroll([candidate(NOW+8*86400,20)],NOW+8*86400),1)
        self.ledger.persist(NOW+8*86400)
        self.assertEqual(self.ledger.read(NOW+8*86400)["total"],2)

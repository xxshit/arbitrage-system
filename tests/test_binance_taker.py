"""Synthetic execution invariants; no app import, production DB or exchange IO."""
import base64
from copy import deepcopy
from decimal import Decimal as D
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from binance_hedge.client import BinanceClient, ExchangeError
from binance_hedge.executor import Executor, final_fill, order_params
from binance_hedge.planner import Book, GuardError, Rules, common_step, dec, plan_clip
from binance_hedge.store import Store, Vault, accounts, intents


def rules(step=".001", minimum="5", futures=False):
    return Rules.parse(dict(symbol="BTCUSDT", baseAsset="BTC", quoteAsset="USDT", status="TRADING",
        isSpotTradingAllowed=True, contractType="PERPETUAL", marginAsset="USDT", orderTypes=["LIMIT"], timeInForce=["FOK", "IOC"],
        filters=[dict(filterType="PRICE_FILTER", tickSize=".01", minPrice=".01", maxPrice="1000000"),
                 dict(filterType="LOT_SIZE", stepSize=step, minQty=step, maxQty="100"),
                 dict(filterType="MIN_NOTIONAL", minNotional=minimum)]), futures)


def book(future=False, age=0, size="100"):
    now = time.monotonic()
    return Book.parse({"lastUpdateId": 123, "bids": [["102" if future else "99.99", size]],
                       "asks": [["102.01" if future else "100", size], ["100.05" if not future else "102.02", size]]}, now-age, now-age)


def request_body(**changes):
    return dict(symbol="BTC/USDT", quote_amount="100", clip_quote="20", slippage_bps="10", min_net_bps="0", leverage="1", residual_policy="hold", **changes)


def make_plan(req=None):
    req = req or request_body()
    result = plan_clip(req, rules(), rules(futures=True), book(), book(True), time.monotonic(), D(".001"), D(".0005"))
    return dict(result, position_side="BOTH", leverage=1, margin_type="CROSSED", account_uid="12345")


class PlannerTests(unittest.TestCase):
    def plan(self, req=None, sr=None, fr=None, sb=None, fb=None, anchor=None):
        return plan_clip(req or request_body(), sr or rules(), fr or rules(futures=True), sb or book(), fb or book(True), time.monotonic(), D(".001"), D(".0005"), anchor)

    def test_common_step_is_lcm_not_largest(self):
        self.assertEqual(common_step(D(".002"), D(".003")), D(".006"))
        p = self.plan(sr=rules(".002"), fr=rules(".003", futures=True))
        self.assertEqual(dec(p["quantity"]) % D(".006"), 0)

    def test_equal_base_quantity_fok_caps_and_conservative_fees(self):
        p = self.plan()
        self.assertEqual(p["time_in_force"], "FOK")
        self.assertEqual(p["buy_cap"], "100.10")
        self.assertEqual(p["sell_cap"], "101.90")
        self.assertLessEqual(dec(p["quantity"]) * max(dec(p["buy_cap"]), dec(p["sell_cap"])), D(20))
        self.assertLess(dec(p["worst_net_bps"]), (dec(p["sell_cap"])/dec(p["buy_cap"])-1)*10000)
        self.assertFalse(p["atomic"])

    def test_quantity_uses_thinner_book_ten_percent(self):
        p = self.plan(sb=book(size=".8"), fb=book(True, size=".9"))
        self.assertEqual(dec(p["quantity"]), D(".09"))

    def test_vwap_walks_multiple_levels(self):
        now=time.monotonic()
        sb=Book.parse(dict(lastUpdateId=4,bids=[["99","8"]],asks=[["100",".01"],["100.05","5"]]),now,now)
        p=self.plan(sb=sb)
        self.assertEqual(p["spot_levels"],2)
        self.assertGreater(dec(p["spot_vwap"]),D(100))

    def test_stale_slow_and_asynchronous_books_rejected(self):
        for sb in (book(age=2), Book(book().bids,book().asks,time.monotonic()-.6,time.monotonic(),1),book(age=.2)):
            with self.subTest(book=sb), self.assertRaises(GuardError):self.plan(sb=sb)

    def test_caps_do_not_chase_when_confirmation_price_moves(self):
        p=self.plan();p["buy_cap"]="99.99"
        with self.assertRaisesRegex(GuardError,"追价"):self.plan(anchor=p)

    def test_min_notional_blocks_clip_instead_of_rounding_up(self):
        with self.assertRaises(GuardError):self.plan(sr=rules(minimum="25"))

    def test_multiplier_contract_rejected(self):
        fr=rules(futures=True)
        from dataclasses import replace
        with self.assertRaises(GuardError):self.plan(fr=replace(fr,base="1000BTC"))

    def test_budget_slippage_and_net_edge_guards(self):
        for changes in ({"slippage_bps":"51"},{"clip_quote":"1001"},{"quote_amount":"5"},{"min_net_bps":"500"}):
            req=request_body();req.update(changes)
            with self.subTest(changes=changes),self.assertRaises(GuardError):self.plan(req)

    def test_invalid_decimals_and_crossed_books(self):
        for v in ["NaN","Infinity","1e-9999999","1e9999999","1"*1000,None]:
            with self.subTest(v=str(v)[:20]),self.assertRaises(GuardError):dec(v)
        with self.assertRaises(GuardError):Book.parse(dict(lastUpdateId=1,bids=[["100","1"]],asks=[["99","1"]]),0,0)


class FakeClient:
    parallel = staticmethod(BinanceClient.parallel)
    def __init__(self):
        self.records={};self.calls=[];self.unknown=set();self.zero=set();self.base_fee="0";self.incomplete=False
        self.barrier=threading.Barrier(2);self.query_missing=set();self.partial={};self.fill_offset=0
    def sync_clocks(self):pass
    def prepare(self,req,anchor=None):return make_plan(req),rules(),rules(futures=True)
    def depth(self,market,symbol):return book(market=="futures")
    def submit(self,market,params):
        if getattr(self,'before_order_send',None):self.before_order_send(market)
        self.calls.append((market,deepcopy(params)))
        if params["newClientOrderId"].endswith(("s","f")):self.barrier.wait(timeout=1)
        quantity="0" if market in self.zero else self.partial.get(market,params["quantity"])
        record=dict(clientOrderId=params["newClientOrderId"],symbol=params["symbol"],side=params["side"],
                    orderId=len(self.calls)+100,executedQty=quantity,origQty=params["quantity"],status="FILLED" if quantity!="0" else "EXPIRED",updateTime=1000)
        self.records[(market,params["newClientOrderId"])]=record
        if market in self.unknown:raise ExchangeError("TIMEOUT",uncertain=True)
        return record
    def query(self,market,symbol,client_id):
        if market in self.query_missing:raise ExchangeError(-2013)
        return self.records[(market,client_id)]
    def fills(self,market,symbol,order_id):
        if self.incomplete:return []
        r=next(r for (m,_),r in self.records.items() if m==market and str(r["orderId"])==str(order_id))
        return [dict(id=order_id,orderId=order_id,symbol=symbol,qty=r["executedQty"],price="100" if market=="spot" else "102",commission=self.base_fee if market=="spot" else "0",commissionAsset="BTC" if market=="spot" else "USDT",time=1000+(self.fill_offset if market=="futures" else 0),isMaker=False,maker=False,isBuyer=r["side"]=="BUY",buyer=r["side"]=="BUY")]


class StoreExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.engine=create_engine("sqlite:///"+self.tmp.name+"/test.db")
        self.store=Store(self.engine);self.store.install();self.vault=Vault(base64.b64encode(b"x"*32).decode())
        self.store.save_account(1,"12345",self.vault.seal(1,"k"*64,"s"*64),"kkkk")
    def tearDown(self):self.engine.dispose();self.tmp.cleanup()
    def intent(self,policy="hold"):
        req=request_body();req["residual_policy"]=policy
        item=self.store.preview(1,self.store.account(1)["version"],req,make_plan(req));self.store.enqueue(1,item["id"])
        return self.store.claim()
    def execute(self,client,policy="hold"):
        item=self.intent(policy);Executor(self.store,client).execute(item,"12345")
        return self.store.get(1,item["id"])

    def test_encrypt_user_binding_and_no_plaintext(self):
        row=self.store.account(1);self.assertNotIn("s"*64,row["sealed"])
        self.assertEqual(self.vault.open(1,row["sealed"])["key"],"k"*64)
        with self.assertRaises(GuardError):self.vault.open(2,row["sealed"])

    def test_duplicate_account_owner_rejected(self):
        with self.assertRaises(GuardError):self.store.save_account(2,"12345","sealed","xxxx")

    def test_single_account_inflight_lock_and_idempotent_confirm(self):
        item=self.intent();self.store.enqueue(1,item["id"])
        other=self.store.preview(1,self.store.account(1)["version"],request_body(),make_plan())
        with self.assertRaises(GuardError):self.store.enqueue(1,other["id"])
        with self.assertRaises(GuardError):self.store.disconnect(1)
        with self.assertRaises(GuardError):self.store.save_account(1,"12345","changed","zzzz")
        self.assertIsNone(self.store.claim())

    def test_expired_preview_never_queues(self):
        item=self.store.preview(1,self.store.account(1)["version"],request_body(),make_plan())
        with patch("binance_hedge.store.time.time",return_value=item["expires_at"]+1),self.assertRaises(GuardError):self.store.enqueue(1,item["id"])

    def test_parallel_fok_both_fill_no_repeat(self):
        c=FakeClient();r=self.execute(c)
        self.assertEqual(r["state"],"HEDGED");self.assertEqual(len(c.calls),2)
        self.assertLessEqual(r["result"]["dispatch_skew_ms"],50)
        self.assertTrue(all(p["timeInForce"]=="FOK" for _,p in c.calls))
        Executor(self.store,c).reconcile(r,r["result"])
        self.assertEqual(len(c.calls),2);self.assertIsNone(self.store.account(1)["active_intent"])

    def test_timeout_after_fill_queries_without_resubmitting(self):
        c=FakeClient();c.unknown={"spot"};r=self.execute(c)
        self.assertEqual(r["state"],"HEDGED");self.assertEqual(len(c.calls),2)

    def test_not_found_after_timeout_remains_unknown_and_blocks_new_order(self):
        c=FakeClient();c.unknown={"spot"};c.query_missing={"spot"};r=self.execute(c)
        self.assertEqual(r["state"],"UNKNOWN");self.assertEqual(self.store.account(1)["active_intent"],r["id"])
        c.query_missing.clear();Executor(self.store,c).reconcile(r,r["result"])
        self.assertEqual(self.store.get(1,r["id"])["state"],"HEDGED");self.assertEqual(len(c.calls),2)

    def test_base_fee_is_net_quantity_and_not_falsely_hedged(self):
        c=FakeClient();c.base_fee=".0001";r=self.execute(c)
        self.assertEqual(r["state"],"ATTENTION");self.assertEqual(dec(r["result"]["residual_qty"]),D("-.0001"))

    def test_single_leg_fill_not_hedged_and_no_missing_leg_purchase(self):
        c=FakeClient();c.zero={"futures"};r=self.execute(c)
        self.assertEqual(r["state"],"ATTENTION");self.assertEqual(len(c.calls),2)
        self.assertEqual(r["result"]["short_qty"],"0")

    def test_bounded_reduction_is_ioc_and_only_reduces_original_fill(self):
        c=FakeClient();c.zero={"spot"};r=self.execute(c,"reduce")
        self.assertEqual(len(c.calls),3)
        market,p=c.calls[-1];self.assertEqual(market,"futures");self.assertEqual(p["side"],"BUY");self.assertEqual(p["reduceOnly"],"true")
        self.assertEqual(p["timeInForce"],"IOC");self.assertLessEqual(dec(p["quantity"]),dec(r["plan"]["quantity"]))
        self.assertEqual(r["state"],"NO_FILL")

    def test_fee_dust_below_lot_does_not_force_minimum_unwind(self):
        c=FakeClient();c.base_fee=".0001";r=self.execute(c,"reduce")
        self.assertEqual(len(c.calls),2);self.assertEqual(r["state"],"ATTENTION")

    def test_incomplete_fills_fail_closed(self):
        c=FakeClient();c.incomplete=True;r=self.execute(c)
        self.assertEqual(r["state"],"UNKNOWN");self.assertNotIn("net_spot_qty",r["result"])

    def test_fill_time_skew_blocks_further_trades_even_when_quantity_equal(self):
        c=FakeClient();c.fill_offset=800;r=self.execute(c)
        self.assertEqual(r["state"],"ATTENTION");self.assertEqual(r["result"]["residual_qty"],"0.000")

    def test_crash_after_durable_dispatch_never_replaces_orders(self):
        c=FakeClient();item=self.intent();p=item["plan"]
        result={"orders":{m:order_params(p,m,item["id"]) for m in ("spot","futures")},"plan":p}
        self.store.state(item["id"],"DISPATCHING",result)
        c.query_missing={"spot","futures"};Executor(self.store,c).reconcile(item,result)
        self.assertEqual(c.calls,[]);self.assertEqual(self.store.get(1,item["id"])["state"],"UNKNOWN")

    def test_mysql_timestamps_have_subsecond_precision(self):
        ddl=str(CreateTable(intents).compile(dialect=mysql.dialect()))
        self.assertIn("expires_at DOUBLE",ddl);self.assertNotIn("expires_at FLOAT",ddl)

    def test_cross_user_intent_cannot_be_read(self):
        item=self.intent()
        with self.assertRaises(GuardError):self.store.get(2,item["id"])

    def test_fok_partial_is_not_accepted_or_automatically_extended(self):
        c=FakeClient();c.partial={'spot':'.05'};r=self.execute(c,'reduce')
        self.assertEqual(r['state'],'ATTENTION');self.assertEqual(len(c.calls),2)
        self.assertIn('非整批',r['result']['reason'])

    def test_depth_thins_after_preflight_sends_nothing(self):
        c=FakeClient();c.depth=lambda market,symbol:book(market=='futures',size='.001')
        r=self.execute(c);self.assertEqual(r['state'],'ABORTED');self.assertEqual(c.calls,[])

    def test_late_socket_dispatch_cancels_late_leg(self):
        c=FakeClient();original=c.submit;c.barrier=threading.Barrier(1)
        def delayed(market,params):
            if market=='futures':time.sleep(.08)
            return original(market,params)
        c.submit=delayed;r=self.execute(c)
        self.assertEqual(len(c.calls),1);self.assertEqual(r['state'],'ATTENTION')
        self.assertTrue(r['result']['futures']['explicit_rejection'])

    def test_invalid_maker_or_limit_violation_is_unknown(self):
        c=FakeClient();original=c.fills
        def bad(market,symbol,ident):
            rows=original(market,symbol,ident);rows[0]['isMaker']=True;return rows
        c.fills=bad;r=self.execute(c)
        self.assertEqual(r['state'],'UNKNOWN');self.assertEqual(len(c.calls),2)

    def test_acknowledgement_requires_exact_terminal_residual(self):
        c=FakeClient();c.base_fee='.0001';r=self.execute(c)
        with self.assertRaises(GuardError):self.store.acknowledge(1,r['id'],'0')
        self.store.acknowledge(1,r['id'],r['result']['residual_qty'])
        self.assertEqual(self.store.get(1,r['id'])['state'],'ACKNOWLEDGED')
        self.assertIsNone(self.store.account(1)['active_intent'])

    def test_cooldown_shared_between_new_store_instances(self):
        self.store.pause_rate(120)
        with self.assertRaises(GuardError):Store(self.engine).check_rate()

    def test_recovery_of_reduction_never_resends(self):
        c=FakeClient();c.zero={'spot'};r=self.execute(c,'reduce')
        Executor(self.store,c).reconcile(r,r['result'])
        self.assertEqual(len(c.calls),3);self.assertEqual(self.store.get(1,r['id'])['state'],'NO_FILL')


if __name__ == "__main__":unittest.main()

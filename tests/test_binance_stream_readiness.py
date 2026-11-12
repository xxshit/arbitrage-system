"""Deterministic stream, deadline and prewarm boundaries. No real credentials."""
import asyncio
from copy import deepcopy
from decimal import Decimal as D
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine

from binance_hedge.budget import BudgetExpired, budget, remaining
from binance_hedge.client import BinanceClient, ExchangeError, OrderGate
from binance_hedge.executor import Executor, final_fill, order_params, verify_fills
from binance_hedge.planner import Book, GuardError
from binance_hedge.store import Store
from binance_hedge.streams import DepthBook, ExecutionStreams, OrderEvents
from binance_hedge.worker import process
from binance_hedge.wakeup import listener, notify, wait as wait_for_work
from test_binance_account_api import PreflightClient
from test_binance_execution_boundaries import Recorder, execute
from test_binance_taker import FakeClient, book, make_plan, request_body, rules


def snapshot():
    return Book.parse(dict(lastUpdateId=100, bids=[['99','10'],['98','20']], asks=[['101','10'],['102','20']]), 10, 10.01)


def delta(market='spot', **changes):
    data=dict(e='depthUpdate', s='BTCUSDT', U=101 if market=='spot' else 99, u=102, pu=98,
              E=10000, b=[['99','12']], a=[['101','8']])
    data.update(changes)
    return data


def event(market, params, *, ident=1, qty=None, cumulative=None, status='FILLED', at=1000, **changes):
    qty = qty if qty is not None else params['quantity']
    row=dict(c=params['newClientOrderId'],s=params['symbol'],S=params['side'],o=params['type'],f=params['timeInForce'],
             q=params['quantity'],p=params['price'],z=cumulative or qty,X=status,i=101,T=at,x='TRADE',t=ident,l=qty,
             L='100' if market=='spot' else '102',n='0',N='USDT',m=False,ps=params.get('positionSide','BOTH'),
             R=params.get('reduceOnly')=='true')
    row.update(changes)
    return dict(e='executionReport',E=at,**row) if market=='spot' else dict(e='ORDER_TRADE_UPDATE',E=at,o=row)


class NoNetwork(unittest.TestCase):
    def setUp(self):
        self.network=patch.object(socket,'create_connection',side_effect=AssertionError('External network forbidden'))
        self.network.start()
    def tearDown(self): self.network.stop()


class DepthScenarios(NoNetwork):
    def test_spot_bridge_absolute_quantity_delete_duplicate_and_gap(self):
        state=DepthBook('spot','BTCUSDT',snapshot())
        self.assertTrue(state.apply(delta(),10.1,.02))
        self.assertEqual(state.current.bids[0],(D(99),D(12)))
        self.assertFalse(state.apply(delta(),10.2,.02))
        self.assertTrue(state.apply(delta(U=103,u=104,b=[['99','0']]),10.3,.02))
        self.assertEqual(state.current.bids[0][0],D(98))
        with self.assertRaises(GuardError):state.apply(delta(U=106,u=107),10.4,.02)

    def test_futures_snapshot_overlap_and_pu_continuity(self):
        state=DepthBook('futures','BTCUSDT',snapshot())
        state.apply(delta('futures'),10.1,.01)
        state.apply(delta('futures',U=103,u=105,pu=102),10.2,.01)
        self.assertEqual(state.current.update_id,105)
        with self.assertRaises(GuardError):state.apply(delta('futures',U=106,u=107,pu=104),10.3,.01)

    def test_no_unobserved_deeper_liquidity_is_invented(self):
        state=DepthBook('spot','BTCUSDT',snapshot())
        with self.assertRaises(GuardError):
            state.apply(delta(b=[['99','0'],['98','0'],['97','5000']]),10.1,0)

    def test_identity_bad_bridge_negative_quantity_and_crossed_book_rejected(self):
        for change in [dict(s='ETHUSDT'),dict(U=103,u=104),dict(b=[['99','-1']]),dict(b=[['102','1']])]:
            with self.subTest(change=change),self.assertRaises(GuardError):
                DepthBook('spot','BTCUSDT',snapshot()).apply(delta(**change),10.1,0)

    def test_capacity_is_bounded_instead_of_trimming_unknown_levels(self):
        state=DepthBook('spot','BTCUSDT',snapshot())
        with self.assertRaises(GuardError):
            state.apply(delta(b=[[str(x),'1'] for x in range(1,2002)]),10.1,0)

    def test_exchange_age_and_clock_expiry_fail_closed(self):
        client=BinanceClient()
        try:
            client._offset={'spot':(0,time.monotonic())}
            with self.assertRaises(GuardError):client.stream_age('spot',int(time.time()*1000)-500)
            client._offset={'spot':(0,time.monotonic()-46)}
            with self.assertRaises(GuardError):client.stream_age('spot',int(time.time()*1000))
        finally:client.close()


class EventScenarios(NoNetwork):
    def setUp(self):
        super().setUp()
        self.params={m:order_params(make_plan(),m,'1'*32) for m in ('spot','futures')}
        self.events=OrderEvents()
        for m,p in self.params.items():self.events.arm(m,p)

    def test_both_markets_complete_exact_fees_without_rest(self):
        for m,p in self.params.items():
            self.events.feed(m,event(m,p,n='.0001',N='BTC' if m=='spot' else 'USDT'))
            client=FakeClient();client.streams=self.events
            client.query=lambda *a: self.fail('complete stream must not query REST')
            fill=final_fill(client,m,p)
            self.assertTrue(fill['fills_verified']);self.assertEqual(fill['evidence_source'],'user_stream')
            self.assertEqual(fill['fees'],{'BTC' if m=='spot' else 'USDT':'0.0001'})

    def test_out_of_order_trade_completes_terminal_without_rollback_and_duplicate_no_double_count(self):
        p=self.params['spot'];half=str(D(p['quantity'])/2)
        last=event('spot',p,ident=2,qty=half,cumulative=p['quantity'],at=1020)
        first=event('spot',p,ident=1,qty=half,cumulative=half,at=1010,status='PARTIALLY_FILLED')
        self.events.feed('spot',last);self.assertIsNone(self.events.evidence('spot',p))
        self.events.feed('spot',first);self.events.feed('spot',first)
        raw,trades=self.events.evidence('spot',p)
        self.assertEqual(raw['status'],'FILLED');self.assertEqual(len(trades),2)
        result=verify_fills('spot',p,raw,trades)
        self.assertEqual(result['first_fill_time_ms'],1010);self.assertEqual(result['last_fill_time_ms'],1020)

    def test_conflicting_trade_id_is_never_usable(self):
        p=self.params['spot'];self.events.feed('spot',event('spot',p))
        self.events.feed('spot',event('spot',p,n='.001'))
        self.assertIsNone(self.events.evidence('spot',p))

    def test_other_orders_not_retained_and_only_three_can_be_armed(self):
        p=dict(self.params['spot'],newClientOrderId='outside')
        for i in range(1500):self.events.feed('spot',event('spot',p,ident=i))
        self.assertEqual(len(self.events.orders),2)
        self.events.arm('spot',p)
        with self.assertRaises(GuardError):self.events.arm('spot',dict(p,newClientOrderId='fourth'))

    def test_wrong_side_symbol_quantity_position_reduce_flag_and_price_are_rejected(self):
        for m,change in [('spot',dict(S='SELL')),('spot',dict(s='ETHUSDT')),('spot',dict(q='999')),
                         ('spot',dict(p='999')),('futures',dict(ps='LONG')),('futures',dict(R=True))]:
            journal=OrderEvents();p=self.params[m];journal.arm(m,p)
            journal.feed(m,event(m,p,**change))
            self.assertIsNone(journal.evidence(m,p))

    def test_maker_and_out_of_limit_fills_still_fail_common_validator(self):
        for change in (dict(m=True),dict(L='999')):
            journal=OrderEvents();p=self.params['spot'];journal.arm('spot',p);journal.feed('spot',event('spot',p,**change))
            with self.assertRaises(GuardError):verify_fills('spot',p,*journal.evidence('spot',p))

    def test_incomplete_event_uses_rest_for_same_id_without_resubmission(self):
        client=FakeClient();p=self.params['spot'];client.barrier=threading.Barrier(1)
        client.submit('spot',p);client.streams=self.events
        half=str(D(p['quantity'])/2)
        self.events.feed('spot',event('spot',p,qty=half,cumulative=p['quantity']))
        fill=final_fill(client,'spot',p)
        self.assertEqual(fill['evidence_source'],'rest');self.assertEqual(len(client.calls),1)

    def test_stream_may_resolve_during_failed_rest_call(self):
        client=FakeClient();client.streams=self.events;p=self.params['spot']
        def query(*a):
            self.events.feed('spot',event('spot',p));raise ExchangeError('NETWORK')
        client.query=query
        fill,error=Executor(Recorder(),client).resolve('spot',p,{'status':'UNKNOWN'})
        self.assertIsNone(error);self.assertTrue(fill['fills_verified']);self.assertEqual(client.calls,[])


class WarmScenarios(NoNetwork):
    def warm_client(self):
        client=PreflightClient();client.warm('BTC/USDT')
        client._offset={m:(0,time.monotonic()) for m in ('spot','futures')}
        class Streams:
            def require_ready(self):pass
            def close(self):pass
        client.streams=Streams();client.fast_mode=True;client.reads.clear()
        self.addCleanup(client.close)
        return client

    def test_warm_confirmation_keeps_seven_fresh_signed_checks_without_rules_fees_clock_or_rest_depth(self):
        client=self.warm_client();client.prepare(request_body())
        self.assertEqual(len(client.reads),7)
        self.assertEqual(set(client.reads),{'/api/v3/account','/sapi/v1/account/apiRestrictions','/api/v3/openOrders',
            '/fapi/v3/account','/fapi/v1/accountConfig','/fapi/v1/openOrders','/fapi/v1/symbolConfig'})

    def test_cached_rules_never_cache_balance_permissions_position_mode_or_open_orders(self):
        for path,change in [('/api/v3/openOrders',[{'orderId':1}]),('/api/v3/account',dict(uid=12345,canTrade=False,balances=[])),
                            ('/fapi/v1/accountConfig',dict(canTrade=True,dualSidePosition=False,multiAssetsMargin=True))]:
            client=self.warm_client();client.payloads[path]=change
            with self.assertRaises(GuardError):client.prepare(request_body())

    def test_metadata_expired_or_disconnected_user_stream_never_falls_back_to_cold_submission(self):
        client=self.warm_client();symbol,at,rows=client._metadata;client._metadata=(symbol,at-31,rows)
        with self.assertRaises(GuardError):client.prepare(request_body())
        client._metadata=(symbol,at,rows);client.streams.require_ready=lambda: (_ for _ in ()).throw(GuardError('disconnected'))
        with self.assertRaises(GuardError):client.prepare(request_body())

    def test_stream_subscription_is_exact_allowlist_and_never_enables_order_writes(self):
        client=BinanceClient('synthetic','synthetic')
        self.addCleanup(client.close)
        for path,method in [('/fapi/v1/listenKey','DELETE'),('/fapi/v1/order','POST'),('/fapi/v1/leverage','POST')]:
            with self.assertRaises(GuardError):client.request('futures',path,method=method)


class DeadlineScenarios(NoNetwork):
    def test_stream_loss_after_persisting_reduction_blocks_its_wire_write(self):
        client=FakeClient();client.zero={'spot'}
        out,store,intent=execute(client)
        intent['request']['residual_policy']='reduce'
        # Give the synthetic confirmed plan the explicitly accepted exit cap;
        # a hold-only plan correctly blocks before reaching the wire guard.
        out['result']['plan']['exit_slippage_bps']=intent['request']['exit_slippage_bps']
        lost=threading.Event()
        class Streams:
            def arm(self,*a):pass
            def evidence(self,*a):return None
            def book(self,*a):
                if lost.is_set():raise GuardError('stream lost before IOC write')
                return book(True)
        client.streams=Streams();original=client.submit
        def submit(m,p,**kwargs):
            if p['timeInForce']=='IOC':
                gate=kwargs['gate'];self.assertLessEqual(gate.deadline-time.monotonic(),.4)
                # The transport treats a failed pre-write guard as proven zero.
                with self.assertRaises(GuardError):gate.before_write(kwargs['timing'])
                raise ExchangeError('SEND_GUARD',uncertain=False)
            return original(m,p,**kwargs)
        client.submit=submit
        store.on_save=lambda state:lost.set() if state['state']=='REDUCING' else None
        Executor(store,client).reduce(intent,out['result'],D(out['result']['residual_qty']),
                                     (rules(),rules(futures=True)),end=time.monotonic()+1)
        self.assertEqual(len(client.calls),2)
        result=store.states[-1]
        self.assertEqual(result['state'],'ATTENTION');self.assertFalse(result['release'])
        self.assertTrue(result['result']['reduction_fill']['explicit_rejection'])
        self.assertTrue(result['result']['futures']['fills_verified'])

    def test_slow_trickle_read_is_interrupted_at_absolute_budget(self):
        stopped=threading.Event()
        class Sock:
            def settimeout(self,value):pass
            def shutdown(self,how):stopped.set()
        class Connection:
            def __init__(self,*a,**k):self.sock=Sock()
            def request(self,*a,**k):pass
            def getresponse(self):return self
            def read(self,*a):
                # Every simulated byte arrives faster than a socket timeout;
                # only the fixed watchdog can terminate this response.
                while not stopped.wait(.005):pass
                raise OSError('closed')
            def close(self):pass
        client=BinanceClient();self.addCleanup(client.close);start=time.monotonic()
        with patch('binance_hedge.client.OrderConnection',Connection),budget(start+.08),self.assertRaises(ExchangeError):
            client.request('spot','/api/v3/time')
        self.assertTrue(stopped.is_set());self.assertLess(time.monotonic()-start,.5)

    def test_lock_wait_and_parallel_tasks_share_budget(self):
        client=BinanceClient();self.addCleanup(client.close)
        client._locks['spot'].acquire();start=time.monotonic()
        try:
            with budget(start+.04),self.assertRaises(BudgetExpired):client.request('spot','/api/v3/time')
        finally:client._locks['spot'].release()
        self.assertLess(time.monotonic()-start,.3)
        with budget(time.monotonic()+.1):
            a,b=client.parallel(lambda:remaining(100),lambda:remaining(100))
        self.assertLessEqual(max(a,b),.1)

    def test_stale_confirmation_does_no_preflight_or_orders(self):
        client=FakeClient();store=Recorder()
        intent=dict(id='a'*32,request=request_body(),plan=make_plan(),expires_at=time.time()+15,result={'confirmed_at':time.time()-3})
        client.prepare=lambda *a,**k:self.fail('stale confirmation must abort before preparation')
        Executor(store,client).execute(intent,'12345')
        self.assertEqual(store.states[-1]['state'],'ABORTED');self.assertEqual(client.calls,[])

    def test_one_leg_beyond_total_deadline_preserves_verified_peer_and_never_reduces(self):
        client=FakeClient();store=Recorder();executor=Executor(store,client)
        intent=dict(id='a'*32,request=request_body(residual_policy='reduce'),plan=make_plan(request_body(residual_policy='reduce')),expires_at=time.time()+15)
        prepared=executor.prepare_execution(intent,'12345')
        finish=threading.Event();original=executor.resolve
        def resolve(m,p,s,end=None):
            if m=='spot':finish.wait(.5)
            return original(m,p,s,end)
        executor.resolve=resolve;start=time.monotonic()
        try:executor.dispatch(intent,*prepared, time.monotonic()+.2,time.monotonic()+.08)
        finally:finish.set()
        self.assertLess(time.monotonic()-start,.4)
        result=store.states[-1]
        self.assertEqual(result['state'],'UNKNOWN');self.assertFalse(result['release'])
        self.assertTrue(result['result']['futures']['fills_verified']);self.assertEqual(len(client.calls),2)

    def test_failure_between_arming_and_send_blocks_gate(self):
        gate=OrderGate(time.monotonic()+1,parties=1,check=lambda: (_ for _ in ()).throw(GuardError('stream lost')))
        timing={};gate.ready()
        with self.assertRaises(GuardError):gate.before_write(timing)
        self.assertNotIn('write_started_monotonic',timing)


class StoreWarmScenarios(NoNetwork):
    def setUp(self):
        super().setUp();self.tmp=tempfile.TemporaryDirectory()
        self.engine=create_engine('sqlite:///'+self.tmp.name+'/db');self.store=Store(self.engine);self.store.install()
        self.store.save_account(1,'12345','sealed','xxxx');self.store.beat('boot')
        self.item=self.store.preview(1,self.store.account(1)['version'],request_body(),make_plan())
    def tearDown(self):self.engine.dispose();self.tmp.cleanup();super().tearDown()

    def test_no_warm_foreign_boot_stale_warm_fail_closed_then_exact_confirm_is_idempotent(self):
        with self.assertRaises(GuardError):self.store.enqueue(1,self.item['id'])
        self.store.prepared(self.item['id'],'old-boot',ready=True)
        with self.assertRaises(GuardError):self.store.enqueue(1,self.item['id'])
        self.store.prepared(self.item['id'],'boot',ready=True)
        with patch('binance_hedge.store.time.time',return_value=time.time()+11),self.assertRaises(GuardError):self.store.enqueue(1,self.item['id'])
        self.store.enqueue(1,self.item['id']);self.store.enqueue(1,self.item['id'])
        item=self.store.claim();self.assertIsNotNone(item['result']['confirmed_at']);self.assertIsNone(self.store.claim())
        self.store.prepared(item['id'],'late',ready=False)
        self.assertEqual(self.store.get(1,item['id'])['state'],'CHECKING')

    def test_preview_selection_bounded_and_restart_cannot_cold_send(self):
        self.assertEqual(self.store.latest_preview()['id'],self.item['id'])
        self.store.prepared(self.item['id'],'boot',ready=True);self.store.enqueue(1,self.item['id']);item=self.store.claim()
        self.assertIsNone(self.store.latest_preview())
        with patch.dict(os.environ,{'BINANCE_EXECUTION_ENABLED':'1'}):process(self.store,None,item)
        self.assertEqual(self.store.get(1,item['id'])['state'],'ABORTED')
        self.assertIsNone(self.store.account(1)['active_intent'])


class StreamLifecycleScenarios(NoNetwork):
    def test_four_channels_bridge_deliver_events_latch_disconnect_and_close_without_threads(self):
        disconnected=threading.Event();holder=[];urls=[];sent=[]
        class Client:
            def request(self,market,path,params=None,**kwargs):
                if path.endswith('listenKey'):
                    return {'listenKey':'synthetic-token-123456789'}
                return dict(lastUpdateId=100,bids=[['99','100']],asks=[['103','100']])
            def stream_age(self,*a):return 0
            def stream_signature(self):return {'apiKey':'synthetic','signature':'synthetic'}
        class WS:
            def __init__(self,url):self.url=url;self.n=0;self.delivered=set()
            async def __aenter__(self):return self
            async def __aexit__(self,*a):pass
            async def send(self,payload):sent.append(json.loads(payload))
            async def ping(self):
                done=asyncio.get_running_loop().create_future();done.set_result(0);return done
            def __aiter__(self):return self
            async def __anext__(self):
                await asyncio.sleep(.01)
                if disconnected.is_set():raise StopAsyncIteration
                if '@depth' in self.url:
                    market='futures' if '/public/' in self.url else 'spot'
                    data=delta(market,U=(101 if market=='spot' else 99) if not self.n else 101+2*self.n,
                               u=102+2*self.n,pu=100+2*self.n,b=[['99','100']],a=[['103','100']])
                    self.n+=1;return json.dumps(data)
                market='spot' if 'ws-api.binance.com' in self.url else 'futures'
                if market=='spot' and not self.n:
                    self.n+=1;return json.dumps(dict(id='subscribe',status=200,result={'subscriptionId':0}))
                if holder:
                    for (m,ident),item in list(holder[0].events.orders.items()):
                        if m==market and ident not in self.delivered:
                            self.delivered.add(ident);data=event(m,item['params'])
                            return json.dumps(dict(subscriptionId=0,event=data) if m=='spot' else data)
                return json.dumps(dict(subscriptionId=0,event={'e':'outboundAccountPosition'}) if market=='spot' else {'e':'ACCOUNT_UPDATE'})
        def connect(url,**options):
            urls.append(url)
            self.assertIsNone(options['compression']);self.assertLessEqual(options['max_queue'],8)
            return WS(url)
        with patch('binance_hedge.streams.connect',connect):
            stream=ExecutionStreams(Client(),'BTC/USDT');holder.append(stream)
            try:
                end=time.monotonic()+1
                while True:
                    try:stream.book('spot','BTCUSDT');stream.book('futures','BTCUSDT');break
                    except GuardError:
                        if time.monotonic()>end:self.fail('synthetic streams failed to become ready')
                        time.sleep(.005)
                p=order_params(make_plan(),'spot','2'*32);stream.arm('spot',p)
                while stream.evidence('spot',p) is None and time.monotonic()<end:time.sleep(.005)
                self.assertIsNotNone(stream.evidence('spot',p))
                disconnected.set()
                while not stream.fault and time.monotonic()<end:time.sleep(.005)
                with self.assertRaises(GuardError):stream.book('spot','BTCUSDT')
                self.assertIsNotNone(stream.evidence('spot',p))
                self.assertTrue(any('/public/ws/' in u for u in urls));self.assertTrue(any('/private/ws/' in u for u in urls))
                self.assertEqual(sent[0]['method'],'userDataStream.subscribe.signature')
            finally:stream.close()
        self.assertFalse(stream.thread.is_alive())
        self.assertFalse(any(t.name.startswith('binance-stream-io') for t in threading.enumerate()))

    def test_local_wakeup_has_no_account_payload_and_lost_hint_is_safe(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'BINANCE_EXECUTOR_LOCK':folder+'/executor.lock'}):
            notify()  # absent worker must not fail a committed DB confirmation
            with listener() as sock:
                # No network egress; private AF_UNIX endpoint only.
                notify();start=time.monotonic();wait_for_work(sock)
                self.assertLess(time.monotonic()-start,.1)
                self.assertEqual(os.stat(folder+'/wake.sock').st_mode&0o777,0o600)

    def test_recovery_deadline_is_shared_including_reduction_without_resends(self):
        client=FakeClient();out,store,intent=execute(client)
        original=client.query;finish=threading.Event()
        def query(m,*a):
            if m=='spot':finish.wait(.5)
            return original(m,*a)
        client.query=query;start=time.monotonic()
        try:
            with patch('binance_hedge.executor.RECOVERY_BUDGET_SECONDS',.06):Executor(store,client).reconcile(intent,out['result'])
        finally:finish.set()
        self.assertLess(time.monotonic()-start,.3)
        self.assertEqual(store.states[-1]['state'],'UNKNOWN');self.assertEqual(len(client.calls),2)
        self.assertTrue(store.states[-1]['result']['futures']['fills_verified'])


if __name__=='__main__':unittest.main()

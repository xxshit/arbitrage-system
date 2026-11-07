"""Short fault scenarios; real HTTP framing with memory sockets, no exchange IO."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import io
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from binance_hedge.client import BinanceClient, ExchangeError, OrderConnection, OrderGate
from binance_hedge.executor import Executor
from binance_hedge.planner import Book, GuardError, plan_clip, reduction_plan
from test_binance_taker import FakeClient, book, make_plan, request_body, rules


class Recorder:
    def __init__(self, on_save=None): self.states=[]; self.on_save=on_save
    def state(self, ident, state, result, release=False):
        snapshot=dict(state=state, result=deepcopy(result), release=release)
        self.states.append(snapshot)
        if self.on_save: self.on_save(snapshot)


def execute(client, *, request=None, store=None):
    request=request or request_body()
    intent=dict(id='a'*32, request=request, plan=make_plan(request), expires_at=time.time()+15)
    store=store or Recorder()
    Executor(store, client).execute(intent, '12345')
    return store.states[-1], store, intent


class MemoryConnection(OrderConnection):
    """No DNS/TCP/TLS. Exercise the stdlib request and response state machine."""
    def __init__(self, host, *, delay=0, fail_connect=False, fail_write=False, write_delay=0):
        super().__init__(host, timeout=2)
        self.delay=delay; self.fail_connect=fail_connect; self.fail_write=fail_write; self.write_delay=write_delay
        self.connect_calls=0; self.writes=[]; self.connected_at=None; self.params=None

    def connect(self):
        self.connect_calls+=1
        if self.delay: time.sleep(self.delay)
        if self.fail_connect: raise TimeoutError('synthetic connect timeout')
        self.connected_at=time.monotonic()
        owner=self
        class MemorySocket:
            def settimeout(self, value): pass
            def sendall(self, data):
                if owner.write_delay: time.sleep(owner.write_delay)
                owner.writes.append((time.monotonic(), bytes(data)))
                if owner.fail_write: raise TimeoutError('synthetic prefix may have been sent')
                head, body=data.split(b'\r\n\r\n',1)
                content_length=[int(row.split(b':',1)[1]) for row in head.split(b'\r\n') if row.lower().startswith(b'content-length:')][0]
                assert content_length==len(body)
                owner.params={k:v[0] for k,v in parse_qs(body.decode()).items()}
            def makefile(self, mode):
                p=owner.params
                data=json.dumps(dict(clientOrderId=p.get('newClientOrderId','synthetic'),symbol=p.get('symbol','BTCUSDT'),side=p.get('side','BUY'),
                                     orderId=101 if owner.host=='api.binance.com' else 102,executedQty=p.get('quantity','0'),status='FILLED',updateTime=1000)).encode()
                return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(data)).encode()+b'\r\n\r\n'+data)
            def close(self): pass
        self.sock=MemorySocket()


class TransportScenarios(unittest.TestCase):
    def setUp(self):
        self.network=patch.object(socket,'create_connection',side_effect=AssertionError('External network forbidden'))
        self.network.start()
        self.client=BinanceClient('synthetic-key','synthetic-secret',allow_orders=True)
        self.client._offset={m:(0,time.monotonic()) for m in ('spot','futures')}
    def tearDown(self): self.client.close(); self.network.stop()
    def pair(self, spot, future, ttl=1):
        self.client._connections={'spot':spot,'futures':future}
        gate=OrderGate(time.monotonic()+ttl)
        timing={m:{} for m in ('spot','futures')}
        def send(market):
            try:
                self.client.submit(market,dict(symbol='BTCUSDT',side='BUY' if market=='spot' else 'SELL',type='LIMIT',timeInForce='FOK',
                                              price='100',quantity='.1',newClientOrderId='test-'+market),gate=gate,timing=timing[market])
                return None
            except ExchangeError as exc: return exc.uncertain
        errors=self.client.parallel(lambda:send('spot'),lambda:send('futures'))
        return timing, errors

    def test_200ms_reconnect_finishes_before_either_order_writes(self):
        s=MemoryConnection('api.binance.com'); f=MemoryConnection('fapi.binance.com',delay=.2)
        timing,errors=self.pair(s,f)
        self.assertEqual(errors,(None,None))
        self.assertEqual((len(s.writes),len(f.writes)),(1,1))
        self.assertGreaterEqual(min(s.writes[0][0],f.writes[0][0]),max(s.connected_at,f.connected_at))
        self.assertLess(abs(s.writes[0][0]-f.writes[0][0]),.05)
        for m,c in [('spot',s),('futures',f)]:
            self.assertLessEqual(timing[m]['write_started_monotonic'],c.writes[0][0])
            self.assertGreaterEqual(timing[m]['write_completed_monotonic'],c.writes[0][0])
            self.assertEqual(c.params['timeInForce'],'FOK')

    def test_failed_connect_aborts_both_without_order_bytes(self):
        s=MemoryConnection('api.binance.com'); f=MemoryConnection('fapi.binance.com',fail_connect=True)
        timing,errors=self.pair(s,f)
        self.assertEqual(errors,(False,False)); self.assertEqual(s.writes+f.writes,[])
        self.assertTrue(all('write_started_monotonic' not in t for t in timing.values()))

    def test_preparation_past_book_deadline_aborts_both(self):
        s=MemoryConnection('api.binance.com'); f=MemoryConnection('fapi.binance.com',delay=.08)
        _,errors=self.pair(s,f,ttl=.03)
        self.assertEqual(errors,(False,False)); self.assertEqual(s.writes+f.writes,[])

    def test_failed_signing_aborts_prepared_peer(self):
        self.client._offset.pop('futures')
        s=MemoryConnection('api.binance.com'); f=MemoryConnection('fapi.binance.com')
        _,errors=self.pair(s,f)
        self.assertEqual(errors,(False,False)); self.assertEqual(s.writes+f.writes,[])

    def test_closed_socket_during_staging_never_reconnects_or_sends_peer(self):
        class ClosedDuringStage(MemoryConnection):
            def stage_order(self,*args):
                wire=super().stage_order(*args); self.sock=None; return wire
        s=MemoryConnection('api.binance.com'); f=ClosedDuringStage('fapi.binance.com')
        _,errors=self.pair(s,f)
        self.assertEqual(errors,(False,False)); self.assertEqual(s.writes+f.writes,[])
        self.assertEqual(f.connect_calls,1)

    def test_write_failure_stays_unknown_and_is_not_retried(self):
        f=MemoryConnection('fapi.binance.com',fail_write=True)
        self.client._connections={'futures':f}; timing={}
        with self.assertRaises(ExchangeError) as exc:
            self.client.submit('futures',{},gate=OrderGate(time.monotonic()+1,parties=1),timing=timing)
        self.assertTrue(exc.exception.uncertain); self.assertEqual(len(f.writes),1); self.assertEqual(f.connect_calls,1)
        self.assertIn('write_started_monotonic',timing); self.assertNotIn('write_completed_monotonic',timing)

    def test_write_stall_is_measured_in_full_request_span(self):
        s=MemoryConnection('api.binance.com'); f=MemoryConnection('fapi.binance.com',write_delay=.08)
        timing,errors=self.pair(s,f)
        self.assertEqual(errors,(None,None))
        result={'dispatch':timing}; Executor.timing(result)
        self.assertGreaterEqual(result['request_write_span_ms'],80)

    def test_prepared_connection_is_reused_without_extra_connect(self):
        s=MemoryConnection('api.binance.com'); self.client._connections={'spot':s}
        for _ in range(2): self.client.submit('spot',{'quantity':'.1'})
        self.assertEqual(s.connect_calls,1); self.assertEqual(len(s.writes),2)


class MatchClient(FakeClient):
    def __init__(self):
        super().__init__(); self.trades={}; self.exit_book=None; self.ioc_available=None
    def depth(self, market, symbol):
        if self.exit_book is not None and len(self.calls)>=2: return self.exit_book
        return super().depth(market,symbol)
    def submit(self, market, params, **kwargs):
        if not params['newClientOrderId'].endswith('r'): return super().submit(market,params,**kwargs)
        gate,timing=kwargs['gate'],kwargs['timing']
        gate.ready(); gate.before_write(timing); timing['write_completed_monotonic']=time.monotonic()
        self.calls.append((market,deepcopy(params)))
        depth=self.depth(market,params['symbol']); buy=params['side']=='BUY'
        remaining=D(params['quantity']); rows=[]
        for price,size in (depth.asks if buy else depth.bids):
            if (price>D(params['price']) if buy else price<D(params['price'])): break
            take=min(size,remaining,self.ioc_available if self.ioc_available is not None else remaining)
            if take<=0: break
            rows.append(dict(id=900+len(rows),orderId=999,symbol=params['symbol'],qty=str(take),price=str(price),commission='0',commissionAsset='USDT',
                             time=1200,isMaker=False,maker=False,isBuyer=buy,buyer=buy))
            remaining-=take
            if remaining==0 or self.ioc_available is not None: break
        qty=D(params['quantity'])-remaining
        raw=dict(clientOrderId=params['newClientOrderId'],symbol=params['symbol'],side=params['side'],orderId=999,
                 executedQty=str(qty),status='FILLED' if remaining==0 else 'EXPIRED')
        self.records[(market,params['newClientOrderId'])]=raw; self.trades[market]=rows
        return raw
    def fills(self, market, symbol, order_id):
        if str(order_id)=='999': return deepcopy(self.trades[market])
        return super().fills(market,symbol,order_id)


class ExecutionScenarios(unittest.TestCase):
    def setUp(self):
        self.network=patch.object(socket,'create_connection',side_effect=AssertionError('External network forbidden')); self.network.start()
    def tearDown(self): self.network.stop()

    def test_future_unwind_crosses_unchanged_ask_using_actual_fill(self):
        c=MatchClient(); c.zero={'spot'}
        out,_,_=execute(c,request=request_body(residual_policy='reduce'))
        self.assertEqual(out['state'],'NO_FILL'); self.assertEqual(len(c.calls),3)
        r=out['result']['reduction']; self.assertEqual(r['params']['price'],'102.01')
        self.assertEqual(r['depth_check']['reference_fill_price'],'102')
        self.assertEqual(out['result']['residual_qty'],'0.000')

    def test_spot_unwind_crosses_bid_without_inventing_base_fee_reserve(self):
        c=MatchClient(); c.zero={'futures'}
        out,_,_=execute(c,request=request_body(residual_policy='reduce'))
        self.assertEqual(out['state'],'NO_FILL'); self.assertEqual(len(c.calls),3)
        self.assertEqual(out['result']['reduction']['params']['price'],'99.99')
        self.assertEqual(D(out['result']['reduction_fill']['executed_qty']),D('.196'))
        self.assertEqual(D(out['result']['residual_qty']),D('0'))

    def test_reverse_book_outside_budget_sends_no_reduction(self):
        c=MatchClient(); c.zero={'spot'}; t=time.monotonic()
        c.exit_book=Book.parse(dict(lastUpdateId=2,bids=[['110','100']],asks=[['110.01','100']]),t,t)
        out,_,_=execute(c,request=request_body(residual_policy='reduce'))
        self.assertEqual(out['state'],'ATTENTION'); self.assertEqual(len(c.calls),2)
        self.assertNotIn('reduction',out['result']); self.assertIn('反向深度',out['result']['reason'])

    def test_spot_base_fee_is_subtracted_once_and_rounding_dust_is_retained(self):
        c=MatchClient(); c.zero={'futures'}; c.base_fee='.0001'
        out,_,_=execute(c,request=request_body(residual_policy='reduce'))
        self.assertEqual(out['state'],'ATTENTION'); self.assertEqual(len(c.calls),3)
        self.assertEqual(D(out['result']['reduction_fill']['executed_qty']),D('.195'))
        self.assertEqual(D(out['result']['residual_qty']),D('.0009'))

    def test_missing_sell_fee_or_increased_confirmed_fee_blocks_opening(self):
        req=request_body(residual_policy='reduce'); anchor=make_plan(req)
        with self.assertRaisesRegex(GuardError,'卖出手续费'):
            plan_clip(req,rules(),rules(futures=True),book(),book(True),time.monotonic(),D('.001'),D('.0005'))
        with self.assertRaisesRegex(GuardError,'手续费高于确认'):
            plan_clip(req,rules(),rules(futures=True),book(),book(True),time.monotonic(),D('.001'),D('.0005'),anchor,spot_exit_fee=D('.002'))

    def test_reverse_depth_covers_full_qty_when_entry_fee_uses_bnb(self):
        t=time.monotonic()
        thin=Book.parse(dict(lastUpdateId=1,bids=[['99.99','.195']],asks=[['100','100']]),t,t)
        with self.assertRaisesRegex(GuardError,'反向深度不足'):
            plan_clip(request_body(residual_policy='reduce'),rules(),rules(futures=True),thin,book(True),time.monotonic(),D('.001'),D('.0005'),spot_exit_fee=D('.001'))

    def test_bnb_base_rejects_automatic_exit_before_any_order(self):
        sr=replace(rules(),symbol='BNBUSDT',base='BNB');fr=replace(rules(futures=True),symbol='BNBUSDT',base='BNB')
        with self.assertRaisesRegex(GuardError,'BNB 手续费代扣'):
            plan_clip(request_body(symbol='BNB/USDT',residual_policy='reduce'),sr,fr,book(),book(True),time.monotonic(),D('.001'),D('.0005'),spot_exit_fee=D('.001'))

    def test_bnb_base_can_only_preview_with_manual_residual_handling(self):
        sr=replace(rules(),symbol='BNBUSDT',base='BNB');fr=replace(rules(futures=True),symbol='BNBUSDT',base='BNB')
        p=plan_clip(request_body(symbol='BNB/USDT',residual_policy='hold'),sr,fr,book(),book(True),time.monotonic(),D('.001'),D('.0005'),spot_exit_fee=D('.001'))
        self.assertIsNone(p['exit_preview'])

    def test_stale_reverse_book_does_not_send(self):
        c=MatchClient(); c.zero={'spot'}; c.exit_book=book(True,age=2)
        out,_,_=execute(c,request=request_body(residual_policy='reduce'))
        self.assertEqual(out['state'],'ATTENTION'); self.assertEqual(len(c.calls),2)

    def test_partial_ioc_is_exact_and_never_chased(self):
        c=MatchClient(); c.zero={'spot'}; c.ioc_available=D('.05')
        out,store,intent=execute(c,request=request_body(residual_policy='reduce'))
        self.assertEqual(out['state'],'ATTENTION'); self.assertEqual(D(out['result']['residual_qty']),D('-.146'))
        Executor(store,c).reconcile(intent,out['result'])
        self.assertEqual(len(c.calls),3); self.assertEqual(D(store.states[-1]['result']['residual_qty']),D('-.146'))

    def test_missing_exit_authorization_is_not_invented(self):
        request=request_body(residual_policy='reduce'); request.pop('exit_slippage_bps')
        with self.assertRaisesRegex(GuardError,'缺少已确认'): make_plan(request)

    def test_preview_refuses_unwind_that_cannot_cross_in_budget(self):
        with self.assertRaises(GuardError): make_plan(request_body(residual_policy='reduce',exit_slippage_bps='0'))
        p=make_plan(request_body(residual_policy='reduce'))
        self.assertEqual(p['exit_slippage_bps'],'20'); self.assertEqual(p['exit_fee_bps']['spot'],'10.000')

    def test_preflight_refuses_subminimum_exit_instead_of_rounding_up(self):
        with self.assertRaises(GuardError):
            plan_clip(request_body(residual_policy='reduce'),rules('.1'),rules('.1',futures=True),book(),book(True),time.monotonic(),D('.001'),D('.0005'),spot_exit_fee=D('.001'))

    def test_spot_query_failure_does_not_skip_futures_or_release_account(self):
        c=FakeClient(); c.unknown={'spot'}; c.query_missing={'spot'}
        out,store,intent=execute(c)
        self.assertEqual(out['state'],'UNKNOWN'); self.assertFalse(out['release'])
        self.assertTrue(out['result']['futures']['fills_verified']); self.assertIn('spot',out['result']['reconciliation_errors'])
        self.assertNotIn('residual_qty',out['result']); self.assertEqual(len(c.calls),2)
        c.query_missing.clear(); Executor(store,c).reconcile(intent,out['result'])
        self.assertEqual(store.states[-1]['state'],'HEDGED'); self.assertEqual(len(c.calls),2)

    def test_clock_failure_is_isolated_to_its_market(self):
        c=FakeClient()
        def clock(market):
            if market=='spot': raise GuardError('synthetic clock failure')
        c.ensure_clock=clock
        out,_,_=execute(c)
        self.assertEqual(out['state'],'UNKNOWN'); self.assertTrue(out['result']['futures']['fills_verified'])
        self.assertEqual(set(out['result']['reconciliation_errors']),{'spot'})

    def test_fast_leg_is_saved_before_other_submission_response_returns(self):
        saved=threading.Event(); released=[]
        class DelayedResponse(FakeClient):
            def submit(self,market,params,**kwargs):
                raw=super().submit(market,params,**kwargs)
                if market=='futures': released.append(saved.wait(timeout=.5))
                return raw
        def on_save(snapshot):
            if snapshot['result'].get('spot',{}).get('fills_verified'): saved.set()
        out,store,_=execute(DelayedResponse(),store=Recorder(on_save))
        self.assertEqual(released,[True]); self.assertEqual(out['state'],'HEDGED')
        self.assertTrue(any(s['result'].get('spot',{}).get('fills_verified') and 'futures' not in s['result'] for s in store.states))

    def test_complete_write_span_over_limit_blocks_next_clip(self):
        c=FakeClient(); original=c.submit
        def slow(market,params,**kwargs):
            raw=original(market,params,**kwargs)
            if market=='futures': kwargs['timing']['write_completed_monotonic']+=.08
            return raw
        c.submit=slow
        out,_,_=execute(c)
        self.assertEqual(out['state'],'ATTENTION'); self.assertFalse(out['release'])
        self.assertGreater(out['result']['request_write_span_ms'],50)


if __name__=='__main__': unittest.main()

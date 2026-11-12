import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from flask import Flask, g, session
from sqlalchemy import create_engine

from binance_hedge.client import BinanceClient, ExchangeError
from binance_hedge.planner import GuardError
from binance_hedge.store import Store, Vault
from binance_hedge.web import register
from test_binance_taker import make_plan, rules
from copy import deepcopy
from dataclasses import asdict
from test_binance_taker import book, request_body


class AccountFake:
    def __init__(self,key,secret,allow_orders=False):
        assert not allow_orders
    def close(self):pass
    def accounts(self):
        return (dict(uid=12345,balances=[dict(asset="USDT",free="1000",locked="0")]),
                dict(assets=[dict(asset="USDT",walletBalance="500",availableBalance="400")],positions=[]),
                dict(enableSpotAndMarginTrading=True,enableFutures=True))
    def prepare(self,body):return make_plan(body),rules(),rules(futures=True)


class AccountApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.engine=create_engine("sqlite:///"+self.tmp.name+"/test.db")
        self.store=Store(self.engine);self.store.install();self.vault=Vault(base64.b64encode(b"k"*32).decode())
        self.app=Flask(__name__);self.app.config.update(TESTING=True,SECRET_KEY="synthetic-only")
        @self.app.before_request
        def auth():g.user=SimpleNamespace(id=session["user_id"],role=session.get("role","admin")) if session.get("user_id") else None
        db=SimpleNamespace(engine=self.engine,session=SimpleNamespace(remove=lambda:None))
        register(self.app,db,lambda:getattr(g,"user",None),client_factory=AccountFake,vault_factory=lambda:self.vault)
        self.client=self.app.test_client();self.headers={"X-CSRF-Token":"csrf"}
        self.login()
    def login(self,role="admin",user_id=1):
        with self.client.session_transaction() as s:s.update(user_id=user_id,role=role,csrf_token="csrf")
    def tearDown(self):self.engine.dispose();self.tmp.cleanup()
    def connect(self):return self.client.post('/api/binance/account',json=dict(api_key="k"*64,api_secret="s"*64),headers=self.headers)
    def preview(self):
        from test_binance_taker import request_body
        return self.client.post('/api/binance/preview',json=request_body(),headers=self.headers)
    def test_credentials_encrypted_and_never_returned(self):
        r=self.connect();self.assertEqual(r.status_code,200)
        r=self.client.get('/api/binance/account');body=r.get_data(as_text=True)
        self.assertNotIn('s'*64,body);self.assertNotIn('sealed',body);self.assertNotIn('uid_hash',body)
        self.assertIn('no-store',r.headers['Cache-Control']);self.assertTrue(r.json['connected'])
    def test_reads_need_auth_and_writes_need_nonempty_csrf(self):
        self.assertEqual(self.client.post('/api/binance/account',json={}).status_code,403)
        with self.client.session_transaction() as s:s.clear()
        self.assertEqual(self.client.get('/api/binance/account').status_code,401)
        self.login('viewer');self.assertEqual(self.client.get('/api/binance/account').status_code,403)
    def test_cross_user_status_isolated(self):
        self.connect();self.login(user_id=2)
        self.assertFalse(self.client.get('/api/binance/account').json['connected'])
    def test_preview_only_and_global_execution_default_off(self):
        self.connect();r=self.preview();self.assertEqual(r.status_code,200)
        ident=r.json['intent']['id'];self.assertNotIn('account_uid',r.json['intent']['plan'])
        r=self.client.post(f'/api/binance/intents/{ident}/confirm',json=dict(intent_id=ident,confirm='确认双腿限价FOK和单腿风险'),headers=self.headers)
        self.assertEqual(r.status_code,409);self.assertEqual(self.store.get(1,ident)['state'],'PREVIEW')
    def test_explicit_confirmation_and_healthy_worker_required(self):
        self.connect();item=self.preview().json['intent'];self.store.beat('fixture')
        self.store.prepared(item['id'],'fixture',ready=True)
        with patch.dict(os.environ,{'BINANCE_EXECUTION_ENABLED':'1'}):
            path=f"/api/binance/intents/{item['id']}/confirm"
            self.assertEqual(self.client.post(path,json={},headers=self.headers).status_code,409)
            payload=dict(intent_id=item['id'],confirm='确认双腿限价FOK和单腿风险')
            self.assertEqual(self.client.post(path,json=payload,headers=self.headers).status_code,202)
            self.assertEqual(self.client.post(path,json=payload,headers=self.headers).status_code,202)
        self.assertEqual(self.store.get(1,item['id'])['state'],'QUEUED')
    def test_account_refresh_cannot_place_orders_and_throttles(self):
        self.connect()
        self.assertEqual(self.client.post('/api/binance/account/refresh',json={},headers=self.headers).status_code,200)
        self.assertEqual(self.client.post('/api/binance/account/refresh',json={},headers=self.headers).status_code,409)
    def test_https_required_for_non_test_credential_submission(self):
        self.app.testing=False;self.assertEqual(self.connect().status_code,409)
    def test_validation_no_echo_of_bad_key(self):
        r=self.client.post('/api/binance/account',json=dict(api_key='secret-invalid',api_secret='SECRET'),headers=self.headers)
        self.assertEqual(r.status_code,409);self.assertNotIn('secret-invalid',r.get_data(as_text=True))


class Response:
    def __init__(self,payload,status=200):self.payload=payload;self.status=status
    def read(self,n):return json.dumps(self.payload).encode()
    def getheader(self,*a):return '60'


class PreflightClient(BinanceClient):
    """Official response shapes; no HTTP, DB, live balances or credentials."""
    def __init__(self):
        super().__init__();self.reads=[]
        symbol=dict(symbol='BTCUSDT',baseAsset='BTC',quoteAsset='USDT',status='TRADING',isSpotTradingAllowed=True,
                    contractType='PERPETUAL',marginAsset='USDT',orderTypes=['LIMIT'],timeInForce=['IOC','FOK'],
                    filters=[dict(filterType='PRICE_FILTER',tickSize='.01',minPrice='.01',maxPrice='1000000'),
                             dict(filterType='LOT_SIZE',stepSize='.001',minQty='.001',maxQty='1000'),
                             dict(filterType='MIN_NOTIONAL',notional='5')])
        self.payloads={
            '/api/v3/account':dict(uid=12345,canTrade=True,balances=[dict(asset='USDT',free='1000',locked='0')]),
            # V3 has no canTrade; read it from accountConfig, not a guessed field.
            '/fapi/v3/account':dict(assets=[dict(asset='USDT',availableBalance='1000')],positions=[]),
            '/sapi/v1/account/apiRestrictions':dict(enableReading=True,enableWithdrawals=False,ipRestrict=True,enableInternalTransfer=False,permitsUniversalTransfer=False,enableFutures=True,enableSpotAndMarginTrading=True),
            '/fapi/v1/accountConfig':dict(canTrade=True,dualSidePosition=True,multiAssetsMargin=False),
            '/api/v3/openOrders':[], '/fapi/v1/openOrders':[],
            '/fapi/v1/symbolConfig':[dict(symbol='BTCUSDT',leverage=1,marginType='CROSSED',maxNotionalValue='100000')],
            '/api/v3/exchangeInfo':dict(symbols=[deepcopy(symbol)]), '/fapi/v1/exchangeInfo':dict(symbols=[deepcopy(symbol)]),
            '/api/v3/account/commission':dict(standardCommission=dict(taker='.001',buyer='0',seller='0'),specialCommission=dict(taker='0',buyer='0',seller='0'),taxCommission=dict(taker='0',buyer='0',seller='0')),
            '/fapi/v1/commissionRate':dict(takerCommissionRate='.0005')}
    def sync_clocks(self):pass
    def request(self,market,path,params=None,**kwargs):
        assert kwargs.get('method','GET')=='GET';self.reads.append(path);return deepcopy(self.payloads[path])
    def depth(self,market,symbol):return book(market=='futures')


class PreflightTests(unittest.TestCase):
    def setUp(self):self.client=PreflightClient()
    def tearDown(self):self.client.close()
    def test_v3_and_config_shapes_produce_short_hedge_without_settings_changes(self):
        p,_,_=self.client.prepare(request_body())
        self.assertEqual(p['position_side'],'SHORT');self.assertIn('/fapi/v1/accountConfig',self.client.reads)
    def test_existing_positions_open_orders_and_leverage_change_reject(self):
        for path,value in [('/fapi/v1/openOrders',[{'orderId':1}]),('/fapi/v3/account',dict(assets=[],positions=[dict(symbol='BTCUSDT',positionAmt='.001')])),('/fapi/v1/symbolConfig',[dict(symbol='BTCUSDT',leverage=2)])]:
            c=PreflightClient();c.payloads[path]=value
            try:
                with self.subTest(path=path),self.assertRaises(GuardError):c.prepare(request_body())
            finally:c.close()
    def test_transfers_and_multi_asset_accounts_rejected(self):
        self.client.payloads['/sapi/v1/account/apiRestrictions']['permitsUniversalTransfer']=True
        with self.assertRaises(GuardError):self.client.accounts()
        self.client.payloads['/sapi/v1/account/apiRestrictions']['permitsUniversalTransfer']=False
        self.client.payloads['/fapi/v1/accountConfig']['multiAssetsMargin']=True
        with self.assertRaises(GuardError):self.client.prepare(request_body())
    def test_special_commission_is_included(self):
        self.client.payloads['/api/v3/account/commission']['specialCommission']['taker']='.002'
        p,_,_=self.client.prepare(request_body());self.assertEqual(p['spot_fee_rate'],'0.003')

    def test_sell_commission_is_independent_from_buy_commission(self):
        self.client.payloads['/api/v3/account/commission']['taxCommission']['seller']='.002'
        p,_,_=self.client.prepare(request_body(residual_policy='reduce'))
        self.assertEqual(p['spot_fee_rate'],'0.001')
        self.assertEqual(p['spot_exit_fee_rate'],'0.003')
        self.assertEqual(p['exit_fee_bps']['spot'],'30.000')
    def test_position_limit_and_unfamiliar_position_control_reject(self):
        self.client.payloads['/api/v3/exchangeInfo']['symbols'][0]['filters'].append(dict(filterType='MAX_POSITION',maxPosition='.01'))
        with self.assertRaises(GuardError):self.client.prepare(request_body())
        self.client.payloads['/api/v3/exchangeInfo']['symbols'][0]['filters'].pop()
        self.client.payloads['/fapi/v1/exchangeInfo']['symbols'][0]['filters'].append(dict(filterType='POSITION_RISK_CONTROL',positionControlSide='SHORT'))
        with self.assertRaises(GuardError):self.client.prepare(request_body())
    def test_btc_minimum_is_actionable_not_rounded_up(self):
        self.client.payloads['/fapi/v1/exchangeInfo']['symbols'][0]['filters'][-1]['notional']='50'
        with self.assertRaisesRegex(GuardError,'不会自动放大'):self.client.prepare(request_body())

class WireTests(unittest.TestCase):
    def setUp(self):
        self.requests=[];self.responses=[Response({'ok':True})]
        outer=self
        class FakeSocket:
            def settimeout(self,timeout):pass
            def shutdown(self,how):pass
        class Connection:
            def __init__(self,*a,**kw):self.host=a[0];self.sock=FakeSocket()
            def request(self,method,path,body=None,headers=None):outer.requests.append((self.host,method,path,body,headers))
            def stage_order(self,path,body,headers):self.staged=(self.host,'POST',path,body,headers);return b'synthetic'
            def write_order(self,data,gate,timing):
                gate.before_write(timing);outer.requests.append(self.staged);timing['write_completed_monotonic']=time.monotonic()
            def getresponse(self):return outer.responses.pop(0)
            def close(self):pass
        self.patch=patch('binance_hedge.client.OrderConnection',Connection);self.patch.start()
        self.client=BinanceClient('K'*64,'S'*64,allow_orders=True)
        self.client._offset={m:(0,time.monotonic()) for m in ('spot','futures')}
    def tearDown(self):self.client.close();self.patch.stop()
    def test_signature_matches_exact_body_and_hosts_are_fixed(self):
        self.client.request('spot','/api/v3/order',dict(symbol='BTCUSDT',type='LIMIT',quantity='0.1'),method='POST',signed=True)
        host,method,path,body,headers=self.requests[0]
        unsigned,signature=body.rsplit('&signature=',1)
        self.assertEqual(signature,hmac.new(('S'*64).encode(),unsigned.encode(),hashlib.sha256).hexdigest())
        self.assertEqual(host,'api.binance.com');self.assertEqual(path,'/api/v3/order');self.assertEqual(headers['X-MBX-APIKEY'],'K'*64)
        self.assertEqual(parse_qs(unsigned)['recvWindow'],['2000'])
    def test_transfer_withdraw_and_account_changes_impossible(self):
        for method,path in [('POST','/sapi/v1/capital/withdraw/apply'),('POST','/fapi/v1/leverage'),('GET','https://elsewhere/')]:
            with self.assertRaises(GuardError):self.client.request('spot',path,method=method)
        self.assertEqual(self.requests,[])
    def test_read_only_adapter_cannot_place(self):
        self.client.allow_orders=False
        with self.assertRaises(GuardError):self.client.submit('spot',{})
        self.assertEqual(self.requests,[])
    def test_503_ambiguous_status_not_retried(self):
        self.responses=[Response({'code':-1007,'msg':'private-sensitive-details'},503)]
        with self.assertRaises(ExchangeError) as ctx:self.client.submit('futures',{})
        self.assertTrue(ctx.exception.uncertain);self.assertNotIn('private-sensitive-details',str(ctx.exception));self.assertEqual(len(self.requests),1)
    def test_explicit_filter_rejection_known_zero(self):
        self.responses=[Response({'code':-1013},400)]
        with self.assertRaises(ExchangeError) as ctx:self.client.submit('spot',{})
        self.assertFalse(ctx.exception.uncertain)
    def test_duplicate_rejection_stays_ambiguous(self):
        self.responses=[Response({'code':-2010,'msg':'Duplicate order sent.'},400)]
        with self.assertRaises(ExchangeError) as ctx:self.client.submit('spot',{})
        self.assertTrue(ctx.exception.uncertain)
    def test_rate_limit_stops_followup_requests(self):
        self.responses=[Response({'code':-1003},429)]
        for _ in range(2):
            with self.assertRaises(ExchangeError):self.client.request('spot','/api/v3/account',signed=True)
        self.assertEqual(len(self.requests),1)

if __name__=='__main__':unittest.main()

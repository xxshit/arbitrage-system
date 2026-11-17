import copy
import json
import time
import tracemalloc
import unittest
import weakref
from unittest.mock import patch

from flask import Flask, g, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select, func

from market_flow import FlowEngine, Bar, MAX_BARS, MAX_FOCUS, catalogue, contract, rest_bars, notice
from market_flow_service import register, cached_market, CATALOGUE_URL

NOW = 1800000000.0  # Exact minute boundary, synthetic clock only.


def instrument(symbol="DEMOUSDT"):
    return dict(symbol=symbol, baseAsset=symbol[:-4], quoteAsset="USDT", status="TRADING", isSpotTradingAllowed=True)


def candle(start, quote=10000., buy=None, opening=100., closing=100.1):
    return [int(start*1000), str(opening), str(max(opening,closing)*1.001), str(min(opening,closing)*.999),
            str(closing), "100", int((start+60)*1000)-1, str(quote), 100, "60", str(quote*.65 if buy is None else buy), "0"]


def history(now=NOW, ratio=3, share=.65):
    return [candle(now-(65-i)*60, quote=10000*(ratio if i>=60 else 1),
                   buy=10000*(ratio if i>=60 else 1)*share) for i in range(65)]


def tick(symbol="DEMOUSDT", now=NOW):
    return dict(symbol=symbol,lastPrice="100",quoteVolume="20000000",priceChangePercent="3",closeTime=int(now*1000))


def book(symbol="DEMOUSDT"):
    return dict(symbol=symbol,bidPrice="99.99",askPrice="100.01")


def stream(symbol="DEMOUSDT", now=NOW, closed=False, raw=None):
    raw=raw or candle(now//60*60-60 if closed else now//60*60)
    return dict(e="kline",E=int(now*1000),s=symbol,k=dict(t=raw[0],T=raw[6],s=symbol,i="1m",o=raw[1],h=raw[2],l=raw[3],c=raw[4],q=raw[7],Q=raw[10],n=raw[8],x=closed))


def engine(symbol="DEMOUSDT", now=NOW, ratio=3, share=.65):
    e=FlowEngine();e.set_catalog(dict(symbols=[instrument(symbol)]),now)
    e.ingest_market([tick(symbol,now)],[book(symbol)],now,now,book_at=now)
    e.set_focus([symbol]);e.seed(symbol,history(now,ratio,share),now)
    e.ingest_stream(stream(symbol,now),now)
    return e


class FlowEvidenceTests(unittest.TestCase):
    def test_catalog_request_omits_unused_permission_matrix(self):
        from urllib.parse import parse_qs,urlsplit
        self.assertEqual(parse_qs(urlsplit(CATALOGUE_URL).query),{'permissions':['SPOT'],'symbolStatus':['TRADING'],'showPermissionSets':['false']})

    def test_exact_five_vs_previous_sixty_and_aggressor_not_cash_inflow(self):
        e=engine();r=e.inspect("DEMOUSDT",NOW);v=r["evidence"]
        self.assertEqual((v["quote_volume"],v["baseline_quote_volume"],v["expected_5m_quote"],v["volume_ratio"]),(150000,600000,50000,3))
        self.assertEqual((v["taker_buy_quote"],v["taker_sell_quote"],v["taker_delta_quote"]),(97500,52500,45000))
        self.assertEqual((v["window_start"],v["window_end"],v["baseline_end"]),(NOW-300,NOW,NOW-300))
        self.assertEqual(r["kind"],"suspected_accumulation");self.assertTrue(r["candidate"])
        self.assertIn("不是资金净流入",v["interpretation"])

    def test_plain_transfer_and_wrong_market_cannot_create_evidence(self):
        e=engine()
        before=copy.deepcopy(e.bars)
        self.assertFalse(e.ingest_stream(dict(e="Transfer",s="DEMOUSDT",value="999999999999"),NOW+1))
        self.assertFalse(e.ingest_stream(dict(from_address="a",to="b",value=10000000),NOW+2))
        self.assertEqual(e.bars,before)
        value=stream("UNWATCHEDUSDT",NOW+3,True)
        self.assertFalse(e.ingest_stream(value,NOW+3))

    def test_sell_volume_is_volume_not_accumulation(self):
        r=engine(share=.2).inspect("DEMOUSDT",NOW)
        self.assertEqual(r["kind"],"volume_expansion");self.assertFalse(r["candidate"])
        self.assertLess(r["evidence"]["taker_delta_quote"],0)

    def test_flat_or_thin_volume_does_not_fake_opportunity(self):
        r=engine(ratio=1).inspect("DEMOUSDT",NOW)
        self.assertIsNone(r["kind"]);self.assertFalse(r["candidate"])
        e=engine();e.bars["DEMOUSDT"]={b.start:Bar(b.start,b.open,b.high,b.low,b.close,b.quote/1000,b.buy/1000,b.trades) for b in e.bars["DEMOUSDT"].values()}
        self.assertIsNone(e.inspect("DEMOUSDT",NOW)["kind"])

    def test_open_candle_is_never_counted_and_initial_history_never_alerts(self):
        e=engine();before=copy.deepcopy(e.bars)
        e.ingest_stream(stream(now=NOW+1,raw=candle(NOW,quote=999999999)),NOW+1)
        self.assertEqual(before,e.bars)
        row=e.inspect("DEMOUSDT",NOW+1)
        self.assertEqual(notice(row,{},NOW+1),(None,{}))
        e.ingest_stream(stream(now=NOW+2,closed=True,raw=history()[-1]),NOW+2)
        row=e.inspect("DEMOUSDT",NOW+2)
        self.assertTrue(row["can_notify"]);self.assertIsNotNone(notice(row,{},NOW+2)[0])

    def test_missing_minute_stale_book_and_stale_stream_never_recommend(self):
        e=engine();del e.bars["DEMOUSDT"][NOW-30*60]
        self.assertEqual(e.inspect("DEMOUSDT",NOW)["status"],"gap")
        e=engine();self.assertEqual(e.inspect("DEMOUSDT",NOW+11)["status"],"stale")
        e.ingest_stream(stream(now=NOW+21),NOW+21)
        r=e.inspect("DEMOUSDT",NOW+21)
        self.assertFalse(r["candidate"]);self.assertEqual(r["kind"],"volume_expansion")

    def test_price_surge_is_not_accumulation(self):
        e=engine();b=e.bars["DEMOUSDT"][NOW-60]
        e.bars["DEMOUSDT"][b.start]=Bar(b.start,100,120,99,119,b.quote,b.buy,b.trades)
        r=e.inspect("DEMOUSDT",NOW)
        self.assertFalse(r["candidate"]);self.assertEqual(r["kind"],"volume_expansion")

    def test_duplicate_out_of_order_conflicting_closed_and_delayed_seed(self):
        e=engine();raw=history()[-1]
        self.assertTrue(e.ingest_stream(stream(now=NOW+2,closed=True,raw=raw),NOW+2))
        self.assertFalse(e.ingest_stream(stream(now=NOW+1,closed=True,raw=raw),NOW+2))
        self.assertFalse(e.ingest_stream(stream(now=NOW+2,closed=True,raw=raw),NOW+2))
        original=e.bars["DEMOUSDT"][NOW-60]
        older=history();older[-1][7]="1000";older[-1][10]="600"
        e.seed("DEMOUSDT",older,NOW+3)
        self.assertEqual(e.bars["DEMOUSDT"][NOW-60],original)
        self.assertFalse(e.ingest_stream(stream(now=NOW+4,closed=True,raw=older[-1]),NOW+4))
        self.assertNotIn("DEMOUSDT",e.bars)

    def test_cooldown_transition_strength_and_frozen_evidence(self):
        e=engine();e.ingest_stream(stream(now=NOW+1,closed=True,raw=history()[-1]),NOW+1)
        row=e.inspect("DEMOUSDT",NOW+1);event,state=notice(row,{},NOW+1)
        original=copy.deepcopy(event)
        self.assertIsNone(notice(row,state,NOW+601)[0])
        row2=copy.deepcopy(row);row2['evidence']['volume_ratio']=5
        self.assertIsNone(notice(row2,state,NOW+10)[0])
        self.assertIsNotNone(notice(row2,state,NOW+601)[0])
        self.assertEqual(event,original)
        row2['kind']=None
        self.assertFalse(notice(row2,state,NOW+20)[1]['active'])

    def test_universe_and_focus_caps_and_eviction(self):
        e=engine()
        with self.assertRaises(ValueError):e.set_focus([f'C{i}USDT' for i in range(MAX_FOCUS+1)])
        e.set_focus([])
        self.assertEqual((e.bars,e.stream_at,e.seeded_at,e.live_closed),({},{},{},{}))
        for bad in ['../BTC','BTC?token=x',True,'A'*40]:
            with self.assertRaises(ValueError):contract(bad)
        self.assertEqual(contract('币安人生/USDT'),'币安人生USDT')

    def test_catalogue_excludes_suspended_stables_and_wrong_quote(self):
        values=[instrument(),instrument('USDCUSDT'),{**instrument('STOPUSDT'),'status':'BREAK'},{**instrument('ETHBTC'),'quoteAsset':'BTC'}]
        self.assertEqual(catalogue({'symbols':values}),{'DEMOUSDT':'DEMO'})

    def test_broad_24h_turnover_is_not_differenced_to_invent_5m_volume(self):
        e=engine();t=tick(now=NOW+60);t['quoteVolume']='1'
        e.ingest_market([t],[book()],NOW+60,NOW+60,book_at=NOW+60)
        self.assertEqual(e.market['DEMOUSDT']['quote_24h'],1)
        self.assertNotIn('volume_5m',e.market['DEMOUSDT'])

    def test_fresh_books_update_without_refetching_the_24h_cache(self):
        e=engine();original=e.market['DEMOUSDT']['market_at']
        self.assertTrue(e.ingest_market([tick()],[book()],NOW,NOW+15,book_at=NOW+15))
        e.ingest_stream(stream(now=NOW+21),NOW+21)
        r=e.inspect('DEMOUSDT',NOW+21)
        self.assertEqual(r['evidence']['book_at'],NOW+15)
        self.assertEqual(r['market']['market_at'],original)
        self.assertTrue(r['candidate'])
        self.assertEqual(len(e.prices['DEMOUSDT']),1)

    def test_stale_24h_statistics_cannot_qualify_with_a_new_book(self):
        e=engine();e.market['DEMOUSDT']['quote_at']=NOW+46
        e.ingest_stream(stream(now=NOW+46),NOW+46)
        self.assertFalse(e.inspect('DEMOUSDT',NOW+46)['candidate'])

    def test_malformed_values_and_partial_baseline_rejected(self):
        for index,value in [(7,'nan'),(10,'9999999'),(3,'101'),(8,'1.5')]:
            row=candle(NOW-60);row[index]=value
            with self.assertRaises(ValueError):Bar.parse(row,NOW)
        with self.assertRaises(ValueError):rest_bars([candle(NOW-60)]*2,NOW)
        e=engine();e.bars['DEMOUSDT'].pop(NOW-65*60)
        self.assertEqual(e.inspect('DEMOUSDT',NOW)['status'],'warming')

    def test_bounded_replacement_does_not_retain_input_payloads(self):
        class Message(dict):pass
        e=engine();msg=Message(stream(now=NOW+1));ref=weakref.ref(msg)
        e.ingest_stream(msg,NOW+1);del msg
        self.assertIsNone(ref()) # No forced collection necessary.
        for minute in range(1,181):
            at=NOW+minute*60
            e.ingest_stream(stream(now=at,closed=True,raw=candle(at-60)),at)
        self.assertEqual(len(e.bars['DEMOUSDT']),MAX_BARS)


class FlowWebTests(unittest.TestCase):
    def setUp(self):
        self.app=Flask(__name__);self.app.config.update(SECRET_KEY='synthetic',SQLALCHEMY_DATABASE_URI='sqlite://',TESTING=True)
        self.db=SQLAlchemy(self.app)
        @self.app.before_request
        def identity():
            from flask import request
            from types import SimpleNamespace
            g.user=SimpleNamespace(role=request.headers['Test-Role']) if request.headers.get('Test-Role') else None
        self.monitor=register(self.app,self.db,lambda:getattr(g,'user',None),lambda:{})
        self.context=self.app.app_context();self.context.push();self.db.create_all();self.monitor.store().initialize();self.monitor.ready=True
        self.monitor.engine=engine(now=time.time()//60*60)
        self.client=self.app.test_client()
        with self.client.session_transaction() as s:s['csrf_token']='synthetic-token'
        self.headers={'Test-Role':'admin','X-CSRF-Token':'synthetic-token'}

    def tearDown(self):
        self.db.session.remove();self.db.drop_all();self.context.pop()

    def test_diagnostics_export_counts_without_tag_or_symbol_payloads(self):
        self.monitor.published=dict(rows=[],manual=[{'contract':'PRIVATE-LABEL','label':'private text'}],recommended=[],checked_at=NOW)
        result=self.monitor.diagnostics(NOW)
        self.assertEqual(result['focus_count'],1)
        self.assertNotIn('PRIVATE-LABEL',json.dumps(result))
        self.assertNotIn('private text',json.dumps(result))
        self.assertTrue(all(not isinstance(v,(list,dict)) for v in result.values()))

    def test_auth_csrf_and_readonly_user(self):
        self.assertEqual(self.client.get('/api/market-flow').status_code,401)
        self.assertEqual(self.client.post('/api/market-flow/tags',json={'symbol':'DEMO'},headers={'Test-Role':'admin'}).status_code,403)
        self.assertEqual(self.client.post('/api/market-flow/tags',json={'symbol':'DEMO'},headers={**self.headers,'Test-Role':'viewer'}).status_code,403)
        r=self.client.get('/api/market-flow',headers={'Test-Role':'viewer'})
        self.assertEqual(r.status_code,200);self.assertEqual(r.headers['Cache-Control'],'private, no-store')

    def test_manual_tag_persistence_update_delete_and_invalid_market(self):
        url='/api/market-flow/tags'
        self.assertEqual(self.client.post(url,json={'symbol':'DEMO','label':'重点'},headers=self.headers).status_code,200)
        self.assertEqual(self.client.post(url,json={'symbol':'DEMO','label':'变更'},headers=self.headers).status_code,200)
        self.assertEqual(self.monitor.store().tag_list()[0]['label'],'变更')
        self.assertEqual(self.client.post(url,json={'symbol':'UNKNOWN'},headers=self.headers).status_code,400)
        self.assertEqual(self.client.delete(url,json={'symbol':'DEMO'},headers=self.headers).status_code,200)
        self.assertEqual(self.monitor.store().tag_list(),[])

    def test_shared_manual_capacity_and_events_not_deleted_with_tag(self):
        store=self.monitor.store()
        for i in range(20):store.tag(f'C{i}USDT','manual',NOW)
        with self.assertRaises(ValueError):store.tag('NEWUSDT','manual',NOW)
        store.tag('C0USDT','new label',NOW)
        e=engine();e.ingest_stream(stream(now=NOW+1,closed=True,raw=history()[-1]),NOW+1)
        row=e.inspect('DEMOUSDT',NOW+1);first=store.advance(row,NOW+1)
        self.assertIsNotNone(first);self.assertIsNone(store.advance(row,NOW+601))
        store.tag('C0USDT','',NOW,remove=True)
        self.assertEqual(len(store.history(NOW+1000)['items']),1)

    def test_failed_commit_does_not_consume_notice(self):
        e=engine();e.ingest_stream(stream(now=NOW+1,closed=True,raw=history()[-1]),NOW+1)
        row=e.inspect('DEMOUSDT',NOW+1);store=self.monitor.store()
        from sqlalchemy import event
        def fail(conn,clause,multiparams,params,options):
            if getattr(clause,'is_insert',False) and clause.table.name=='market_flow_event':raise RuntimeError('synthetic storage failure')
        event.listen(self.db.engine,'before_execute',fail)
        try:
            with self.assertRaises(RuntimeError):store.advance(row,NOW+1)
        finally:event.remove(self.db.engine,'before_execute',fail)
        self.assertEqual(store.history(NOW+2)['items'],[])
        self.assertIsNotNone(store.advance(row,NOW+2))

    def test_alarm_http_path_no_longer_depends_on_store_availability(self):
        self.monitor.published=dict(rows=[],manual=[],recommended=[],checked_at=NOW)
        with patch.object(self.monitor,'store',side_effect=RuntimeError('synthetic')):
            result=self.monitor.alerts(NOW)
        self.assertEqual(result['active_events'],[])
        self.assertFalse(result['health']['persistence_error'])

    def test_stale_recommendations_are_removed_on_read(self):
        e=engine();row=e.inspect('DEMOUSDT',NOW)
        self.monitor.engine=e
        self.monitor.published=dict(rows=[row],manual=[],recommended=['DEMOUSDT'],checked_at=NOW)
        self.assertEqual(self.monitor.payload(NOW)['recommended'],['DEMOUSDT'])
        self.assertEqual(self.monitor.payload(NOW+16)['recommended'],[])

    def test_history_paginated_full_evidence_and_parameter_validation(self):
        store=self.monitor.store()
        with self.db.engine.begin() as conn:
            conn.execute(store.events.insert(),[dict(id=f'{i:040d}',contract='DEMOUSDT',observed_at=int(NOW-i),evidence_json=json.dumps(dict(id=i,evidence={'raw_metric':i+.125}))) for i in range(67)])
        a=store.history(NOW,page=1);b=store.history(NOW,page=3)
        self.assertEqual((len(a['items']),a['has_more'],len(b['items']),b['has_more']),(30,True,7,False))
        self.assertEqual(a['items'][0]['evidence']['raw_metric'],.125)
        self.assertEqual(self.client.get('/api/market-flow/events?page=0',headers=self.headers).status_code,400)

    def test_control_http_budget_connection_release_and_gap_recovery(self):
        symbols=[f'C{i}USDT' for i in range(32)]
        monitor=self.monitor;monitor.engine=FlowEngine()
        monitor.engine.set_catalog({'symbols':[instrument(s) for s in symbols]},NOW)
        for s in symbols[:20]:monitor.store().tag(s,'manual',NOW)
        from sqlalchemy import event
        checked_out=[0]
        def checkout(*args):checked_out[0]+=1
        def checkin(*args):checked_out[0]-=1
        event.listen(self.db.engine,'checkout',checkout);event.listen(self.db.engine,'checkin',checkin)
        requests=[]
        def fetch(url):
            self.assertEqual(checked_out[0],0,'SQL connection held during public network IO')
            requests.append(url);return history(now=NOW)
        try:
            for step in range(4):
                now=NOW+step*5
                monitor.cache_reader=lambda:{'https://api.binance.com/api/v3/ticker/24hr':dict(payload=[tick(s,now) for s in symbols],fetched_at=1000), 'https://api.binance.com/api/v3/ticker/bookTicker':dict(payload=[book(s) for s in symbols],fetched_at=1000)}
                with patch('market_flow_service.time.time',return_value=now),patch('market_flow_service.time.monotonic',return_value=1000):
                    count=len(requests);monitor.cycle(now,fetch)
                self.assertLessEqual(len(requests)-count,1)
            self.assertEqual(len(monitor.engine.focus),32)
            self.assertEqual(len(requests),4)
            self.assertEqual(checked_out[0],0)
            # A missing closed minute is repaired from bounded REST history.
            s=symbols[0];monitor.engine.bars[s].pop(NOW-20*60)
            monitor.engine.ingest_stream(stream(s,NOW+61),NOW+61)
            self.assertEqual(monitor.engine.inspect(s,NOW+61)['status'],'gap')
            monitor.engine.seed(s,history(NOW+60),NOW+61)
            self.assertEqual(monitor.engine.inspect(s,NOW+61)['status'],'live')
        finally:
            event.remove(self.db.engine,'checkout',checkout);event.remove(self.db.engine,'checkin',checkin)


class FlowAlarmIntegrationTests(unittest.TestCase):
    def test_original_alarm_endpoint_includes_new_events_and_survives_new_storage_failure(self):
        # Existing fixture imports the app against an isolated synthetic SQLite DB.
        from tests import test_alert_history as fixture
        case=fixture.AlertHistoryTests();case.setUp()
        module=fixture.module;monitor=module.FLOW_MONITOR
        now=case.now.replace(tzinfo=module.timezone.utc).timestamp()
        old=(monitor.ready,monitor.engine,monitor.published)
        try:
            e=engine(now=now//60*60);raw=history(now//60*60)[-1]
            e.ingest_stream(stream(now=now+1,closed=True,raw=raw),now+1)
            row=e.inspect('DEMOUSDT',now+1)
            monitor.ready=True;monitor.engine=e
            with module.app.app_context():
                monitor.store().initialize();event=monitor.store().advance(row,now+1)
            monitor.published=dict(rows=[row],manual=[{'contract':'DEMOUSDT'}],recommended=[],checked_at=now+1,active_events=[event])
            with patch.object(module.time,'time',return_value=now+2):
                data=case.get(view='active')
                self.assertEqual(data['market_flow']['active_events'][0]['contract'],'DEMOUSDT')
                self.assertIn('funding',data);self.assertIn('active_events',data)
                with patch.object(monitor,'store',side_effect=RuntimeError('synthetic failure')):
                    failed=case.get(view='active')
                self.assertEqual(failed['active_events'],data['active_events'])
                self.assertEqual(failed['funding'],data['funding'])
                self.assertEqual(failed['market_flow']['active_events'],data['market_flow']['active_events'])
        finally:
            monitor.ready,monitor.engine,monitor.published=old
            case.tearDown()


if __name__=='__main__':unittest.main()

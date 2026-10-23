import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"]="sqlite://"
import app as module
from opportunity_validation import ValidationLedger

NOW=1800000000
SPOT={"symbols":[{"symbol":"DEMO/USDT","rows":[{"long_exchange":"Gate"},{"long_exchange":"Bitget"}]}]}
DUAL={"symbols":[{"symbol":"DEMO/USDT","rows":[{"long_exchange":"Bybit","short_exchange":"Binance"}]}]}


class PersonalOpportunityTests(unittest.TestCase):
    def setUp(self):
        module.app.config.update(TESTING=True)
        with module.app.app_context():
            module.db.drop_all();module.db.create_all()
            users=[module.UserAccount(username=name,password_hash="not-a-secret-test",role="viewer",account_level="lv2",
                                      active_session_hash=module.session_digest(name+"-token")) for name in ("one","two")]
            module.db.session.add_all(users);module.db.session.commit()
            self.ids=[u.id for u in users]
        self.client=module.app.test_client();self.login(0)
        self.spot=patch.object(module,"load_latest_market_snapshot",return_value=SPOT);self.spot.start()
        self.dual=patch.object(module,"load_latest_dual_futures_snapshot",return_value=DUAL);self.dual.start()
        self.rwa=patch.object(module,"is_rwa_stock_pair",return_value=False);self.rwa.start()

    def tearDown(self):
        self.spot.stop();self.dual.stop();self.rwa.stop()
        with module.app.app_context():
            module.db.session.remove();module.db.drop_all()

    def login(self,index):
        name=("one","two")[index]
        with self.client.session_transaction() as session:
            session.clear();session.update(user_id=self.ids[index],auth_token=name+"-token",csrf_token=name+"-csrf")

    def put(self,**overrides):
        data=dict(strategy="spot",symbol="DEMO/USDT",long_exchange="Gate",short_exchange="Binance",watched=True)
        data.update(overrides)
        return self.client.put('/api/arbitrage-thinking/watchlist',json=data,headers={'X-CSRF-Token':'one-csrf'})

    def test_unauthenticated_and_empty_or_wrong_csrf_are_rejected(self):
        payload=dict(strategy="spot",symbol="DEMO/USDT",long_exchange="Gate",short_exchange="Binance",watched=True)
        self.assertEqual(self.client.put('/api/arbitrage-thinking/watchlist',json=payload).status_code,403)
        with self.client.session_transaction() as session:session.pop("csrf_token")
        self.assertEqual(self.client.put('/api/arbitrage-thinking/watchlist',json=payload).status_code,403)
        with self.client.session_transaction() as session:session.clear()
        for endpoint in ('/api/arbitrage-thinking/watchlist','/api/arbitrage-thinking/validation'):
            self.assertEqual(self.client.get(endpoint).status_code,401)

    def test_viewer_can_save_own_path_idempotently_without_notifications(self):
        for _ in range(2):self.assertEqual(self.put().status_code,200)
        with module.app.app_context():
            self.assertEqual(module.OpportunityWatch.query.count(),1)
            self.assertEqual(module.ThoughtWatch.query.count(),0)
            self.assertEqual(module.HedgePosition.query.count(),0)
            self.assertEqual(module.AlertEvent.query.count(),0)
        response=self.client.get('/api/arbitrage-thinking/watchlist')
        self.assertIn('no-store',response.headers['Cache-Control'])
        self.assertEqual(response.json['paths'],[['spot','DEMO/USDT','Gate','Binance']])
        self.login(1)
        self.assertEqual(self.client.get('/api/arbitrage-thinking/watchlist').json['paths'],[])
        self.assertEqual(self.put(user_id=self.ids[0]).status_code,403)  # Wrong CSRF first.
        self.login(0)
        self.assertEqual(self.put(user_id=self.ids[1]).status_code,400)

    def test_strategy_and_ordered_venues_are_separate_not_whole_coin(self):
        self.assertEqual(self.put().status_code,200)
        self.assertEqual(self.put(long_exchange='Bitget').status_code,200)
        self.assertEqual(self.put(strategy='dual',long_exchange='Bybit').status_code,200)
        self.assertEqual(len(self.client.get('/api/arbitrage-thinking/watchlist').json['paths']),3)
        self.assertEqual(self.put(watched=False).status_code,200)
        paths=self.client.get('/api/arbitrage-thinking/watchlist').json['paths']
        self.assertEqual(len(paths),2)
        self.assertIn(['spot','DEMO/USDT','Bitget','Binance'],paths)

    def test_unsupported_payload_and_nonexistent_or_unverified_routes_rejected(self):
        for patch_data in (dict(symbol='../DEMO'),dict(strategy='funding'),dict(long_exchange='Fake'),
                           dict(strategy='dual',long_exchange='Binance'),dict(short_exchange='OKX'),dict(watched=1)):
            self.assertEqual(self.put(**patch_data).status_code,400)
        with patch.object(module,'is_rwa_stock_pair',return_value=True):
            self.assertEqual(self.put().status_code,400)

    def test_delisted_or_missing_path_can_still_be_removed(self):
        self.put()
        with patch.object(module,'load_latest_market_snapshot',return_value=None):
            self.assertEqual(self.put(watched=False).status_code,200)
            self.assertEqual(self.put().status_code,400)

    def test_cap_is_per_user_and_existing_save_does_not_consume_more_slots(self):
        self.put()
        with module.app.app_context():
            module.db.session.add_all([module.OpportunityWatch(user_id=self.ids[0],strategy='spot',symbol=f'C{i}/USDT',
               long_exchange='Gate',short_exchange='Binance') for i in range(199)])
            module.db.session.commit()
        self.assertEqual(self.put().status_code,200)
        self.assertEqual(self.put(long_exchange='Bitget').status_code,400)

    def test_missing_watched_path_is_visible_without_old_prices_or_score(self):
        self.put()
        with patch.object(module,'spot_thinking_candidates',return_value=[]), \
                patch.object(module,'mark_delisting_items'),patch.object(module.ALERT_STREAM_HUB,'screening_quotes',return_value={}):
            data=self.client.get('/api/arbitrage-thinking/simple?strategy=spot&scope=watched').json
        self.assertEqual(len(data['spot_simple']),1)
        item=data['spot_simple'][0]
        self.assertEqual(item['watch_state'],'unavailable')
        self.assertIsNone(item['open_spread']);self.assertIsNone(item['score'])
        self.assertFalse(item['scenario'])

    def test_live_dual_watch_uses_actual_futures_futures_route_namespace(self):
        self.assertEqual(self.put(strategy='dual',long_exchange='Bybit').status_code,200)
        snapshot={'symbols':[{'symbol':'DEMO/USDT','rows':[dict(long_exchange='Bybit',short_exchange='Binance',
                  open_spread=-.1,close_spread=-.08,funding_difference=-.002)]}]}
        now=time.time()
        path=('futures_futures','DEMO/USDT','Bybit','Binance')
        quote=dict(long=dict(bid=100,ask=100.1,bid_qty=100,ask_qty=100),
                   short=dict(bid=101,ask=101.1,bid_qty=100,ask_qty=100),long_at=now,short_at=now)
        with patch.object(module,'load_latest_dual_futures_snapshot',return_value=snapshot), \
                patch.object(module,'funding_statistics',return_value={}),patch.object(module,'mark_delisting_items'), \
                patch.object(module.ALERT_STREAM_HUB,'screening_quotes',return_value={path:quote}) as read:
            response=self.client.get('/api/arbitrage-thinking/simple?strategy=dual&scope=watched')
        self.assertEqual(response.status_code,200)
        self.assertEqual(read.call_args.args[0],[path])
        item=response.json['dual_simple'][0]
        self.assertEqual(item['watch_state'],'outside')
        self.assertAlmostEqual(item['open_spread'],100*(101/100.1-1))
        self.assertAlmostEqual(item['close_spread'],1.1)

    def test_validation_route_is_read_only_scoped_and_has_no_fake_results(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(module,'OPPORTUNITY_VALIDATION',ValidationLedger(temp)):
            data=self.client.get('/api/arbitrage-thinking/validation?symbol=DEMO/USDT&exchange=Gate&horizon=3600')
            self.assertEqual(data.status_code,200)
            self.assertEqual(data.json['observed'],0);self.assertEqual(data.json['cohorts'],[])
            self.assertIn('no-store',data.headers['Cache-Control'])
            for query in ('horizon=5','symbol=DEMO/USDT','symbol=DEMO/USDT&exchange=Fake','symbol=../DEMO&exchange=Gate'):
                self.assertEqual(self.client.get('/api/arbitrage-thinking/validation?'+query).status_code,400)

    def test_quality_stream_uses_exact_actual_settlements_and_keeps_only_aggregates(self):
        with module.app.app_context():
            module.db.session.add_all([module.FundingRateRecord(symbol='DEMOUSDT',funding_time=(NOW-i*14400)*1000,funding_rate=.01)
                                      for i in range(49)])
            module.db.session.commit()
            with patch.object(module.OPPORTUNITY_VALIDATION,'funding_requests',return_value=[]):
                module.refresh_opportunity_quality(NOW)
            evidence=module.OPPORTUNITY_QUALITY['data']['DEMOUSDT']
        self.assertEqual(evidence['windows']['7d']['count'],42)
        self.assertEqual(evidence['windows']['7d']['above'],42)
        self.assertNotIn('events',evidence)

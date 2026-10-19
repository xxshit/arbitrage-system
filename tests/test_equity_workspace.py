import json
import os
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

os.environ['DATABASE_URL'] = 'sqlite://'
os.environ.setdefault('SECRET_KEY', 'equity-workspace-test')
os.environ.setdefault('CHAT_ENCRYPTION_KEY', 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=')
import app as module
from asset_catalog import instrument_category


class EquityWorkspaceTests(unittest.TestCase):
    def setUp(self):
        module.app.config.update(TESTING=True)
        self.patches = [patch.object(module, name, {}) for name in ('ASSET_CLASSIFICATIONS', 'ASSET_CLASSIFICATION_SAVED')]
        self.patches.append(patch.object(module, 'RWA_STOCK_SYMBOLS', set()))
        for item in self.patches:
            item.start()
        with module.app.app_context():
            module.db.drop_all()
            module.db.create_all()
            user = module.UserAccount(username='equity-reader', password_hash='unused', role='admin', account_level='lv3', active_session_hash=module.session_digest('equity-test'))
            module.db.session.add(user)
            module.db.session.commit()
            user_id = user.id
        self.client = module.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'], session['auth_token'] = user_id, 'equity-test'

    def tearDown(self):
        with module.app.app_context():
            module.db.session.remove()
            module.db.drop_all()
        for item in reversed(self.patches):
            item.stop()

    def row(self, **overrides):
        return dict(dict(long_exchange='Bybit', short_exchange='Binance', long_ask=100, long_bid=99.9, short_bid=101, short_ask=101.1,
                         long_basis=-.2, short_basis=.01, long_funding_rate=.03, short_funding_rate=-.01,
                         long_funding_interval_hours=4, short_funding_interval_hours=8, open_spread=1, close_spread=1.2012), **overrides)

    def request(self, groups, query='', spot=None):
        snapshot = dict(symbols=groups, errors={}, updated_at='12:00:00', captured_at_epoch=time.time())
        with patch.object(module, 'load_latest_dual_futures_snapshot', return_value=snapshot), \
                patch.object(module, 'load_latest_market_snapshot', return_value=spot), \
                patch.object(module, 'get_json', side_effect=AssertionError('GET page must not fetch upstream')):
            return self.client.get('/api/equity-arbitrage' + query)

    def test_official_types_are_not_suffix_guesses_or_all_tradfi_as_us_equity(self):
        self.assertEqual(instrument_category({'underlyingType': 'EQUITY'}, 'Binance'), 'us_equity')
        self.assertEqual(instrument_category({'underlyingType': 'HK_EQUITY'}, 'Binance'), 'other_equity')
        self.assertEqual(instrument_category({'underlyingType': 'PREMARKET'}, 'Binance'), 'premarket')
        self.assertEqual(instrument_category({'underlyingType': 'COMMODITY'}, 'Binance'), 'commodity')
        self.assertEqual(instrument_category({'symbolType': 'ETF'}, 'Bybit'), 'equity')
        self.assertIsNone(instrument_category({'symbolType': 'innovation', 'symbol': 'STOCKUSDT'}, 'Bybit'))
        self.assertIsNone(instrument_category({'underlyingType': 'INDEX', 'underlyingSubType': ['Crypto']}, 'Binance'))

    def test_cold_start_bootstrap_also_covers_bybit_only_equities_and_normalizes_case(self):
        for symbol in ('CGNX/USDT', 'ewy-usdt', 'AMDSTOCK_USDT', 'XAU/USDT', 'SAMSUNG/USDT'):
            self.assertTrue(module.is_rwa_stock_pair(symbol), symbol)
        for symbol in ('BTC/USDT', 'SIREN/USDT', 'STOCK/USDT', 'BTCDOM/USDT'):
            self.assertFalse(module.is_rwa_stock_pair(symbol), symbol)
        self.assertTrue(module.is_us_equity_pair('EWY/USDT'))
        self.assertFalse(module.is_us_equity_pair('XAU/USDT'))
        self.assertFalse(module.is_us_equity_pair('SAMSUNG/USDT'))
        self.assertFalse(module.is_us_equity_pair('OPENAI/USDT'))

    def test_new_classification_is_durable_and_empty_partial_refresh_never_erases_identity(self):
        with module.app.app_context():
            module.refresh_asset_classifications([{'symbol': 'NEWSTOCKUSDT', 'underlyingType': 'EQUITY'}], 'Binance')
            module.ASSET_CLASSIFICATIONS.clear()
            module.ASSET_CLASSIFICATION_SAVED.clear()
            module.load_asset_classifications()
            self.assertTrue(module.is_us_equity_pair('NEWSTOCK/USDT'))
            module.refresh_asset_classifications([], 'Binance')
            module.refresh_asset_classifications([{'symbol': 'BTCUSDT', 'underlyingType': 'COIN'}], 'Binance')
            self.assertTrue(module.is_rwa_stock_pair('NEWSTOCK/USDT'))
            self.assertEqual(module.MarketAssetClassification.query.count(), 1)

    def test_bybit_stock_and_etf_do_not_classify_innovation_coins(self):
        with module.app.app_context():
            module.refresh_asset_classifications([{'symbol': 'ONLYBYUSDT', 'symbolType': 'stock'}, {'symbol': 'ONLYETFUSDT', 'symbolType': 'ETF'}, {'symbol': 'COINXYZUSDT', 'symbolType': 'innovation'}], 'Bybit')
        self.assertTrue(module.is_rwa_stock_pair('ONLYBY/USDT'))
        self.assertTrue(module.is_rwa_stock_pair('ONLYETF/USDT'))
        self.assertFalse(module.is_rwa_stock_pair('COINXYZ/USDT'))
        self.assertFalse(module.is_us_equity_pair('ONLYBY/USDT'))  # region unverified

    def test_crypto_alert_summary_detail_tracking_and_isolation_do_not_leak_equities(self):
        with module.app.app_context():
            for symbol in ('BTC/USDT', 'CGNX/USDT', 'EWY/USDT'):
                module.db.session.add(module.AlertEvent(symbol=symbol, strategy='spot_futures', alert_type='rapid_spread', message='test', open_spread=1, created_at=datetime.now()-timedelta(minutes=1)))
            module.db.session.add(module.SpotDivergence(symbol='EWY/USDT', long_exchange='Gate', short_exchange='Binance', started_at=datetime.now(), last_seen_at=datetime.now(), evidence_json='{}'))
            module.db.session.add(module.BasisTracking(symbol='CGNX/USDT', strategy='spot_futures', direction='positive', last_recorded_level=1, max_basis=1, max_abs_basis=1, max_at=datetime.now()))
            module.db.session.commit()
        for view in ('summary', 'legacy'):
            data = self.client.get('/api/alerts?view='+view).get_json()
            self.assertEqual([row['symbol'] for row in data['events']], ['BTC/USDT'])
            self.assertEqual(data['total_symbols'], 1)
            self.assertEqual(data['isolated_paths'], [])
            self.assertEqual(data['basis_tracking'], [])
            self.assertTrue(all(row['symbol']=='BTC/USDT' for row in data['active_events']))
        self.assertEqual(self.client.get('/api/alerts?view=detail&symbol=CGNX/USDT').get_json()['items'], [])
        with module.app.app_context():
            self.assertEqual(module.AlertEvent.query.count(), 3)  # hide, never delete

    def test_equity_reader_excludes_crypto_metals_foreign_and_unverified_contract_legs(self):
        groups = [dict(symbol=symbol, rows=[self.row()]) for symbol in ('TSLA/USDT', 'BTC/USDT', 'XAU/USDT', 'SAMSUNG/USDT', 'OPENAI/USDT')]
        groups.append(dict(symbol='AAPL/USDT', rows=[self.row(long_exchange='OKX')]))
        response = self.request(groups)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual([row['symbol'] for row in data['items']], ['TSLA/USDT'])
        self.assertEqual(data['items'][0]['long_interval'], 4)
        self.assertEqual(data['items'][0]['short_interval'], 8)
        self.assertEqual(data['items'][0]['short_funding_rate'], -.01)
        self.assertFalse(data['execution_enabled'])
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_venue_specific_verified_spot_alias_required_not_a_name_or_price_guess(self):
        alias = SimpleNamespace(verified=True, exchange='Gate', market_type='spot', canonical_symbol='TSLAUSDT', alias_symbol='TSLAXUSDT')
        self.assertTrue(module.verified_equity_spot_alias('TSLAUSDT', 'TSLAXUSDT', 'Gate', [alias]))
        self.assertFalse(module.verified_equity_spot_alias('TSLAUSDT', 'TSLAUSDT', 'Gate', [alias]))
        self.assertFalse(module.verified_equity_spot_alias('TSLAUSDT', 'TSLAXUSDT', 'Bitget', [alias]))
        alias.exchange = 'ANY'
        self.assertFalse(module.verified_equity_spot_alias('TSLAUSDT', 'TSLAXUSDT', 'Gate', [alias]))
        row = self.row(long_exchange='Gate', funding_rate=.03, funding_interval_hours=8, alert_stream_route=dict(long_key=['gate','TSLA_USDT'], short_key=['bn_future','TSLAUSDT']))
        spot = dict(symbols=[dict(symbol='TSLA/USDT', rows=[row])], captured_at_epoch=time.time(), errors={})
        self.assertEqual(self.request([], '?strategy=spot_futures', spot).get_json()['total'], 0)

    def test_verified_spot_path_has_funding_only_on_short_leg_and_transfer_evidence(self):
        alias = SimpleNamespace(verified=True, exchange='Gate', market_type='spot', canonical_symbol='TSLAUSDT', alias_symbol='TSLAXUSDT')
        row = self.row(long_exchange='Gate', basis=.123, funding_rate=.03, funding_interval_hours=8, alert_stream_route=dict(long_key=['gate','TSLAX_USDT'], short_key=['bn_future','TSLAUSDT']))
        spot = dict(symbols=[dict(symbol='TSLA/USDT', rows=[row])], captured_at_epoch=time.time(), errors={})
        with patch.object(module, 'symbol_alias_rows', return_value=[alias]), patch.object(module, 'load_transfer_statuses', return_value={('TSLA/USDT','Gate'):dict(exchange='Gate',state='unknown',chains=[])}):
            item = self.request([], '?strategy=spot_futures', spot).get_json()['items'][0]
        self.assertIsNone(item['long_funding_rate'])
        self.assertIsNone(item['long_basis'])
        self.assertEqual(item['short_funding_rate'], .03)
        self.assertEqual(item['short_basis'], .123)
        self.assertEqual(item['transfer_status']['state'], 'unknown')

    def test_filter_sort_and_page_are_applied_before_slicing(self):
        groups = [dict(symbol='TSLA/USDT', rows=[self.row(open_spread=i) for i in range(35)]), dict(symbol='AAPL/USDT', rows=[self.row(open_spread=99)])]
        data = self.request(groups, '?symbol=tsla&sort_by=open_spread&direction=desc&page=2').get_json()
        self.assertEqual((data['total'], data['page'], data['pages']), (35, 2, 2))
        self.assertEqual([row['open_spread'] for row in data['items']], [4,3,2,1,0])
        self.assertEqual(self.request(groups, '?strategy=spot_futures').get_json()['items'], [])

    def test_each_old_db_row_keeps_its_own_stale_timestamp(self):
        old = time.time()-3600
        data = self.request([dict(symbol='TSLA/USDT', rows=[self.row(captured_at_epoch=old)])]).get_json()
        self.assertEqual(data['items'][0]['captured_at_epoch'], old)
        self.assertFalse(data['items'][0]['fresh'])
        self.assertEqual(self.request([], '?page=oops').status_code, 400)
        self.assertEqual(self.request([], '?strategy=invalid').status_code, 400)

    def test_new_endpoint_requires_login_and_does_not_create_orders(self):
        self.assertEqual(module.app.test_client().get('/api/equity-arbitrage').status_code, 401)
        self.request([dict(symbol='TSLA/USDT', rows=[self.row()])])
        with module.app.app_context():
            self.assertEqual(module.HedgePosition.query.count(), 0)


if __name__ == '__main__':
    unittest.main()

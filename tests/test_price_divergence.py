import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ['DATABASE_URL'] = 'sqlite://'
os.environ.setdefault('SECRET_KEY', 'divergence-test')
os.environ.setdefault('CHAT_ENCRYPTION_KEY', 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=')
import app as module


class PriceDivergenceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now().replace(microsecond=0)
        module.app.config.update(TESTING=True)
        self.ctx = module.app.app_context()
        self.ctx.push()
        module.db.drop_all()
        module.db.create_all()
        self.transfer = {'state': 'fresh', 'chains': [{'name': 'BSC', 'deposit_open': False, 'withdraw_open': True}]}
        self.wallet = patch.object(module, 'load_transfer_statuses', side_effect=lambda pairs, **kw: {key: self.transfer for key in pairs})
        self.wallet.start()
        owner = module.UserAccount(username='risk-test', password_hash='unused', role='admin', account_level='lv3', active_session_hash=module.session_digest('risk-test-token'))
        module.db.session.add(owner)
        module.db.session.commit()
        self.client = module.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=owner.id, auth_token='risk-test-token')

    def tearDown(self):
        module.db.session.remove()
        module.db.drop_all()
        self.wallet.stop()
        self.ctx.pop()

    def row(self, **overrides):
        row = dict(long_exchange='Gate', short_exchange='Binance 合约', long_ask=.12225, long_bid=.12203,
                   short_bid=.02814, short_ask=.02815, mark_price=.02814, index_price=.02804, funding_rate=.01)
        row.update(overrides)
        row.update(open_spread=(row['short_bid']/row['long_ask']-1)*100,
                   close_spread=(row['short_ask']/row['long_bid']-1)*100,
                   basis=(row['mark_price']/row['index_price']-1)*100)
        return row

    def update(self, seconds=0, rows=None):
        result = module.update_spot_divergences([{'symbol': 'GUA/USDT', 'rows': rows or [self.row()]}], self.now+timedelta(seconds=seconds))
        module.db.session.commit()
        return result

    def confirm(self):
        for second in range(0, 301, 5):
            active = self.update(second)
        return active[('GUA/USDT', 'Gate')]

    def event(self, exchange='Gate', strategy='spot_futures', seconds=-60):
        return module.AlertEvent(symbol='GUA/USDT', strategy=strategy, long_exchange=exchange, short_exchange='Binance',
                                 alert_type='rapid_spread', message='确认后的测试', open_spread=-77, close_spread=-76.9, basis=.3,
                                 created_at=self.now+timedelta(seconds=seconds))

    def test_small_basis_and_large_executable_gap_with_blocked_chain_are_evidence(self):
        evidence = module.spot_divergence_evidence(self.row(), self.transfer)
        self.assertTrue(evidence['signal'])
        self.assertAlmostEqual(evidence['open_spread'], -76.981595, places=4)
        self.assertEqual(evidence['blocked_chains'], ['BSC 充关'])
        self.assertEqual(evidence['confirm_seconds'], 300)

    def test_closed_chain_alone_and_large_contract_basis_do_not_classify_depeg(self):
        normal = self.row(long_ask=.0282, long_bid=.0281)
        self.assertFalse(module.spot_divergence_evidence(normal, self.transfer)['signal'])
        self.assertFalse(module.spot_divergence_evidence(self.row(mark_price=.032), self.transfer)['signal'])
        self.assertFalse(module.spot_divergence_evidence(self.row(short_bid=.032, short_ask=.0321), self.transfer)['signal'])

    def test_missing_nan_crossed_or_wrong_contract_quotes_cannot_confirm(self):
        for field, value in [('index_price', None), ('mark_price', float('nan')), ('long_bid', 0), ('short_bid', .5), ('short_exchange', 'Bybit')]:
            row = self.row()
            row[field] = value
            self.assertIsNone(module.spot_divergence_evidence(row, self.transfer), field)

    def test_unavailable_wallet_requires_stronger_price_and_longer_confirmation(self):
        evidence = module.spot_divergence_evidence(self.row(), {'state': 'stale', 'chains': self.transfer['chains']})
        self.assertTrue(evidence['signal'])
        self.assertEqual(evidence['confirm_seconds'], 900)
        self.assertEqual(evidence['blocked_chains'], [])
        modest = self.row(long_ask=.030, long_bid=.02999)
        self.assertTrue(module.spot_divergence_evidence(modest, self.transfer)['signal'])
        self.assertFalse(module.spot_divergence_evidence(modest)['signal'])

    def test_confirm_requires_elapsed_time_and_consecutive_samples(self):
        first = self.update()[('GUA/USDT', 'Gate')]
        self.assertIsNone(first.confirmed_at)
        self.assertEqual(module.spot_divergence_payload(first, self.now)['state'], 'observing')
        self.assertIsNone(self.update(300)[('GUA/USDT', 'Gate')].confirmed_at)
        for second in range(305, 601, 5):
            item = self.update(second)[('GUA/USDT', 'Gate')]
        self.assertIsNotNone(item.confirmed_at)

    def test_restart_preserves_isolation_and_stale_data_cannot_release_it(self):
        item = self.confirm()
        item_id = item.id
        module.db.session.remove()
        item = self.update(600)[('GUA/USDT', 'Gate')]
        self.assertEqual(item.id, item_id)
        self.assertIsNotNone(item.confirmed_at)
        module.update_spot_divergences([], self.now+timedelta(seconds=700))
        self.assertIsNone(item.resolved_at)
        self.assertTrue(module.spot_divergence_payload(item, self.now+timedelta(seconds=700))['stale'])

    def test_convergence_must_last_two_minutes_and_history_stays_excluded(self):
        item = self.confirm()
        normal = self.row(long_ask=.0282, long_bid=.0281)
        for second in range(305, 425, 5):
            self.assertIn(('GUA/USDT', 'Gate'), self.update(second, [normal]))
        self.assertNotIn(('GUA/USDT', 'Gate'), self.update(425, [normal]))
        episodes = {('GUA/USDT', 'Gate'): [item]}
        self.assertTrue(module.alert_is_divergent(self.event(), episodes))
        self.assertFalse(module.alert_is_divergent(self.event(seconds=426), episodes))

    def test_summary_detail_legacy_counts_exclude_path_but_retain_other_paths_and_database_rows(self):
        self.update()
        module.db.session.add_all([self.event(), self.event('Bitget'), self.event('Gate', 'futures_futures')])
        module.db.session.commit()
        with patch.object(module, 'is_rwa_stock_pair', return_value=False):
            for view in ['summary', 'legacy', 'detail']:
                data = self.client.get('/api/alerts', query_string={'view': view, 'symbol': 'GUA/USDT'}).get_json()
                self.assertEqual(data['total'] if view == 'detail' else data['total_alerts'], 2)
                if view != 'detail':
                    self.assertEqual(len(data['isolated_paths']), 1)
                    self.assertEqual(data['events'][0]['alert_count'], 2)
        self.assertEqual(module.AlertEvent.query.count(), 3)

    def test_isolated_path_emits_no_new_alerts_and_healthy_path_still_evaluates(self):
        healthy = self.row(long_exchange='Bitget', long_ask=.0282, long_bid=.0281)
        group = [{'symbol': 'GUA/USDT', 'rows': [self.row(), healthy]}]
        with patch.object(module, 'rapid_move_alerts', return_value=[(30, .5, .6)]), patch.object(module, 'validate_spot_alert_quote', return_value=True):
            module.evaluate_alerts(group)
        events = module.AlertEvent.query.all()
        self.assertTrue(events)
        self.assertEqual({item.long_exchange for item in events}, {'Bitget'})

    def test_positive_divergence_is_also_isolated_and_invalid_quotes_do_not_refresh_evidence(self):
        row = self.row(long_ask=.020, long_bid=.01999)
        item = self.update(rows=[row])[('GUA/USDT', 'Gate')]
        before = item.last_seen_at
        bad = {**row, 'index_price': None}
        self.update(20, [bad])
        self.assertEqual(item.last_seen_at, before)
        self.assertIsNone(item.candidate_since)

    def test_missing_legacy_exchange_is_not_guessed_from_current_market(self):
        item = self.update()[('GUA/USDT', 'Gate')]
        event = self.event()
        event.long_exchange = None
        self.assertFalse(module.alert_is_divergent(event, {('GUA/USDT', 'Gate'): [item]}))
        event.message = 'Gate 现货与 Binance 合约确认后的报警'
        self.assertTrue(module.alert_is_divergent(event, {('GUA/USDT', 'Gate'): [item]}))

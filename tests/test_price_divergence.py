import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ['DATABASE_URL'] = 'sqlite://'
os.environ.setdefault('SECRET_KEY', 'divergence-test')
os.environ.setdefault('CHAT_ENCRYPTION_KEY', 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=')
import app as module
from anomaly_streams import AlertStreamHub


class PriceDivergenceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now().replace(microsecond=0)
        self.key = ('GUA/USDT', 'Gate', 'Binance')
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
        return active[self.key]

    def event(self, exchange='Gate', strategy='spot_futures', seconds=-60):
        return module.AlertEvent(symbol='GUA/USDT', strategy=strategy, long_exchange=exchange, short_exchange='Binance',
                                 alert_type='rapid_spread', message='确认后的测试', open_spread=-77, close_spread=-76.9, basis=.3,
                                 created_at=self.now+timedelta(seconds=seconds))

    def test_small_basis_and_large_executable_gap_with_blocked_chain_are_evidence(self):
        evidence = module.spot_divergence_evidence(self.row(), self.transfer)
        self.assertTrue(evidence['signal'])
        self.assertAlmostEqual(evidence['open_spread'], -76.981595, places=4)
        self.assertEqual(evidence['blocked_chains'], ['BSC 充关'])
        self.assertEqual(evidence['blocked_transfers'], [{'exchange': 'Gate', 'chain': 'BSC', 'deposit_open': False, 'withdraw_open': True}])
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
        self.assertEqual(evidence['blocked_transfers'], [])
        modest = self.row(long_ask=.030, long_bid=.02999)
        self.assertTrue(module.spot_divergence_evidence(modest, self.transfer)['signal'])
        self.assertFalse(module.spot_divergence_evidence(modest)['signal'])

    def test_confirm_requires_elapsed_time_and_consecutive_samples(self):
        first = self.update()[self.key]
        self.assertIsNone(first.confirmed_at)
        self.assertEqual(module.spot_divergence_payload(first, self.now)['state'], 'observing')
        self.assertIsNone(self.update(300)[self.key].confirmed_at)
        for second in range(305, 601, 5):
            item = self.update(second)[self.key]
        self.assertIsNotNone(item.confirmed_at)

    def test_restart_preserves_isolation_and_stale_data_cannot_release_it(self):
        item = self.confirm()
        item_id = item.id
        module.db.session.remove()
        item = self.update(600)[self.key]
        self.assertEqual(item.id, item_id)
        self.assertIsNotNone(item.confirmed_at)
        module.update_spot_divergences([], self.now+timedelta(seconds=700))
        self.assertIsNone(item.resolved_at)
        self.assertTrue(module.spot_divergence_payload(item, self.now+timedelta(seconds=700))['stale'])

    def test_convergence_must_last_two_minutes_and_history_stays_excluded(self):
        item = self.confirm()
        normal = self.row(long_ask=.0282, long_bid=.0281)
        for second in range(305, 425, 5):
            self.assertIn(self.key, self.update(second, [normal]))
        self.assertNotIn(self.key, self.update(425, [normal]))
        episodes = {self.key: [item]}
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
        module.db.session.add(self.event())
        module.db.session.commit()
        healthy = self.row(long_exchange='Bitget', long_ask=.0282, long_bid=.0281)
        healthy['alert_stream_route'] = dict(long_key=('bitget','GUAUSDT'), short_key=('bn_future','GUAUSDT'))
        group = [{'symbol': 'GUA/USDT', 'rows': [self.row(), healthy]}]
        hub = AlertStreamHub()
        with patch.object(module, 'ALERT_STREAM_HUB', hub):
            module.evaluate_alerts(group)
        self.assertEqual({path[2] for path in hub.routes}, {'Bitget'})
        for at, mid in ((1000, 100), (1001, 101)):
            for feed, price in (('bitget', 100), ('bn_future', mid)):
                hub.ingest(feed, dict(symbol='GUAUSDT', at=at, bid=price-.001, ask=price+.001, bid_qty=1, ask_qty=1), at+.001 if feed=='bn_future' else at)
        module.persist_alert_pulses(hub.batch())
        events = module.AlertEvent.query.filter(module.AlertEvent.long_exchange != 'Gate').all()
        self.assertTrue(events)
        self.assertEqual({item.long_exchange for item in events}, {'Bitget'})
        self.assertEqual(module.AlertEvent.query.filter_by(long_exchange='Gate').count(), 1)

    def test_positive_divergence_is_also_isolated_and_invalid_quotes_do_not_refresh_evidence(self):
        row = self.row(long_ask=.020, long_bid=.01999)
        item = self.update(rows=[row])[self.key]
        before = item.last_seen_at
        bad = {**row, 'index_price': None}
        self.update(20, [bad])
        self.assertEqual(item.last_seen_at, before)
        self.assertIsNone(item.candidate_since)

    def test_missing_legacy_exchange_is_not_guessed_from_current_market(self):
        item = self.update()[self.key]
        event = self.event()
        event.long_exchange = None
        self.assertFalse(module.alert_is_divergent(event, {self.key: [item]}))
        event.message = 'Gate 现货与 Binance 合约确认后的报警'
        self.assertTrue(module.alert_is_divergent(event, {self.key: [item]}))

    def test_isolation_preserves_both_legs_quote_asset_and_strategy(self):
        item = self.update()[self.key]
        episodes = {self.key: [item]}
        for field, value in [('long_exchange', 'Bitget'), ('short_exchange', 'Bybit'), ('symbol', 'GUA/USDC'), ('strategy', 'futures_futures')]:
            event = self.event()
            setattr(event, field, value)
            self.assertFalse(module.alert_is_divergent(event, episodes), (field, value))
        legacy = self.event()
        legacy.short_exchange = None
        self.assertTrue(module.alert_is_divergent(legacy, episodes))
        legacy.short_exchange = 'Binance 合约'
        self.assertTrue(module.alert_is_divergent(legacy, episodes))

    def test_only_matching_spot_row_gets_badge_and_structured_chain_ownership(self):
        self.update()
        rows = [self.row(), self.row(long_exchange='Bitget'), self.row(short_exchange='Bybit')]
        groups = [{'symbol': 'GUA/USDT', 'rows': rows}, {'symbol': 'GUA/USDC', 'rows': [self.row()]}]
        module.enrich_transfer_networks(groups)
        risk = rows[0]['price_divergence']
        self.assertEqual(risk['scope'], 'path')
        self.assertEqual(risk['strategy'], 'spot_futures')
        self.assertEqual(json.loads(risk['path_id']), ['spot_futures', 'Gate', 'Binance'])
        self.assertEqual(risk['affected_exchange'], 'Gate')
        self.assertEqual(risk['blocked_transfers'][0]['exchange'], 'Gate')
        for row in rows[1:] + groups[1]['rows']:
            self.assertIsNone(row['price_divergence'])

    def test_other_venue_extreme_does_not_suppress_healthy_path_but_same_path_still_deduplicates(self):
        module.db.session.add(self.event())
        module.db.session.commit()
        healthy = self.row(long_exchange='Bitget', long_ask=.0285, long_bid=.0284)
        for spread, expected in [(-1.4, 1), (-1.5, 1), (-1.7, 2)]:
            module.create_alert('GUA/USDT', 'rapid_spread', '确认后的测试', {**healthy, 'open_spread': spread}, 'spot_futures', 'Bitget', 'Binance')
            module.db.session.commit()
            self.assertEqual(module.AlertEvent.query.filter_by(long_exchange='Bitget').count(), expected)
        self.assertEqual(module.AlertEvent.query.filter_by(long_exchange='Gate').count(), 1)

    def test_summary_extremes_active_tips_and_path_details_keep_only_healthy_paths(self):
        self.update()
        healthy, dual = self.event('Bitget', seconds=-3), self.event('Gate', 'futures_futures', seconds=-2)
        healthy.open_spread, dual.open_spread = -1.4, 2.1
        healthy.basis, dual.basis = .2, .4
        module.db.session.add_all([self.event(seconds=-1), healthy, dual])
        module.db.session.commit()
        with patch.object(module, 'is_rwa_stock_pair', return_value=False):
            summary = self.client.get('/api/alerts?view=summary').get_json()
            self.assertEqual(summary['total_symbols'], 1)
            self.assertEqual(summary['total_alerts'], 2)
            group = summary['events'][0]
            self.assertEqual(group['move_peaks']['open_spread'], 2.1)
            self.assertEqual(group['value_ranges']['open_spread'], {'max': 2.1, 'min': -1.4})
            self.assertEqual(len(group['paths']), 2)
            self.assertEqual(summary['active_events'][0]['strategy'], 'futures_futures')
            for strategy, venue, expected in [('spot_futures', 'Gate', 0), ('spot_futures', 'Bitget', 1), ('futures_futures', 'Gate', 1)]:
                detail = self.client.get('/api/alerts', query_string={'view': 'detail', 'symbol': 'GUA/USDT', 'path': json.dumps([strategy, venue, 'Binance'])}).get_json()
                self.assertEqual(detail['total'], expected)
                self.assertTrue(all(event['long_exchange'] == venue and event['strategy'] == strategy for event in detail['items']))

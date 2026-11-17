"""Short regression scenarios for monitor freshness, fault isolation and read cost."""
import unittest
from unittest.mock import patch

from tests.test_market_flow import (FlowWebTests, NOW, engine, history, instrument,
                                    stream, tick, book)


class FlowReviewTests(unittest.TestCase):
    setUp = FlowWebTests.setUp
    tearDown = FlowWebTests.tearDown

    def prepare(self):
        monitor = self.monitor
        monitor.engine = engine()
        monitor.engine.ingest_stream(stream(now=NOW+1, closed=True, raw=history()[-1]), NOW+1)
        monitor.store().tag('DEMOUSDT', 'manual', NOW)
        monitor.scouts_at = NOW
        monitor.scout_symbols = ['DEMOUSDT']
        monitor.cache_reader = lambda: {
            'https://api.binance.com/api/v3/ticker/24hr': dict(payload=[tick()], fetched_at=1000),
            'https://api.binance.com/api/v3/ticker/bookTicker': dict(payload=[book()], fetched_at=1000)}
        return monitor

    def cycle(self, now=NOW+2, fetch=None, mono=1002):
        with patch('market_flow_service.time.time', return_value=now), \
                patch('market_flow_service.time.monotonic', return_value=mono):
            self.monitor.cycle(now, fetch or (lambda url: self.fail('unexpected network')))

    def test_global_alarm_reads_do_not_query_store_or_rescan_broad_market(self):
        m = self.prepare(); self.cycle()
        with patch.object(m, 'store', side_effect=AssertionError('HTTP must use published events')), \
                patch.object(m.engine, 'overview', side_effect=AssertionError('HTTP broad rescan')):
            for _ in range(100):
                self.assertEqual(len(m.alerts(NOW+3)['active_events']), 1)

    def test_read_time_rejects_expired_book_and_24h_statistic(self):
        m = self.prepare()
        for field, age in [('quote_at', 18), ('market_at', 42)]:
            with self.subTest(field=field):
                m.engine = engine()
                m.engine.market['DEMOUSDT'][field] = NOW-age
                row = m.engine.inspect('DEMOUSDT', NOW)
                self.assertTrue(row['candidate'])
                m.published = dict(rows=[row], manual=[], recommended=['DEMOUSDT'], checked_at=NOW)
                self.assertEqual(m.payload(NOW+5)['recommended'], [])

    def test_same_market_cache_version_does_not_rebuild_from_clock_conversion_jitter(self):
        m = self.prepare(); self.cycle()
        original = m.engine.market['DEMOUSDT']
        self.cycle(NOW+7.0001, mono=1007)
        self.assertIs(m.engine.market['DEMOUSDT'], original)

    def test_book_only_refresh_reuses_market_row_and_preserves_old_inspection(self):
        e = engine(); original = e.market['DEMOUSDT']
        snapshot = e.inspect('DEMOUSDT', NOW)
        e.ingest_market([tick()], [dict(book(), bidPrice='99.95')], NOW, NOW+5, book_at=NOW+5)
        self.assertIs(e.market['DEMOUSDT'], original)
        self.assertEqual(snapshot['market']['quote_at'], NOW)
        self.assertEqual(e.market['DEMOUSDT']['quote_at'], NOW+5)

    def test_failed_hourly_catalogue_refresh_does_not_suppress_valid_live_coin(self):
        m = self.prepare(); m.engine.catalog_at = NOW-3601
        def failed(url): raise OSError('synthetic public request failure')
        self.cycle(fetch=failed)
        self.assertEqual(m.published['checked_at'], NOW+2)
        self.assertEqual(m.payload(NOW+3)['rows'][0]['status'], 'live')
        self.assertEqual(len(m.alerts(NOW+3)['active_events']), 1)

    def test_one_bad_seed_does_not_abort_other_coin_evidence_or_alert(self):
        m = self.prepare()
        m.engine.set_catalog({'symbols': [instrument(), instrument('C0USDT')]}, NOW)
        m.scout_symbols = ['C0USDT']
        def failed(url): raise OSError('synthetic isolated seed failure')
        self.cycle(fetch=failed)
        rows = {r['contract']: r for r in m.published['rows']}
        self.assertEqual(rows['DEMOUSDT']['status'], 'live')
        self.assertFalse(rows['C0USDT']['can_notify'])
        self.assertEqual(len(m.alerts(NOW+3)['active_events']), 1)

    def test_committed_event_cache_expires_and_failed_refresh_retries_without_duplicate(self):
        m = self.prepare()
        real_store = m.store()
        with patch.object(real_store, 'history', side_effect=OSError('synthetic after-commit failure')), \
                patch.object(m, 'store', return_value=real_store):
            with self.assertRaises(OSError): self.cycle()
        self.assertTrue(m.active_dirty)
        self.cycle(NOW+3, mono=1003)
        self.assertEqual(len(real_store.history(NOW+4)['items']), 1)
        self.assertEqual(len(m.alerts(NOW+4)['active_events']), 1)
        self.assertFalse(m.active_dirty)
        # Fresh live data does not extend the frozen notice's120 second life.
        m.engine = engine(now=NOW+120)
        m.published['rows'] = [m.engine.inspect('DEMOUSDT', NOW+120)]
        m.published['checked_at'] = NOW+120
        self.assertEqual(m.alerts(NOW+123)['active_events'], [])

    def test_expired_catalogue_still_blocks_signals_on_refresh_failure(self):
        m = self.prepare(); m.engine.catalog_at = NOW-7201
        def failed(url): raise OSError('synthetic catalogue outage')
        self.cycle(fetch=failed)
        self.assertEqual(m.payload(NOW+3)['rows'][0]['status'], 'unavailable')
        self.assertEqual(m.alerts(NOW+3)['active_events'], [])

    def test_missing_final_close_is_repaired_after_reconnect(self):
        e = engine()
        e.ingest_stream(stream(now=NOW+80), NOW+80)
        self.assertEqual(e.inspect('DEMOUSDT', NOW+80)['status'], 'stale')
        self.assertTrue(e.needs_seed('DEMOUSDT', NOW+80))
        self.assertFalse(e.needs_seed('DEMOUSDT', NOW+100))

    def test_old_published_events_are_not_mutated_by_next_cycle(self):
        m = self.prepare(); self.cycle()
        original = m.published
        self.cycle(NOW+7, mono=1007)
        self.assertEqual(original['checked_at'], NOW+2)
        self.assertEqual(original['active_events'][0]['observed_at'], NOW+2)
        self.assertIsNot(original, m.published)

    def test_upstream_payload_roots_release_before_seed_network_wait(self):
        import weakref
        class Payload(list): pass
        refs = []
        m = self.prepare(); m.engine.seeded_at.clear()
        def cache():
            tickers, books = Payload([tick()]), Payload([book()])
            refs.extend([weakref.ref(tickers), weakref.ref(books)])
            return {'https://api.binance.com/api/v3/ticker/24hr': dict(payload=tickers, fetched_at=1000),
                    'https://api.binance.com/api/v3/ticker/bookTicker': dict(payload=books, fetched_at=1000)}
        m.cache_reader = cache
        def fetch(url):
            self.assertTrue(all(r() is None for r in refs), 'old public JSON retained while waiting')
            return history()
        self.cycle(fetch=fetch)
        self.assertIsNone(m.status()['last_error'])


if __name__ == '__main__':
    unittest.main()

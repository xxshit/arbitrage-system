"""Quiet-book recovery without account access or live network in tests."""
from copy import deepcopy
from dataclasses import replace
import json
import sys
import unittest
from unittest.mock import Mock, patch

from anomaly_engine import HISTORY_FIELDS, HistorySample, Sample
from anomaly_streams import AlertStreamHub, PublicWorker, Route, parse_frame
from quote_refresh import LIMITS, MAX_RTT, PublicBookRefresh, PublicHTTP, parse_snapshot


def snapshot(feed='gate', stamp=1002000, sequence=20):
    data = dict(bids=[['100','2']], asks=[['100.01','3']], id=sequence,
                current=stamp, update=990000, lastUpdateId=sequence, E=stamp, T=stamp-2)
    if feed == 'bybit':
        data = dict(retCode=0, result=dict(s='X', b=data['bids'], a=data['asks'], ts=stamp, seq=sequence, cts=stamp-2))
    return data


class SnapshotTests(unittest.TestCase):
    def parse(self, feed='gate', **kwargs):
        return parse_snapshot(feed, 'X', kwargs.pop('data', snapshot(feed)), kwargs.pop('started', 1002), kwargs.pop('received', 1002.1), kwargs.pop('elapsed', .1))

    def test_source_timestamps_never_replaced_with_receipt_time(self):
        for feed in ('gate','bybit','bn_future'):
            item = self.parse(feed)
            self.assertEqual((item['at'], item['timestamp_source']), (1002, 'exchange'))
            self.assertEqual((item['bid_qty'],item['ask_qty']), (2,3))
        item=self.parse('gate')
        self.assertEqual(item['last_change_at'],990)  # old last change is not old snapshot

    def test_binance_spot_has_conservative_explicit_request_time(self):
        item=self.parse('bn_spot')
        self.assertEqual((item['at'],item['timestamp_source']), (1002,'request_start'))
        self.assertIsNone(item['last_change_at'])
        self.assertIsNone(self.parse('bn_spot',elapsed=MAX_RTT+.001))

    def test_old_future_slow_and_clock_jumps_are_rejected(self):
        for feed in LIMITS:
            self.assertIsNone(self.parse(feed,elapsed=2))
            self.assertIsNone(self.parse(feed,received=1001))
            self.assertIsNone(self.parse(feed,received=1004))
        for feed in ('gate','bybit','bn_future'):
            for stamp in (997999,1005000):
                self.assertIsNone(self.parse(feed,data=snapshot(feed,stamp)))

    def test_invalid_crossed_empty_and_unmatched_books_are_rejected(self):
        for data in (None, [], {}, {'bids':[]}):
            self.assertIsNone(self.parse(data=data))
        for changes in (dict(bids=[]),dict(bids=[['101','2']]),dict(bids=[['nan','2']]),dict(asks=[['100.01','0']]),dict(current=None),dict(id=-1)):
            self.assertIsNone(self.parse(data={**snapshot(),**changes}))
        data=snapshot('bybit');data['result']['s']='Y'
        self.assertIsNone(self.parse('bybit',data=data))
        data=snapshot('bybit');data['retCode']=10006
        self.assertIsNone(self.parse('bybit',data=data))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.hub=AlertStreamHub()
        self.route=Route(('spot_futures','X/USDT','Gate','Binance'),('gate','X'),('bn_future','X'))
        self.hub.register('spot_futures',[self.route])
        self.owner=PublicWorker('gate',('X',),self.hub)
        self.owner.connected=True;self.owner.last_source_at=1002
        self.hub.owners[('gate','X')]=self.owner
        self.hub.ingest('gate',dict(symbol='X',bid=100,ask=100.01,bid_qty=1,ask_qty=1,at=1000,sequence=10),1000,owner=self.owner)
        self.refresh=PublicBookRefresh(self.hub)

    def item(self, sequence=20):
        return parse_snapshot('gate','X',snapshot(sequence=sequence),1002,1002.1,.1)

    def test_quiet_complete_book_recovers_and_preserves_actual_time(self):
        selected=self.refresh.choose('gate',1002)
        self.assertTrue(self.refresh.accept(selected,self.item(),1002.1))
        book=self.hub.books[('gate','X')]
        self.assertEqual((book['at'],book['last_change_at'],book['bid_qty']), (1002,990,2))
        self.assertIsNone(self.refresh.choose('gate',1002.5))
        self.assertFalse(self.hub.pending)  # confirmation alone is not a price event

    def test_inflight_response_cannot_overwrite_any_newer_websocket_quote(self):
        selected=self.refresh.choose('gate',1002)
        item=self.item()
        self.hub.ingest('gate',{**item,'at':1002.05,'bid':100.005,'sequence':21},1002.05,owner=self.owner)
        self.assertFalse(self.refresh.accept(selected,item,1002.1))
        self.assertEqual(self.hub.books[('gate','X')]['sequence'],21)

    def test_old_sequence_never_renews_a_book(self):
        selected=self.refresh.choose('gate',1002)
        self.assertFalse(self.refresh.accept(selected,self.item(sequence=9),1002.1))
        self.assertEqual(self.hub.books[('gate','X')]['at'],1000)

    def test_removed_replaced_disconnected_and_lagging_sources_are_not_revived(self):
        for case in ('removed','replaced','disconnected','lagging'):
            self.setUp()
            selected=self.refresh.choose('gate',1002)
            if case=='removed':self.hub.register('spot_futures',[])
            if case=='replaced':self.hub.owners[('gate','X')]=PublicWorker('gate',('X',),self.hub)
            if case=='disconnected':self.owner.connected=False
            if case=='lagging':self.owner.last_source_at=990
            self.assertFalse(self.refresh.accept(selected,self.item(),1002.1),case)
            self.assertIsNone(self.refresh.choose('gate',1005),case)

    def test_fresh_books_are_not_polled_and_selection_is_fair(self):
        self.assertIsNone(self.refresh.choose('gate',1001))
        another=replace(self.route,path=('spot_futures','Y/USDT','Gate','Binance'),long_key=('gate','Y'))
        self.hub.register('spot_futures',[self.route,another])
        self.hub.owners[('gate','Y')]=self.owner
        self.assertEqual(self.refresh.choose('gate',1002)[0],('gate','X'))
        self.assertEqual(self.refresh.choose('gate',1002.01)[0],('gate','Y'))
        self.assertIsNone(self.refresh.choose('gate',1002.02))

    def test_bybit_cross_endpoint_sequence_domains_do_not_silence_ws(self):
        route=replace(self.route,long_key=('bybit','X'))
        self.hub.register('spot_futures',[route])
        for stamp, seq, kind in ((1000,999999,'bybit_orderbook'),(1001,22,'bybit_ticker')):
            self.hub.ingest('bybit',dict(symbol='X',at=stamp,bid=100,ask=100.01,bid_qty=1,ask_qty=1,sequence=seq,sequence_kind=kind),stamp)
        self.assertEqual(self.hub.books[('bybit','X')]['sequence'],22)
        self.hub.ingest('bybit',dict(symbol='X',at=1002,bid=100,ask=100.01,bid_qty=1,ask_qty=1,sequence=21,sequence_kind='bybit_ticker'),1002)
        self.assertEqual(self.hub.books[('bybit','X')]['sequence'],22)

    def test_registry_changes_wake_subscription_but_metadata_does_not(self):
        self.assertTrue(self.hub.subscription_dirty.is_set())
        with patch.object(self.hub,'_start_worker'):
            self.hub.reconcile()
        self.assertFalse(self.hub.subscription_dirty.is_set())
        self.hub.register('spot_futures',[replace(self.route,metadata_at=1001,long_funding=.5)])
        self.assertFalse(self.hub.subscription_dirty.is_set())
        self.hub.register('spot_futures',[])
        self.assertTrue(self.hub.subscription_dirty.is_set())


class TransportTests(unittest.TestCase):
    def test_request_ceiling_has_no_catch_up_burst_or_retry_during_cooldown(self):
        class Clock:
            now=0.0
            def is_set(self):return self.now>=5
            def wait(self,delay):self.now+=delay
        for slow, limited in ((False,False),(True,False),(False,True)):
            clock=Clock();refresh=PublicBookRefresh(AlertStreamHub());refresh.stop=clock
            sent=[]
            def fetch(*args):
                sent.append(clock.now)
                if slow:clock.now+=.7
                if limited:refresh.cooldown['gate']=clock.now+60
                return {}
            with patch('quote_refresh.PublicHTTP'), patch('quote_refresh.time.monotonic',side_effect=lambda:clock.now), patch('quote_refresh.time.time',side_effect=lambda:1000+clock.now), patch.object(refresh,'choose',return_value=(('gate','X'),None,None)), patch.object(refresh,'fetch',side_effect=fetch), patch.object(refresh,'accept',return_value=False):
                refresh.run('gate')
            self.assertLessEqual(len(sent),31)
            self.assertTrue(all(b-a>=1/6-1e-9 for a,b in zip(sent,sent[1:])))
            if limited:self.assertEqual(len(sent),1)
            if slow:self.assertLessEqual(len(sent),8)

    def test_http_status_size_rate_budget_and_redirect_are_fail_closed(self):
        refresh=PublicBookRefresh(AlertStreamHub())
        response=Mock(status=429,headers={'Retry-After':'120'})
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        session=Mock();session.get.return_value=response
        with patch('quote_refresh.time.monotonic',return_value=10):
            with self.assertRaises(ValueError):refresh.fetch(session,'gate','X')
        self.assertEqual(refresh.cooldown['gate'],130)
        session.get.assert_called_once_with('X')
        response.status=200;response.headers={'X-MBX-USED-WEIGHT-1M':'3001'}
        response.read.side_effect=[b'x'*32769]
        with self.assertRaises(ValueError):refresh.fetch(session,'bn_spot','X')
        self.assertGreater(refresh.cooldown['bn_spot'],0)
        response.status=302;response.headers={}
        with self.assertRaises(ValueError):refresh.fetch(session,'gate','X')

    def test_public_client_only_uses_get_and_verified_tls_without_implicit_credentials(self):
        import ssl
        client=PublicHTTP('bybit')
        self.assertTrue(client.tls.check_hostname)
        self.assertEqual(client.tls.verify_mode,ssl.CERT_REQUIRED)
        with patch('quote_refresh.http.client.HTTPSConnection') as connection:
            client.get('X')
            self.assertEqual(connection.call_args.args,('api.bybit.com',))
            request=connection.return_value.request.call_args
            self.assertEqual(request.args,('GET','/v5/market/orderbook?category=linear&symbol=X&limit=1'))
            self.assertEqual(set(request.kwargs['headers']),{'Accept','User-Agent'})
            client.close()
            self.assertIsNone(client.connection)

    def test_sessions_close_and_start_is_idempotent(self):
        refresh=PublicBookRefresh(AlertStreamHub());refresh.stop.set()
        with patch('quote_refresh.PublicHTTP') as factory:
            refresh.run('gate')
            factory.assert_called_once_with('gate')
            factory.return_value.get.assert_not_called()
            factory.return_value.close.assert_called_once()
        with patch('quote_refresh.threading.Thread') as thread:
            refresh.start();refresh.start()
            self.assertEqual(thread.call_count,4)


class CompactHistoryTests(unittest.TestCase):
    def test_same_evidence_at_less_than_half_the_dictionary_storage(self):
        for metric,fields in HISTORY_FIELDS.items():
            history=HistorySample(1000,.1,metric,1,2)
            expected=dict(zip(fields,(1,2)))
            self.assertEqual(history.context,expected)
            if fields:
                self.assertLess(sys.getsizeof(history), (sys.getsizeof(Sample(1000,.1,expected))+sys.getsizeof(expected))/2)
            view=history.context;view['extraneous']=9
            self.assertEqual(history.context,expected)
            self.assertEqual(deepcopy(history).context,expected)

    def test_bybit_new_same_sequence_confirms_only_unchanged_books(self):
        state={}
        frame=dict(topic='tickers.X',type='snapshot',ts=1000000,cs=5,data=dict(symbol='X',bid1Price='100',ask1Price='101',bid1Size='2',ask1Size='3'))
        self.assertEqual(len(parse_frame('bybit',frame,1000,state)),1)
        delta=dict(topic='tickers.X',type='delta',ts=1001000,cs=5,data=dict(symbol='X',fundingRate='0.001'))
        self.assertEqual(parse_frame('bybit',delta,1001,state)[0]['at'],1001)
        self.assertEqual(parse_frame('bybit',delta,1001,state),[])
        changed={**delta,'ts':1002000,'data':dict(symbol='X',bid1Price='100.1')}
        self.assertEqual(parse_frame('bybit',changed,1002,state),[])
        self.assertEqual(len(parse_frame('bybit',{**changed,'cs':6},1002,state)),1)
        # Genuine distinct update IDs can share the same millisecond.
        self.assertEqual(len(parse_frame('bybit',{**changed,'cs':7,'data':dict(symbol='X',bid1Price='100.2')},1002,state)),1)


if __name__=='__main__':
    unittest.main()

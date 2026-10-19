from dataclasses import replace
from unittest.mock import Mock, patch
import unittest

from anomaly_streams import AlertStreamHub, PublicWorker, Route
from quote_refresh import PublicBookRefresh, parse_bulk_snapshot


def payload(feed, symbols=('X',), **changes):
    if feed == 'bn_spot':
        return [dict(symbol=s, bidPrice='100', askPrice='101', bidQty='2', askQty='3', **changes) for s in symbols]
    return dict(retCode=0, time=1002100, result=dict(category='linear', list=[
        dict(symbol=s, bid1Price='100', ask1Price='101', bid1Size='2', ask1Size='3', **changes) for s in symbols]))


class BulkParseTests(unittest.TestCase):
    def parse(self, feed='bn_spot', data=None, **kwargs):
        return parse_bulk_snapshot(feed, payload(feed) if data is None else data, {'X'},
                                   kwargs.get('started',1002), kwargs.get('received',1002.2), kwargs.get('elapsed',.2))

    def test_binance_full_bbo_uses_request_start_not_receipt(self):
        item=self.parse()['X']
        self.assertEqual((item['at'], item['timestamp_source']), (1002,'request_start'))
        self.assertEqual((item['bid'],item['ask'],item['bid_qty'],item['ask_qty']), (100,101,2,3))
        self.assertIsNone(item['last_change_at'])

    def test_bybit_snapshot_time_is_not_claimed_as_last_change(self):
        item=self.parse('bybit')['X']
        self.assertEqual((item['at'],item['timestamp_source']), (1002.1,'exchange_snapshot'))
        self.assertIsNone(item['sequence'])
        self.assertIsNone(item['last_change_at'])

    def test_slow_future_or_missing_snapshot_time_is_rejected(self):
        for feed in ('bn_spot','bybit'):
            for kwargs in (dict(elapsed=1.51),dict(received=1004),dict(received=1001),dict(elapsed=-1)):
                self.assertEqual(self.parse(feed,**kwargs),{})
        for stamp in (None, 990000, 1005000, 'nan'):
            data=payload('bybit');data['time']=stamp
            self.assertEqual(self.parse('bybit',data),{})

    def test_no_wrong_category_error_or_delta_is_accepted(self):
        for data in ({'retCode':10006}, {'retCode':0,'time':1002100,'result':{'category':'spot','list':[]}}, {}, []):
            self.assertEqual(self.parse('bybit',data),{})

    def test_incomplete_crossed_nonfinite_or_duplicate_rows_fail_closed(self):
        for key,value in (('bidQty','0'),('askPrice','99'),('bidPrice','nan')):
            data=payload('bn_spot');data[0][key]=value
            self.assertEqual(self.parse(data=data),{})
        data=payload('bn_spot');data[0].pop('bidQty')
        self.assertEqual(self.parse(data=data),{})
        data=payload('bn_spot')*3
        self.assertEqual(self.parse(data=data),{})

    def test_unsubscribed_symbols_are_never_returned(self):
        self.assertEqual(set(self.parse(data=payload('bn_spot',('X','Y','Z')))),{'X'})


class BulkRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.hub=AlertStreamHub()
        self.route=Route(('spot_futures','X/USDT','Binance','Binance'),('bn_spot','X'),('bn_future','X'))
        self.hub.register('spot_futures',[self.route])
        self.owner=PublicWorker('bn_spot',('X',),self.hub)
        self.owner.connected=True;self.owner.last_source_at=1002
        self.hub.owners[('bn_spot','X')]=self.owner
        self.hub.ingest('bn_spot',dict(symbol='X',bid=100,ask=101,bid_qty=1,ask_qty=1,at=1000),1000,owner=self.owner)
        self.refresh=PublicBookRefresh(self.hub)
        self.client=Mock()
        self.client.get_json.return_value=payload('bn_spot')

    def confirm(self, selected):
        with patch('quote_refresh.time.time',side_effect=[1002,1002.2,1002.2]), patch('quote_refresh.time.monotonic',side_effect=[50,50.2]):
            self.refresh.confirm_bulk(self.client,'bn_spot',selected)

    def test_one_read_confirms_every_quiet_subscribed_symbol_without_rest_queue(self):
        routes=[replace(self.route,path=('spot_futures',f'X{i}/USDT','Binance','Binance'),long_key=('bn_spot',f'X{i}')) for i in range(350)]
        self.hub.register('spot_futures',routes)
        for route in routes:self.hub.owners[route.long_key]=self.owner
        selected=self.refresh.choose_bulk('bn_spot',1002)
        self.assertEqual(len(selected),350)
        self.client.get_json.return_value=payload('bn_spot',tuple(f'X{i}' for i in range(350)))
        with patch('quote_refresh.time.time',return_value=1002.2),patch('quote_refresh.time.monotonic',return_value=50):
            self.refresh.confirm_bulk(self.client,'bn_spot',selected)
        self.client.get_json.assert_called_once()
        self.assertEqual(self.refresh.counts['bn_spot']['accepted'],350)
        self.assertEqual(len([key for key in self.hub.books if key[0]=='bn_spot' and key in self.hub.by_key]),350)

    def test_fresh_or_disconnected_books_are_not_polled(self):
        self.assertEqual(self.refresh.choose_bulk('bn_spot',1001),[])
        self.owner.connected=False
        self.assertEqual(self.refresh.choose_bulk('bn_spot',1002),[])
        self.owner.connected=True;self.owner.last_source_at=990
        self.assertEqual(self.refresh.choose_bulk('bn_spot',1002),[])

    def test_concurrent_websocket_quote_always_wins(self):
        selected=self.refresh.choose_bulk('bn_spot',1002)
        self.hub.ingest('bn_spot',dict(symbol='X',bid=100,ask=101,bid_qty=5,ask_qty=6,at=1002.1),1002.1,owner=self.owner)
        self.confirm(selected)
        self.assertEqual(self.hub.books[('bn_spot','X')]['bid_qty'],5)
        self.assertEqual(self.refresh.counts['bn_spot']['accepted'],0)

    def test_disconnected_removed_or_replaced_paths_are_not_revived(self):
        for mode in ('disconnect','remove','replace','lag'):
            self.setUp();selected=self.refresh.choose_bulk('bn_spot',1002)
            if mode=='disconnect':self.owner.connected=False
            if mode=='remove':self.hub.register('spot_futures',[])
            if mode=='replace':self.hub.owners[('bn_spot','X')]=PublicWorker('bn_spot',('X',),self.hub)
            if mode=='lag':self.owner.last_source_at=990
            self.confirm(selected)
            self.assertEqual(self.refresh.counts['bn_spot']['accepted'],0,mode)

    def test_confirmation_preserves_real_price_quantity_and_observation(self):
        self.confirm(self.refresh.choose_bulk('bn_spot',1002))
        self.assertEqual(self.hub.books[('bn_spot','X')]['at'],1002)
        self.assertEqual(self.hub.books[('bn_spot','X')]['bid_qty'],2)

    def test_bulk_interval_has_no_catch_up_bursts_and_closes_sessions(self):
        now=[0.0];sent=[]
        stop=Mock()
        stop.is_set.side_effect=lambda:now[0]>=10
        stop.wait.side_effect=lambda seconds:now.__setitem__(0,now[0]+seconds)
        self.refresh.stop=stop
        with patch('quote_refresh.PublicMarketHttp') as factory, patch('quote_refresh.time.monotonic',side_effect=lambda:now[0]), \
             patch('quote_refresh.time.time',side_effect=lambda:1000+now[0]),patch.object(self.refresh,'choose_bulk',return_value=[('synthetic',)]), \
             patch.object(self.refresh,'confirm_bulk',side_effect=lambda *_:sent.append(now[0])):
            self.refresh.run_bulk('bn_spot')
        self.assertEqual(sent,[0,2,4,6,8])
        factory.return_value.close.assert_called_once()

    def test_health_samples_cover_other_feeds_after_first_24_paths(self):
        routes=[Route(('futures_futures',f'A{i}/USDT','Bybit','Binance'),('bybit',f'A{i}'),('bn_future',f'A{i}')) for i in range(30)]
        self.hub.register('futures_futures',routes)
        gate=Route(('spot_futures','GUA/USDT','Gate','Binance'),('gate','GUA_USDT'),('bn_future','GUAUSDT'))
        self.hub.register('spot_futures',[gate])
        status=self.hub.status(1002)
        self.assertTrue(status['unfresh_by_feed']['gate'])
        self.assertLessEqual(len(status['unfresh_by_feed']['bybit']),6)


if __name__=='__main__':
    unittest.main()

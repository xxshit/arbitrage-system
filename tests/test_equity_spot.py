import copy
import json
import unittest

from equity_spot import XSTOCKS, build_snapshot, verified_mappings


class EquitySpotTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000.0
        self.data = {
            'futures_info': {'symbols': [dict(symbol='AAPLUSDT', baseAsset='AAPL', underlyingType='EQUITY', status='TRADING', quoteAsset='USDT', contractType='TRADIFI_PERPETUAL')]},
            'futures_books': [dict(symbol='AAPLUSDT', askPrice='102', bidPrice='101', time=self.now*1000)],
            'funding': [dict(symbol='AAPLUSDT', markPrice='100', indexPrice='100', lastFundingRate='.0002')],
            'funding_info': [dict(symbol='AAPLUSDT', fundingIntervalHours=4)],
            'equity_gate_pairs': [dict(id='AAPLX_USDT', base='AAPLX', base_name='Apple xStock', quote='USDT', trade_status='tradable')],
            'equity_gate_coins': [dict(currency='AAPLX', name='Apple xStock', category=['xstocks'], delisted=False, trade_disabled=False, chains=[dict(name='SOL', addr=XSTOCKS['AAPLX'][2])])],
            'gate_spot': [dict(currency_pair='AAPLX_USDT', lowest_ask='100', highest_bid='99')],
            'equity_bitget_instruments': {'code': '00000', 'data': [dict(symbol='RAAPLUSDT', baseCoin='rAAPL', category='SPOT', isReality='yes', status='online', quoteCoin='USDT', instrumentType='stock', isin='US0378331005', fullName='Apple')]},
            'equity_bitget_stocks': {'code': '00000', 'data': [dict(symbol='RAAPLUSDT', code='AAPL', weekendTradable='no', tradingPeriod=[])]},
            'bitget_spot': {'code': '00000', 'data': [dict(symbol='RAAPLUSDT', askPr='100', bidPr='99', ts=str(int(self.now*1000)))]},
            'equity_bybit_instruments': {'retCode': 0, 'result': {'list': [dict(symbol='AAPLXUSDT', baseCoin='AAPLX', quoteCoin='USDT', symbolType='xstocks', status='Trading', xstockMultiplier='2')]}},
            'equity_bybit_tickers': {'retCode': 0, 'result': {'list': [dict(symbol='AAPLXUSDT', ask1Price='200', bid1Price='198')]}},
        }
        self.observed = {key: self.now for key in self.data}

    def build(self, errors=None, previous=None):
        return build_snapshot(self.data, errors or {}, self.observed, self.now, previous)

    def by_venue(self, snapshot=None):
        return {row['long_exchange']: row for group in (snapshot or self.build())['symbols'] for row in group['rows']}

    def test_three_verified_sources_use_own_units_and_real_codes(self):
        result = self.build()
        values = self.by_venue(result)
        self.assertEqual(set(values), {'Gate', 'Bitget', 'Bybit'})
        for venue, row in values.items():
            self.assertEqual(row['long_ask'], 100, venue)
            self.assertEqual(row['open_spread'], 1)
            self.assertAlmostEqual(row['close_spread'], 3.03030303)
            self.assertEqual(row['funding_interval_hours'], 4)
            self.assertEqual(row['funding_rate'], .02)
            self.assertTrue(all(url.startswith('https://') for url in row['pairing_sources']))
        self.assertEqual(values['Bybit']['raw_long_ask'], 200)
        self.assertEqual(values['Gate']['units_per_token'], 1)
        self.assertEqual(values['Bitget']['spot_symbol'], 'RAAPL/USDT')
        self.assertEqual(values['Gate']['spot_symbol'], 'AAPLX/USDT')
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_bybit_multiplier_updates_and_never_assumes_one(self):
        item = self.data['equity_bybit_instruments']['result']['list'][0]
        for bad in (None, '', 'NaN', 'Infinity', 0, -1, True):
            item['xstockMultiplier'] = bad
            self.assertNotIn('Bybit', self.by_venue(), repr(bad))
        item['xstockMultiplier'] = '1.0032690125398187'
        self.assertAlmostEqual(self.by_venue()['Bybit']['long_ask'], 200/1.0032690125398187)

    def test_gate_requires_issuer_mint_product_name_and_category(self):
        original = copy.deepcopy(self.data)
        for field, value in (('name','Apple'), ('category', ['stocks']), ('trade_disabled',True), ('delisted',True), ('chains',[dict(name='SOL',addr='wrong')])):
            self.data = copy.deepcopy(original)
            self.data['equity_gate_coins'][0][field] = value
            self.assertNotIn('Gate', self.by_venue(), field)

    def test_not_arbitrary_x_r_on_or_leveraged_suffix_stripping(self):
        self.data['equity_gate_pairs'][0]['base'] = 'AAPLON'
        self.data['equity_bybit_instruments']['result']['list'][0]['baseCoin'] = 'AAPL3L'
        self.data['equity_bitget_stocks']['data'][0]['code'] = 'DIFFERENT'
        self.assertEqual(self.build()['symbols'], [])
        self.assertEqual(self.build()['errors'], {})  # valid catalog, genuinely no verified match

    def test_bitget_official_join_required_not_r_prefix(self):
        original = copy.deepcopy(self.data)
        for field, value in (('isReality','no'), ('isin',''), ('status','offline'), ('category','MARGIN'), ('baseCoin','rOTHER'), ('instrumentType','warrant')):
            self.data = copy.deepcopy(original)
            self.data['equity_bitget_instruments']['data'][0][field] = value
            self.assertNotIn('Bitget', self.by_venue(), field)
        self.data = original
        self.data['equity_bitget_instruments']['data'][0]['instrumentType'] = 'etf'
        self.assertEqual(len(list(verified_mappings('Bitget', self.data))), 1)

    def test_only_current_binance_equity_with_matching_base_is_paired(self):
        original = copy.deepcopy(self.data)
        for field, value in (('underlyingType','HK_EQUITY'), ('underlyingType','COIN'), ('status','SETTLING'), ('baseAsset','OTHER'), ('quoteAsset','USDC')):
            self.data = copy.deepcopy(original)
            self.data['futures_info']['symbols'][0][field] = value
            self.assertEqual(self.build()['symbols'], [], field)

    def test_invalid_and_crossed_books_do_not_become_opportunities(self):
        quote = self.data['gate_spot'][0]
        for ask in ('98', '0', '-1', 'nan', 'inf', True):
            quote['lowest_ask'] = ask
            self.assertNotIn('Gate', self.by_venue(), str(ask))

    def test_failed_venue_retains_old_timestamp_without_harming_healthy_ones(self):
        previous = self.build()
        self.now += 10
        self.observed = {key: self.now for key in self.data}
        self.data['futures_books'][0]['time'] = self.now*1000
        self.data['bitget_spot']['data'][0]['ts'] = self.now*1000
        snapshot = self.build({'equity_bybit_tickers':'timeout'}, previous)
        values = self.by_venue(snapshot)
        self.assertEqual(values['Bybit']['captured_at_epoch'], self.now-10)
        self.assertTrue(values['Bybit']['data_error'])
        self.assertEqual(values['Gate']['captured_at_epoch'], self.now)
        self.assertIsNone(values['Gate']['data_error'])
        self.assertEqual(set(snapshot['errors']), {'Bybit'})
        self.assertEqual(snapshot['coverage']['Bybit']['status'], 'stale')

    def test_metadata_expiry_and_incomplete_pagination_fail_closed(self):
        previous = self.build()
        self.observed['equity_bybit_instruments'] -= 21
        self.assertIn('Bybit', self.build(previous=previous)['errors'])
        self.observed['equity_bybit_instruments'] = self.now
        self.data['equity_bybit_instruments']['result']['nextPageCursor'] = 'next'
        self.assertIn('Bybit', self.build()['errors'])

    def test_per_quote_old_clock_or_future_clock_not_relabelled_now(self):
        self.data['bitget_spot']['data'][0]['ts'] = (self.now-3600)*1000
        self.assertEqual(self.by_venue()['Bitget']['captured_at_epoch'], self.now-3600)
        self.data['bitget_spot']['data'][0]['ts'] = (self.now+100)*1000
        self.assertEqual(self.by_venue()['Bitget']['captured_at_epoch'], 0)

    def test_unknown_or_stale_funding_interval_never_fabricates_eight_hours(self):
        self.data['funding_info'] = []
        self.assertIsNone(self.by_venue()['Gate']['funding_interval_hours'])
        self.data['funding_info'] = [dict(symbol='AAPLUSDT', fundingIntervalHours=4)]
        self.observed['funding_info'] -= 66
        self.assertIsNone(self.by_venue()['Gate']['funding_interval_hours'])
        self.data['funding'][0]['lastFundingRate'] = 1e308
        json.dumps(self.build(), allow_nan=False)

    def test_api_error_and_bad_common_payload_keep_stale_without_crashing(self):
        previous = self.build()
        self.data['equity_bitget_instruments']['code'] = '500'
        self.assertIn('Bitget', self.build(previous=previous)['errors'])
        self.data['futures_info'] = 'bad response'
        self.data['futures_books'] = None
        snapshot = self.build(previous=previous)
        self.assertEqual(len(snapshot['errors']), 3)
        self.assertTrue(all(row['data_error'] for row in self.by_venue(snapshot).values()))


if __name__ == '__main__':
    unittest.main()

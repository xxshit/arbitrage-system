"""Ownership of handled private futures; no app import or external I/O."""
import ast
from contextlib import nullcontext
from datetime import datetime, timezone
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import gc
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.parse import urlencode
import weakref


class Payload(dict):
    pass


class Inputs(list):
    pass


class ImmediateExecutor:
    def __init__(self):
        self.failed = []

    def submit(self, function, *args):
        result = Future()
        try:
            result.set_result(function(*args))
        except BaseException as error:
            result.set_exception(error)
            self.failed.append(result)
        return result


def load_functions():
    path = Path(__file__).resolve().parents[1] / 'app.py'
    names = {'refresh_binance_open_interest', 'refresh_okx_funding',
             'fetch_dashboard_symbol_quotes', 'collect_equity_metadata',
             'refresh_dashboard_opportunities', 'run_funding_sync_batch'}
    nodes = [n for n in ast.parse(path.read_text()).body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == len(names)
    env = dict(time=SimpleNamespace(time=lambda: 5000., monotonic=lambda: 1000.),
               as_completed=lambda futures: iter(futures), math=math, urlencode=urlencode,
               BINANCE_OPEN_INTEREST_CURSOR=0, BINANCE_OPEN_INTEREST_CACHE={},
               OKX_FUNDING_CURSOR=0, OKX_FUNDING_CACHE={}, format_funding_time=lambda t: str(t))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), env)
    return env


class FutureConsumerTests(unittest.TestCase):
    def setUp(self):
        self.env = load_functions()
        self.pool = ImmediateExecutor()
        self.env.update(MARKET_HTTP_EXECUTOR=self.pool, DASHBOARD_HTTP_EXECUTOR=self.pool)
        self.was_enabled = gc.isenabled()
        # Only measure immediate reference ownership in this generated unit test.
        gc.disable()

    def tearDown(self):
        for future in self.pool.failed:
            future.exception().__traceback__ = None
        if self.was_enabled:
            gc.enable()

    def test_open_interest_failure_releases_input_keys_view_and_keeps_success(self):
        def fetch(url, timeout):
            self.assertEqual(timeout, 1)
            if 'BAD' in url:
                raise TimeoutError('synthetic deadline')
            return {'openInterest': '123.456'}
        self.env['get_json'] = fetch
        contracts = Payload(BADUSDT={}, GOODUSDT={'bid': 1.23})
        reference = weakref.ref(contracts)
        self.env['refresh_binance_open_interest'](contracts.keys())
        self.assertEqual(self.env['BINANCE_OPEN_INTEREST_CACHE'],
                         {'GOODUSDT': {'contracts': 123.456, 'updated_at': 5000.}})
        del contracts
        self.assertIsNone(reference())

    def test_okx_failure_releases_input_and_preserves_next_settlement(self):
        def fetch(url, timeout):
            self.assertEqual(timeout, 1)
            if 'BAD' in url:
                raise TimeoutError('synthetic deadline')
            return {'data': [{'fundingRate': '0.0002', 'fundingTime': '28800000',
                              'nextFundingTime': '57600000'}]}
        self.env['get_json'] = fetch
        names = Inputs(['BAD-USDT-SWAP', 'GOOD-USDT-SWAP'])
        reference = weakref.ref(names)
        self.env['refresh_okx_funding'](names)
        self.assertEqual(self.env['OKX_FUNDING_CACHE'], {
            'GOOD-USDT-SWAP': {'funding_rate': .02, 'funding_interval_hours': 8,
                             'next_funding_time': '28800000', 'updated_at': 5000.}})
        del names
        self.assertIsNone(reference())

    def test_dashboard_failed_peer_does_not_retain_successful_raw_payload(self):
        references = []
        def fetch(url, timeout):
            self.assertEqual(timeout, 1.5)
            if 'fapi.binance.com' not in url:
                raise TimeoutError('synthetic peer unavailable')
            payload = Payload(lastPrice='12.34', priceChangePercent='1.25',
                              quoteVolume='12345.67', unused='x' * 262144)
            references.append(weakref.ref(payload))
            return payload
        self.env['get_json'] = fetch
        result = self.env['fetch_dashboard_symbol_quotes']('SYNUSDT')
        self.assertEqual(result, ('SYN/USDT', {'Binance': 12.34}, {'Binance': 5000.},
                                  {'change_24h': 1.25, 'volume_24h': 12345.67}))
        self.assertEqual(len(references), 1)
        self.assertIsNone(references[0]())

    def test_equity_failed_completed_job_releases_batch_but_keeps_retry(self):
        def fetch(url):
            if url == 'bad':
                raise TimeoutError('synthetic peer unavailable')
            return Payload(value='metadata'), 999.
        good = self.pool.submit(fetch, 'good')
        bad = self.pool.submit(fetch, 'bad')
        reference = weakref.ref(good.result()[0])
        self.env.update(EQUITY_METADATA_PENDING={'good': good, 'equity_bybit_instruments': bad},
                        EQUITY_SPOT_URLS={'good': 'good', 'equity_bybit_instruments': 'bad'},
                        EQUITY_SPOT_TTLS={'good': 3600, 'equity_bybit_instruments': 3600},
                        MARKET_PAYLOAD_CACHE={}, EQUITY_METADATA_ERRORS={},
                        EQUITY_METADATA_RETRY_AT={}, EQUITY_METADATA_POOL=self.pool,
                        fetch_equity_metadata=fetch)
        results, errors = self.env['collect_equity_metadata']()
        self.assertEqual(results, {'good': {'value': 'metadata'}})
        self.assertEqual(errors, {'equity_bybit_instruments': 'TimeoutError'})
        self.assertEqual(self.env['EQUITY_METADATA_RETRY_AT'], {'equity_bybit_instruments': 1005.})
        self.assertEqual(self.env['EQUITY_METADATA_PENDING'], {})
        self.env['MARKET_PAYLOAD_CACHE'].clear()
        del results, good, bad
        self.assertIsNone(reference())

    def test_real_pool_failure_releases_open_interest_input(self):
        def fail(*args, **kwargs):
            raise ValueError('synthetic public failure')
        self.env['get_json'] = fail
        names = Payload({f'SYN{i}USDT': {} for i in range(5)})
        reference = weakref.ref(names)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.env.update(MARKET_HTTP_EXECUTOR=pool, as_completed=as_completed)
            self.env['refresh_binance_open_interest'](names.keys())
        del names
        self.assertIsNone(reference())

    def test_dashboard_batch_releases_replaced_rows_and_keeps_failed_row_age(self):
        old_good = Payload(symbol='GOOD/USDT', updated_at_epoch=4000., estimated_profit=-2.)
        old_bad = Payload(symbol='BAD/USDT', updated_at_epoch=4100., estimated_profit=-1.)
        reference = weakref.ref(old_good)
        def fetch(symbol):
            if symbol == 'BADUSDT':
                raise TimeoutError('synthetic unavailable')
            return 'GOOD/USDT', {'Binance': 100., 'Bybit': 101.}, {'Binance': 5000., 'Bybit': 4999.}, {'change_24h': 2.}
        self.env.update(DASHBOARD_EXECUTOR=self.pool, fetch_dashboard_symbol_quotes=fetch,
                        MAJOR_MARKET_SYMBOLS=['GOODUSDT', 'BADUSDT'], datetime=datetime,
                        SHANGHAI_TZ=timezone.utc,
                        DASHBOARD_OPPORTUNITY_CACHE={'items': [old_good, old_bad], 'ts': 4000.})
        rows = self.env['refresh_dashboard_opportunities']()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['symbol'], 'GOOD/USDT')
        self.assertEqual((rows[0]['buy_price'], rows[0]['sell_price'], rows[0]['spread'],
                          rows[0]['estimated_profit'], rows[0]['updated_at_epoch']),
                         (100., 101., 1., .88, 4999.))
        self.assertIs(rows[1], old_bad)
        self.assertEqual(self.env['DASHBOARD_OPPORTUNITY_CACHE']['ts'], 4100.)
        del old_good
        self.assertIsNone(reference())

    def test_funding_batch_releases_catalog_and_plans_after_handled_failure(self):
        references, stored, completed, warnings = [], [], [], []
        rollbacks = []
        class HistoryError(Exception):
            pass
        def catalog(exchange):
            self.assertEqual(exchange, 'Binance')
            targets = Payload(GOOD={'candidates': ['GOOD']}, BAD={'candidates': ['BAD']})
            references.append(weakref.ref(targets))
            return targets
        def plan(targets, latest, now, limit):
            self.assertEqual((now, limit), (5000., 8))
            plans = Inputs([('GOOD', True), ('BAD', False)])
            references.append(weakref.ref(plans))
            return plans
        def fetch(exchange, candidates, start, end):
            if candidates == ['BAD']:
                raise HistoryError('synthetic_safe_error')
            return 'GOOD', [(4000000, -.005)]
        def done(key, target, now, latest, **status):
            completed.append((key, now, latest, status))
        schedule = SimpleNamespace(plan=plan, done=done, save=lambda status: None, last_cleanup=4999.)
        self.env.update(funding_sync_catalog=catalog,
                        funding_sync_latest=lambda exchange, targets: {'GOOD': 0, 'BAD': 0},
                        db=SimpleNamespace(session=SimpleNamespace(rollback=lambda: rollbacks.append(True))),
                        background_http_batch=lambda name: nullcontext(self.pool),
                        FUNDING_HISTORY_CLIENT=SimpleNamespace(fetch=fetch), DAY_MS=86400000,
                        FundingHistoryError=HistoryError, expected_settlement=lambda target, now: None,
                        store_settled_funding=lambda exchange, values: (stored.append((exchange, values)) or len(values)),
                        app=SimpleNamespace(logger=SimpleNamespace(warning=lambda message, *args: warnings.append((message, args)))))
        status = self.env['run_funding_sync_batch']('Binance', schedule)
        self.assertEqual((status['targets'], status['attempted'], status['inserted'], status['errors']), (2, 2, 1, 1))
        self.assertEqual(stored, [('Binance', [('GOOD', 4000000, -.005)])])
        self.assertEqual(completed, [('GOOD', 5000., 4000000, {'full': True}),
                                    ('BAD', 5000., 0, {'error': 'HistoryError'})])
        self.assertEqual(len(rollbacks), 2)
        self.assertEqual(warnings[0][1], ('Binance', 'BAD', 'synthetic_safe_error'))
        self.assertEqual(len(references), 2)
        self.assertTrue(all(ref() is None for ref in references))

    def test_shutdown_still_propagates_from_public_consumers(self):
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt('synthetic shutdown')
        self.env['get_json'] = interrupted
        for name, argument in [('refresh_binance_open_interest', ['SYNUSDT']),
                               ('refresh_okx_funding', ['SYN-USDT-SWAP']),
                               ('fetch_dashboard_symbol_quotes', 'SYNUSDT')]:
            try:
                self.env[name](argument)
            except KeyboardInterrupt as error:
                self.assertEqual(str(error), 'synthetic shutdown')
                self.assertIsNotNone(error.__traceback__)
            else:
                self.fail('shutdown was swallowed in ' + name)


if __name__ == '__main__':
    unittest.main(verbosity=2)

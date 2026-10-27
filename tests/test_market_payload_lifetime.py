"""Generated-data ownership tests; never start the app or contact an upstream."""
import ast
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import gc
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
import weakref


def load_batch(source=None):
    source = Path(source) if source else Path(__file__).resolve().parents[1] / 'app.py'
    nodes = [node for node in ast.parse(source.read_text()).body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef))
             and node.name in {'MarketPayloads', 'fetch_market_payloads'}]
    namespace = dict(time=SimpleNamespace(monotonic=lambda: 1000., time=lambda: 5000.),
                     as_completed=as_completed, MARKET_PAYLOAD_CACHE={},
                     MARKET_PAYLOAD_CACHE_LOCK=threading.Lock())
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


class Payload(dict):
    """Weak-referenceable public payload; production payload shape is unchanged."""


class ImmediateExecutor:
    def __init__(self):
        self.failed = []

    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except BaseException as error:
            future.set_exception(error)
            self.failed.append(future)
        return future


class MarketPayloadLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_batch()
        self.refs = []
        self.executor = ImmediateExecutor()
        self.ns.update(MARKET_HTTP=SimpleNamespace(get_json=self.get_json),
                       MARKET_HTTP_EXECUTOR=self.executor,
                       as_completed=lambda futures: iter(futures))
        self.was_enabled = gc.isenabled()
        # Test reference ownership, independently of automatic cyclic collection.
        # Never force collection, and never change GC in application code.
        gc.disable()

    def tearDown(self):
        # Preserve a failing baseline test without retaining its generated data.
        for future in self.executor.failed:
            future.exception().__traceback__ = None
        if self.was_enabled:
            gc.enable()

    def get_json(self, url, timeout):
        self.assertEqual(timeout, 1.5)
        if url == 'failure':
            raise TimeoutError('synthetic_deadline')
        value = Payload(url=url, nested={'value': '0.00005001', 'empty': None},
                        body='x' * (256 * 1024))
        self.refs.append(weakref.ref(value))
        return value

    def test_handled_failure_does_not_retain_other_successful_payloads(self):
        for _ in range(6):
            result, errors = self.ns['fetch_market_payloads'](
                {'first': 'a', 'second': 'b', 'broken': 'failure'})
            self.assertEqual(errors, {'broken': 'synthetic_deadline'})
            self.assertEqual(result['first']['nested'], {'value': '0.00005001', 'empty': None})
            self.assertEqual(result.observed_at, {'first': 5000., 'second': 5000.})
            self.ns['MARKET_PAYLOAD_CACHE'].clear()
            del result, errors
            self.assertTrue(all(reference() is None for reference in self.refs))
        self.assertTrue(all(future.exception().__traceback__ is None
                            for future in self.executor.failed))

    def test_failed_refresh_preserves_cache_age_and_error_text(self):
        cached = Payload(value='cached')
        reference = weakref.ref(cached)
        self.ns['MARKET_PAYLOAD_CACHE']['failure'] = dict(payload=cached, fetched_at=950.)
        result, errors = self.ns['fetch_market_payloads']({'broken': 'failure'})
        self.assertIs(result['broken'], cached)
        self.assertEqual(result.observed_at, {'broken': 4950.})
        self.assertEqual(errors, {'broken': 'using cached payload: synthetic_deadline'})
        self.assertEqual(self.ns['MARKET_PAYLOAD_CACHE']['failure']['fetched_at'], 950.)
        self.ns['MARKET_PAYLOAD_CACHE'].clear()
        del result, errors, cached
        self.assertIsNone(reference())

    def test_fresh_metadata_cache_does_not_submit_a_request(self):
        cached = Payload(value='metadata')
        self.ns['MARKET_PAYLOAD_CACHE']['failure'] = dict(payload=cached, fetched_at=950.)
        result, errors = self.ns['fetch_market_payloads']({'metadata': 'failure'}, {'metadata': 3600})
        self.assertIs(result['metadata'], cached)
        self.assertEqual(result.observed_at, {'metadata': 4950.})
        self.assertEqual(errors, {})
        self.assertEqual(self.executor.failed, [])

    def test_real_worker_future_failure_releases_batch_after_return(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            self.ns.update(MARKET_HTTP_EXECUTOR=pool, as_completed=as_completed)
            result, errors = self.ns['fetch_market_payloads'](
                {'first': 'a', 'second': 'b', 'broken': 'failure'})
        self.assertEqual(errors, {'broken': 'synthetic_deadline'})
        self.ns['MARKET_PAYLOAD_CACHE'].clear()
        del result, errors
        self.assertTrue(all(reference() is None for reference in self.refs))

    def test_shutdown_exception_still_propagates_with_its_traceback(self):
        def interrupted(*_):
            raise KeyboardInterrupt('synthetic_shutdown')
        self.ns['MARKET_HTTP'].get_json = interrupted
        try:
            self.ns['fetch_market_payloads']({'first': 'a'})
        except KeyboardInterrupt as error:
            # unittest.assertRaises itself clears tracebacks on context exit.
            self.assertIs(error, self.executor.failed[0].exception())
            self.assertIsNotNone(error.__traceback__)
        else:
            self.fail('shutdown exception was swallowed')


if __name__ == '__main__':
    unittest.main(verbosity=2)

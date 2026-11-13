"""Real transport/batch boundaries over finite generated responses; no app import."""
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import gzip
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

import market_transport as transport
from test_market_transport import Response, Connection, URL
from test_market_payload_lifetime import load_batch


class MarketWireOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.client = transport.PublicMarketHttp(connections_per_host=1)

    def tearDown(self):
        self.client.close()

    def test_worker_future_has_only_wire_and_caller_decodes_exact_tree(self):
        payload = {'retCode': 0, 'data': [{'p': '0.000000001', 'q': None, 'n': -0.0}]}
        conn = Connection([Response(payload, 'gzip')])
        actual = transport.decode_json
        calls = []
        with ThreadPoolExecutor(max_workers=1) as pool, \
                patch.object(transport.http.client, 'HTTPSConnection', return_value=conn), \
                patch.object(transport, 'decode_json', side_effect=lambda *a: (calls.append(threading.get_ident()) or actual(*a))):
            future = pool.submit(self.client.get_wire, URL)
            wire = future.result()
            self.assertEqual(calls, [])
            self.assertIsInstance(wire._body, bytes)
            self.assertEqual(wire.decode(), payload)
            self.assertEqual(calls, [threading.get_ident()])
            self.assertIsNone(future.result()._body)
            self.assertIsNone(wire._client)
            self.assertIsNone(wire._connection)
        self.assertEqual(self.client.pools['api.bybit.com'].qsize(), 1)
        self.assertEqual(self.client.status()[URL]['attempts'], 1)

    def test_lease_stays_bounded_until_decode_and_is_returned_once(self):
        conn = Connection([Response()])
        with patch.object(transport.http.client, 'HTTPSConnection', return_value=conn):
            wire = self.client.get_wire(URL)
            with self.assertRaisesRegex(TimeoutError, 'connection_wait_timeout'):
                self.client.get_wire(URL, timeout=.01)
            wire.decode()
            wire.close()
            with self.assertRaisesRegex(RuntimeError, 'already_consumed'):
                wire.decode()
        self.assertEqual(self.client.pools['api.bybit.com'].qsize(), 1)
        self.assertFalse(conn.closed)

    def test_late_decode_respects_original_deadline_and_preserves_no_success(self):
        conn = Connection([Response()])
        clock = [100.]
        with patch.object(transport.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(transport.http.client, 'HTTPSConnection', return_value=conn):
            wire = self.client.get_wire(URL, timeout=1.5)
            clock[0] = 101.6
            with self.assertRaisesRegex(TimeoutError, 'deadline'):
                wire.decode()
        self.assertTrue(conn.closed)
        self.assertIsNone(wire._body)
        self.assertNotIn('last_success_at', self.client.status()[URL])
        self.assertEqual(self.client.pools['api.bybit.com'].qsize(), 1)

    def test_decode_time_itself_counts_against_deadline(self):
        conn = Connection([Response()])
        clock = [100.]
        real = transport.decode_json
        def slow(*args):
            answer = real(*args)
            clock[0] += 2
            return answer
        with patch.object(transport.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(transport.http.client, 'HTTPSConnection', return_value=conn), \
                patch.object(transport, 'decode_json', side_effect=slow):
            wire = self.client.get_wire(URL)
            with self.assertRaises(TimeoutError):
                wire.decode()
        self.assertTrue(conn.closed)

    def test_corruption_and_api_limit_still_close_release_and_back_off(self):
        for response, error in [(Response(encoding='gzip', raw=b'bad'), OSError),
                                (Response({'retCode': 10006}), ValueError),
                                (Response(raw=b'not json'), ValueError)]:
            with self.subTest(error=error):
                self.client.cooldowns.clear()
                conn = Connection([response])
                with patch.object(transport.http.client, 'HTTPSConnection', return_value=conn):
                    wire = self.client.get_wire(URL)
                    with self.assertRaises(error):
                        wire.decode()
                self.assertTrue(conn.closed)
                self.assertIsNone(wire._body)
                self.assertEqual(self.client.pools['api.bybit.com'].qsize(), 1)
                if error is ValueError and response.stream.getvalue().startswith(b'{'):
                    self.assertIn('api.bybit.com', self.client.cooldowns)

    def test_callback_discard_closes_unconsumed_completed_wire(self):
        conn = Connection([Response()])
        with patch.object(transport.http.client, 'HTTPSConnection', return_value=conn):
            future = Future()
            wire = self.client.get_wire(URL)
            future.set_result(wire)
            future.add_done_callback(transport.discard_market_response)
            transport.discard_market_response(future)
        self.assertTrue(conn.closed)
        self.assertIsNone(wire._body)
        self.assertEqual(self.client.status()[URL]['attempts'], 1)
        self.assertEqual(self.client.pools['api.bybit.com'].qsize(), 1)

    def test_batch_submission_failure_discards_already_issued_wire(self):
        env = load_batch()
        conn = Connection([Response()])
        issued = []
        class RejectSecond:
            def submit(_, fn, *args):
                if issued:
                    raise RuntimeError('synthetic submit rejected')
                f = Future()
                f.set_result(fn(*args))
                issued.append(f)
                return f
        env.update(MARKET_HTTP=self.client, MARKET_HTTP_EXECUTOR=RejectSecond())
        with patch.object(transport.http.client, 'HTTPSConnection', return_value=conn):
            with self.assertRaisesRegex(RuntimeError, 'submit rejected'):
                env['fetch_market_payloads']({'first': URL, 'second': URL+'&symbol=X'})
        self.assertTrue(conn.closed)
        self.assertIsNone(issued[0].result()._body)
        self.assertEqual(self.client.pools['api.bybit.com'].qsize(), 1)

    def test_two_cache_consumers_share_bounded_pool_without_decoder_workers(self):
        client = transport.PublicMarketHttp(connections_per_host=2)
        env = load_batch()
        env['time'] = time
        data = {'retCode': 0, 'data': [{'p': '12.000000001'}]}
        class Reusable(Connection):
            def getresponse(self):
                return Response(data, 'gzip')
        conns = []
        def factory(*a, **kw):
            conn = Reusable([])
            conns.append(conn)
            return conn
        decoder_threads = set()
        consumer_threads = set()
        decode = transport.decode_json
        def record(*args):
            decoder_threads.add(threading.get_ident())
            return decode(*args)
        barrier = threading.Barrier(2)
        def caller(offset):
            consumer_threads.add(threading.get_ident())
            barrier.wait(2)
            return env['fetch_market_payloads']({str(i): URL+'&symbol=S'+str(i+offset) for i in range(6)})
        with ThreadPoolExecutor(max_workers=12) as io, ThreadPoolExecutor(max_workers=2) as callers, \
                patch.object(transport.http.client, 'HTTPSConnection', side_effect=factory), \
                patch.object(transport, 'decode_json', side_effect=record):
            env.update(MARKET_HTTP=client, MARKET_HTTP_EXECUTOR=io)
            jobs = [callers.submit(caller, i) for i in (0, 3)]
            for job in jobs:
                result, errors = job.result(timeout=4)
                self.assertEqual(errors, {})
                self.assertEqual(list(result.values()), [data]*6)
        self.assertEqual(decoder_threads, consumer_threads)
        self.assertEqual(len(env['MARKET_PAYLOAD_CACHE']), 9)
        self.assertEqual(len(conns), 2)
        self.assertEqual(client.pools['api.bybit.com'].qsize(), 2)
        client.close()

    def test_successful_wire_future_never_keeps_decoded_cache_generations(self):
        class Root(dict):
            pass
        env = load_batch()
        futures, refs = [], []
        class KeepFutures:
            def submit(_, fn, *args):
                f = Future()
                f.set_result(fn(*args))
                futures.append(f)
                return f
        decode = transport.decode_json
        def witnessed(*args):
            raw, size = decode(*args)
            root = Root(raw)
            refs.append(weakref.ref(root))
            return root, size
        conn = Connection([Response(), Response()])
        env.update(MARKET_HTTP=self.client, MARKET_HTTP_EXECUTOR=KeepFutures())
        with patch.object(transport.http.client, 'HTTPSConnection', return_value=conn), \
                patch.object(transport, 'decode_json', side_effect=witnessed):
            for _ in range(2):
                result, errors = env['fetch_market_payloads']({'one': URL})
                self.assertFalse(errors)
                del result
            self.assertIsNone(refs[0]())
            env['MARKET_PAYLOAD_CACHE'].clear()
        self.assertTrue(all(ref() is None for ref in refs))
        self.assertTrue(all(f.result()._body is None for f in futures))

    def test_close_racing_with_decode_leaves_one_owner_and_one_pool_slot(self):
        entered, release = threading.Event(), threading.Event()
        real = transport.decode_json
        def blocked(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return real(*args)
        conn = Connection([Response()])
        with ThreadPoolExecutor(max_workers=1) as pool, \
                patch.object(transport.http.client, 'HTTPSConnection', return_value=conn), \
                patch.object(transport, 'decode_json', side_effect=blocked):
            wire = self.client.get_wire(URL)
            future = pool.submit(wire.decode)
            self.assertTrue(entered.wait(2))
            try:
                wire.close()
                self.assertEqual(self.client.pools['api.bybit.com'].qsize(), 0)
                self.assertFalse(conn.closed)
            finally:
                release.set()
            future.result(2)
        self.assertEqual(self.client.pools['api.bybit.com'].qsize(), 1)
        self.assertEqual(self.client.status()[URL]['attempts'], 1)

    def test_interrupted_batch_cancels_queued_and_discards_running_result(self):
        env = load_batch()
        started, release = threading.Event(), threading.Event()
        issued = []
        conn = Connection([Response()])
        with ThreadPoolExecutor(max_workers=1) as pool, \
                patch.object(transport.http.client, 'HTTPSConnection', return_value=conn):
            class Executor:
                def submit(_, fn, *args):
                    def delayed():
                        started.set()
                        if not release.wait(2):
                            raise RuntimeError('test barrier timeout')
                        return fn(*args)
                    future = pool.submit(delayed)
                    issued.append(future)
                    return future
            def interrupted(futures):
                self.assertTrue(started.wait(2))
                raise KeyboardInterrupt('synthetic consumer shutdown')
            env.update(MARKET_HTTP=self.client, MARKET_HTTP_EXECUTOR=Executor(), as_completed=interrupted)
            try:
                with self.assertRaises(KeyboardInterrupt):
                    env['fetch_market_payloads']({'first': URL, 'second': URL+'&symbol=X'})
                self.assertTrue(issued[1].cancelled())
            finally:
                release.set()
                # Keep the synthetic connection factory installed until the
                # running read and its completion callback have both exited.
                pool.shutdown(wait=True)
        self.assertTrue(conn.closed)
        self.assertIsNone(issued[0].result()._body)
        self.assertEqual(self.client.pools['api.bybit.com'].qsize(), 1)

    def test_older_concurrent_batch_cannot_replace_newer_cache_generation(self):
        env = load_batch()
        old_started, new_done = threading.Event(), threading.Event()
        clock = threading.local()
        env['time'] = SimpleNamespace(monotonic=lambda: clock.tick, time=lambda: 5000.+clock.tick)
        def wire(url, timeout):
            return SimpleNamespace(close=lambda: None, decode=lambda: decode())
        def decode():
            if clock.tick == 1000.:
                old_started.set()
                self.assertTrue(new_done.wait(2))
                return {'version': 'old', 'exact': '0.00000001'}
            return {'version': 'new', 'exact': '0.00000002'}
        def caller(tick):
            clock.tick = tick
            result = env['fetch_market_payloads']({'one': URL})
            if tick == 1001.:
                new_done.set()
            return result
        with ThreadPoolExecutor(max_workers=2) as callers, ThreadPoolExecutor(max_workers=2) as io:
            env.update(MARKET_HTTP=SimpleNamespace(get_wire=wire), MARKET_HTTP_EXECUTOR=io)
            old = callers.submit(caller, 1000.)
            self.assertTrue(old_started.wait(2))
            new = callers.submit(caller, 1001.)
            newest, errors = new.result(2)
            previous, old_errors = old.result(2)
        self.assertFalse(errors or old_errors)
        self.assertEqual(previous['one']['version'], 'old')
        self.assertEqual(newest['one']['version'], 'new')
        self.assertEqual(previous.observed_at['one'], 6000.)
        self.assertEqual(newest.observed_at['one'], 6001.)
        self.assertIs(env['MARKET_PAYLOAD_CACHE'][URL]['payload'], newest['one'])


if __name__ == '__main__':
    unittest.main()

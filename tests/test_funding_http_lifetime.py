"""Generated HTTP error resources; no app startup, network or GC changes."""
from concurrent.futures import ThreadPoolExecutor
import gc
import http.client
import io
import os
import socket
import unittest
from urllib.error import HTTPError
import weakref

from funding_history import FundingHistoryClient, FundingHistoryError, RequestGate


class FundingHttpLifetimeTests(unittest.TestCase):
    def client(self, get):
        client = FundingHistoryClient(get)
        self.clock = [1000.]
        client.gates['Binance'] = RequestGate(0, lambda: self.clock[0])
        return client

    def test_completed_failed_futures_do_not_keep_error_responses(self):
        references, closed = [], []
        enabled, thresholds = gc.isenabled(), gc.get_threshold()

        class Body(io.BytesIO):
            def close(self):
                if not self.closed:
                    closed.append(True)
                super().close()

        def get(url, timeout):
            body = Body(b'synthetic error body')
            references.append(weakref.ref(body))
            raise HTTPError(url, 503, 'synthetic unavailable', {}, body)

        client = self.client(get)
        # Real batches keep completed futures while waiting on other targets.
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(client.request, 'Binance', 'https://example.invalid')
                       for _ in range(8)]
            for future in pending:
                try:
                    future.result()
                except FundingHistoryError as error:
                    self.assertEqual(str(error), 'http_503')
                    # Match the existing handled-future consumer.
                    error.__traceback__ = None
            self.assertEqual(len(closed), 8)
            self.assertTrue(all(ref() is None for ref in references))
            self.assertTrue(all(f.exception().__context__ is None for f in pending))
        self.assertEqual((gc.isenabled(), gc.get_threshold()), (enabled, thresholds))

    def test_http_response_closed_even_when_error_is_retained_by_caller(self):
        body = io.BytesIO(b'private generated response')
        original = HTTPError('https://example.invalid', 404, 'unused details', {}, body)
        def get(*args, **kwargs):
            raise original
        client = self.client(get)
        with self.assertRaisesRegex(FundingHistoryError, '^http_404$') as caught:
            client.request('Binance', 'https://example.invalid')
        self.assertTrue(body.closed)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_real_response_releases_socket_before_failed_future_is_discarded(self):
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        reader.settimeout(1)
        writer.sendall(b'HTTP/1.1 503 Unavailable\r\nContent-Length: 4\r\n\r\nbody')
        response = http.client.HTTPResponse(reader)
        response.begin()
        self.addCleanup(response.close)
        descriptor = response.fileno()
        reader.close()  # urllib also leaves only the response's file reference.
        os.fstat(descriptor)
        def get(url, timeout):
            raise HTTPError(url, response.status, response.reason, response.headers, response)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.client(get).request, 'Binance', 'https://example.invalid')
            with self.assertRaisesRegex(FundingHistoryError, '^http_503$'):
                future.result()
            self.assertTrue(response.isclosed())
            with self.assertRaises(OSError):
                os.fstat(descriptor)
            self.assertIsNone(future.exception().__context__)

    def test_error_response_close_preserves_rate_limit_policy(self):
        for code, header, delay in ((403, '2', 600), (418, '120', 120),
                                    (429, 'NaN', 60), (429, 'invalid', 60),
                                    (429, '999999', 86400), (503, '120', 0)):
            with self.subTest(code=code, header=header):
                body = io.BytesIO(b'generated')
                def get(url, timeout):
                    raise HTTPError(url, code, 'generated', {'Retry-After': header}, body)
                client = self.client(get)
                with self.assertRaisesRegex(FundingHistoryError, '^http_' + str(code) + '$'):
                    client.request('Binance', 'https://example.invalid')
                self.assertTrue(body.closed)
                self.assertEqual(client.gates['Binance'].blocked_until, 1000.+delay if delay else 0)
                if delay:
                    with self.assertRaisesRegex(FundingHistoryError, '^rate_backoff$'):
                        client.request('Binance', 'https://example.invalid')

    def test_non_http_error_and_shutdown_keep_original_identity(self):
        for original in (TimeoutError('generated deadline'), KeyboardInterrupt('generated stop')):
            with self.subTest(error=type(original).__name__):
                def get(*args, **kwargs):
                    raise original
                try:
                    self.client(get).request('Binance', 'https://example.invalid')
                except BaseException as error:
                    self.assertIs(error, original)
                    self.assertIsNotNone(error.__traceback__)
                else:
                    self.fail('error was swallowed')

    def test_success_keeps_complete_payload(self):
        payload = [{'fundingTime': 123456, 'fundingRate': '-0.00005001'}]
        self.assertIs(self.client(lambda *a, **k: payload).request(
            'Binance', 'https://example.invalid'), payload)


if __name__ == '__main__':
    unittest.main()

"""Public JSON reads reuse certificate verification, never cache observations."""
import gzip
import io
import os
import ssl
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

os.environ['DATABASE_URL'] = 'sqlite://'
os.environ.setdefault('SECRET_KEY', 'public-json-context-test')
import app as module


class Response:
    def __init__(self, body, encoding=''):
        self.stream = io.BytesIO(body)
        self.headers = {'Content-Encoding': encoding}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True
        self.stream.close()

    def read(self, size):
        return self.stream.read(size)


class PublicJsonContextTests(unittest.TestCase):
    def test_each_read_uses_same_verified_context_and_new_response(self):
        responses = [Response(b'{"value":1}'), Response(b'{"value":2}')]
        with patch.object(module, 'urlopen', side_effect=responses) as opened:
            first = module.get_json('https://fapi.binance.com/fapi/v1/premiumIndex', timeout=2)
            second = module.get_json('https://www.okx.com/api/v5/public/funding-rate', timeout=3)
        self.assertEqual((first, second), ({'value': 1}, {'value': 2}))
        for call, timeout in zip(opened.call_args_list, (2, 3)):
            self.assertIs(call.kwargs['context'], module.MARKET_HTTP.context)
            self.assertTrue(call.kwargs['context'].check_hostname)
            self.assertEqual(call.kwargs['context'].verify_mode, ssl.CERT_REQUIRED)
            self.assertEqual(call.kwargs['timeout'], timeout)
            self.assertEqual(call.args[0].get_method(), 'GET')
            self.assertEqual(call.args[0].get_header('Accept-encoding'), 'gzip')
            self.assertIsNone(call.args[0].get_header('Authorization'))
        self.assertTrue(all(response.closed for response in responses))

    def test_gzip_precision_and_response_cleanup_preserved(self):
        response = Response(gzip.compress(b'{"fundingRate":"0.00005001"}'), 'gzip')
        with patch.object(module, 'urlopen', return_value=response):
            self.assertEqual(module.get_json('https://fapi.binance.com/fapi/v1/fundingRate'),
                             {'fundingRate': '0.00005001'})
        self.assertTrue(response.closed)

    def test_malformed_body_closes_response_without_replacing_tls_context(self):
        context = module.MARKET_HTTP.context
        response = Response(b'not json')
        with patch.object(module, 'urlopen', return_value=response), self.assertRaises(ValueError):
            module.get_json('https://fapi.binance.com/fapi/v1/fundingRate')
        self.assertTrue(response.closed)
        self.assertIs(context, module.MARKET_HTTP.context)

    def test_concurrent_reads_do_not_construct_more_certificate_stores(self):
        context = module.MARKET_HTTP.context
        def open_response(request, *, timeout, context):
            self.assertIs(context, module.MARKET_HTTP.context)
            return Response(b'{"ok":true}')
        with patch.object(module, 'urlopen', side_effect=open_response), \
                patch.object(ssl, 'create_default_context', side_effect=AssertionError('new CA store')):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: module.get_json('https://fapi.binance.com/fapi/v1/premiumIndex'), range(64)))
        self.assertEqual(results, [{'ok': True}]*64)
        self.assertIs(context, module.MARKET_HTTP.context)


if __name__ == '__main__':
    unittest.main()

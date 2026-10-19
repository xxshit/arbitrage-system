import gzip
import http.client
import io
import json
import unittest
from unittest.mock import patch

import market_transport as transport
from market_status import snapshot_delays

URL = "https://api.bybit.com/v5/market/tickers?category=linear"


class Response:
    def __init__(self, payload=None, encoding="", status=200, headers=None, raw=None):
        self.status, self.headers = status, {"Content-Encoding": encoding, **(headers or {})}
        body = json.dumps(payload if payload is not None else {"retCode": 0, "result": {"list": []}}).encode()
        if encoding == "gzip":
            body = gzip.compress(body)
        self.stream = io.BytesIO(raw if raw is not None else body)

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        return self.stream.read(size)


class Connection:
    def __init__(self, responses):
        self.responses = responses
        self.sock, self.timeout, self.closed = None, None, False
        self.requests = []

    def request(self, method, path, headers):
        self.requests.append((method, path, headers))

    def getresponse(self):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


class PublicTransportTests(unittest.TestCase):
    def setUp(self):
        self.client = transport.PublicMarketHttp(connections_per_host=1)

    def run_response(self, response):
        connection = Connection([response])
        with patch.object(transport.http.client, "HTTPSConnection", return_value=connection):
            result = self.client.get_json(URL)
        return result, connection

    def test_compression_keeps_exact_fields_and_reuses_tls_connection(self):
        data = {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "bid1Price": "123.00000001"}]}}
        connection = Connection([Response(data, "gzip"), Response(data, "gzip")])
        with patch.object(transport.http.client, "HTTPSConnection", return_value=connection) as factory:
            self.assertEqual(self.client.get_json(URL), data)
            self.assertEqual(self.client.get_json(URL), data)
        self.assertEqual(factory.call_count, 1)
        self.assertFalse(connection.closed)
        self.assertEqual(connection.requests[0][0], "GET")
        self.assertEqual(connection.requests[0][2]["Accept-Encoding"], "gzip")
        self.assertNotIn("Authorization", connection.requests[0][2])
        self.assertEqual(self.client.status()[URL]["attempts"], 2)
        self.assertTrue(self.client.status()[URL]["compressed"])

    def test_uncompressed_response_remains_supported(self):
        result, _ = self.run_response(Response())
        self.assertEqual(result["retCode"], 0)
        self.assertFalse(self.client.status()[URL]["compressed"])

    def test_only_allowlisted_public_https_reads(self):
        invalid = [URL.replace("https:", "http:"), URL.replace("api.bybit.com", "evil.example"),
                   URL.replace("/market/tickers", "/order/create"), URL+"&signature=x", URL+"&signature=",
                   URL+"#secret", URL.replace("api.bybit.com", "key@api.bybit.com"),
                   URL.replace("api.bybit.com", "api.bybit.com:444"), URL+"\n", URL+"&api_key=abc"]
        with patch.object(transport.http.client, "HTTPSConnection") as factory:
            for url in invalid:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    self.client.get_json(url)
            factory.assert_not_called()

    def test_redirect_is_not_followed(self):
        connection = Connection([Response(status=302, headers={"Location": "https://evil.example"})])
        with patch.object(transport.http.client, "HTTPSConnection", return_value=connection) as factory:
            with self.assertRaisesRegex(RuntimeError, "http_302"):
                self.client.get_json(URL)
        self.assertEqual(factory.call_count, 1)
        self.assertTrue(connection.closed)

    def test_api_error_does_not_pass_as_http_success_or_reflect_message(self):
        connection = Connection([Response({"retCode": 10001, "retMsg": "private upstream message"})])
        with patch.object(transport.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(ValueError, "api_error_10001"):
                self.client.get_json(URL)
        metric = self.client.status()[URL]
        self.assertNotIn("private", metric["last_error"])
        self.assertNotIn("last_success_at", metric)
        self.assertEqual(metric["failures"], 1)

    def test_all_exchange_api_error_envelopes_are_rejected(self):
        for payload in ({"code": "50011"}, {"code": -1003}, {"code": "40015"},
                        {"code": False}, {"label": "ERROR", "message": "x"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                transport.check_api_success(payload)
        for payload in ([], {"code": 0}, {"code": "0"}, {"code": "00000"}, {"retCode": 0}):
            transport.check_api_success(payload)

    def test_429_cooldown_respects_retry_after_and_never_retries_immediately(self):
        connection = Connection([Response(status=429, headers={"Retry-After": "180"})])
        with patch.object(transport.http.client, "HTTPSConnection", return_value=connection) as factory:
            with self.assertRaisesRegex(RuntimeError, "http_429"):
                self.client.get_json(URL)
            with self.assertRaisesRegex(RuntimeError, "backoff"):
                self.client.get_json(URL)
        self.assertEqual(factory.call_count, 1)
        self.assertGreater(self.client.cooldowns["api.bybit.com"]-transport.time.monotonic(), 175)

    def test_bybit_http200_rate_limit_also_backs_off(self):
        connection = Connection([Response({"retCode": 10006})])
        with patch.object(transport.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaises(ValueError):
                self.client.get_json(URL)
            with self.assertRaisesRegex(RuntimeError, "backoff"):
                self.client.get_json(URL)

    def test_binance_shared_ip_budget_keeps_valid_read_but_pauses_further_probes(self):
        url='https://api.binance.com/api/v3/ticker/bookTicker'
        connection=Connection([Response([],headers={'X-MBX-USED-WEIGHT-1M':'3100'})])
        with patch.object(transport.http.client,'HTTPSConnection',return_value=connection):
            self.assertEqual(self.client.get_json(url,weight_limit=3000),[])
            with self.assertRaisesRegex(RuntimeError,'backoff'):
                self.client.get_json(url,weight_limit=3000)
        self.client.close()
        self.assertTrue(connection.closed)

    def test_dead_keepalive_retries_only_once_and_recovers(self):
        dead, good = Connection([http.client.RemoteDisconnected()]), Connection([Response()])
        with patch.object(transport.http.client, "HTTPSConnection", side_effect=[dead, good]) as factory:
            self.assertEqual(self.client.get_json(URL)["retCode"], 0)
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(dead.closed)
        self.assertFalse(good.closed)

    def test_partial_or_corrupt_gzip_closes_connection(self):
        for raw in (b"not gzip", gzip.compress(b'{"retCode":0}')[:-5]):
            connection = Connection([Response(encoding="gzip", raw=raw)])
            with patch.object(transport.http.client, "HTTPSConnection", return_value=connection):
                with self.assertRaises((OSError, EOFError)):
                    self.client.get_json(URL)
            self.assertTrue(connection.closed)

    def test_slow_trickle_is_bounded_by_total_deadline_not_per_read_timeout(self):
        now = [100.0]
        response = Response()
        def trickle(_):
            now[0] += 0.6
            return b" "
        response.read1 = trickle
        connection = Connection([response])
        with patch.object(transport.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(transport.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(TimeoutError, "deadline"):
                self.client.get_json(URL, timeout=1.5)
        self.assertLess(now[0], 102)
        self.assertTrue(connection.closed)

    def test_body_and_decompression_sizes_are_bounded(self):
        with patch.object(transport, "MAX_BODY", 100):
            with self.assertRaisesRegex(ValueError, "too_large"):
                transport.decode_json(b" "*101)
            with self.assertRaisesRegex(ValueError, "too_large"):
                transport.decode_json(gzip.compress(b" "*10000), "gzip")

    def test_failure_releases_pool_and_later_success_clears_current_error(self):
        bad, good = Connection([Response(status=503)]), Connection([Response()])
        with patch.object(transport.http.client, "HTTPSConnection", side_effect=[bad, good]):
            with self.assertRaises(RuntimeError):
                self.client.get_json(URL)
            self.client.get_json(URL)
        self.assertEqual(self.client.status()[URL]["consecutive_failures"], 0)
        self.assertIsNone(self.client.status()[URL]["last_error"])
        self.assertEqual(self.client.status()[URL]["failures"], 1)

    def test_metric_history_is_bounded_and_returned_by_copy(self):
        for i in range(270):
            self.client._record(URL+"&symbol="+str(i), transport.time.monotonic(), transport.time.time())
        result = self.client.status()
        self.assertEqual(len(result), 256)
        result[next(iter(result))]["attempts"] = 999
        self.assertNotEqual(self.client.status()[next(iter(result))]["attempts"], 999)


class SourceAttributionTests(unittest.TestCase):
    def test_gate_failure_does_not_blame_binance_or_bitget_spot_paths(self):
        groups = [{"symbol": "GUA/USDT", "rows": [dict(long_exchange=v, short_exchange="Binance 合约") for v in ("Gate", "Binance", "Bitget")]}]
        result = snapshot_delays(groups, {"gate_spot": "TimeoutError"}, {"gate_spot": 123}, "spot_futures")
        self.assertEqual(result["affected_paths"], 1)
        self.assertEqual(result["sources"][0]["examples"][0]["long_exchange"], "Gate")
        self.assertEqual(result["sources"][0]["observed_at_epoch"], 123)

    def test_binance_future_failure_affects_all_spot_legs_without_double_count(self):
        groups = [{"symbol": "BTC/USDT", "rows": [dict(long_exchange=v, short_exchange="Binance 合约") for v in ("Gate", "Binance")]}]
        result = snapshot_delays(groups, {"futures_books": "timeout", "funding": "timeout"}, {}, "spot_futures")
        self.assertEqual(result["affected_paths"], 2)
        self.assertEqual(len(result["sources"]), 2)

    def test_dual_venue_failures_only_affect_paths_using_that_venue(self):
        groups = [{"symbol": "BTC/USDT", "rows": [dict(long_exchange=a, short_exchange=b) for a,b in (("Bybit","Binance"),("OKX","Binance"),("Bybit","OKX"))]}]
        result = snapshot_delays(groups, {"bybit_tickers": "upstream_http_429"}, {}, "futures_futures")
        self.assertEqual(result["affected_paths"], 2)
        self.assertTrue(all(p["long_exchange"] == "Bybit" for p in result["sources"][0]["examples"]))
        self.assertIn("限流", result["sources"][0]["reason"])

    def test_auxiliary_field_is_named_instead_of_calling_it_a_quote_failure(self):
        result = snapshot_delays([], {"okx_open_interest": "timeout"}, {}, "futures_futures")
        self.assertEqual(result["sources"][0]["field"], "持仓量")

    def test_no_errors_means_no_delayed_paths(self):
        self.assertEqual(snapshot_delays([], {}, {}, "spot_futures"), {"sources": [], "affected_paths": 0, "affected_symbols": 0})


if __name__ == "__main__":
    unittest.main()

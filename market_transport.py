"""Bounded, reusable HTTPS GETs for public market data only.

No cookies, credentials, environment proxies, redirects or trading endpoints.
The total network deadline includes pool wait, TLS, headers and body reads.
"""
from collections import OrderedDict
import gzip
import http.client
import io
import json
import math
import queue
import ssl
import threading
import time
from urllib.parse import parse_qs, urlsplit


PUBLIC_PATHS = {
    "api.binance.com": {"/api/v3/ticker/bookTicker", "/api/v3/ticker/24hr"},
    "fapi.binance.com": {"/fapi/v1/exchangeInfo", "/fapi/v1/ticker/bookTicker", "/fapi/v1/ticker/24hr",
                         "/fapi/v1/premiumIndex", "/fapi/v1/fundingInfo", "/fapi/v1/klines",
                         "/futures/data/openInterestHist", "/futures/data/globalLongShortAccountRatio"},
    "api.gateio.ws": {"/api/v4/spot/tickers"},
    "api.bitget.com": {"/api/v2/spot/market/tickers"},
    "api.bybit.com": {"/v5/market/tickers", "/v5/market/instruments-info"},
    "www.okx.com": {"/api/v5/public/instruments", "/api/v5/market/tickers", "/api/v5/public/mark-price",
                    "/api/v5/market/index-tickers", "/api/v5/public/open-interest"},
}
MAX_BODY = 16 * 1024 * 1024


def decode_json(body, encoding=""):
    if len(body) > MAX_BODY:
        raise ValueError("public_payload_too_large")
    if encoding.lower() == "gzip":
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            body = stream.read(MAX_BODY + 1)
    elif encoding not in ("", "identity"):
        raise ValueError("unsupported_content_encoding")
    if len(body) > MAX_BODY:
        raise ValueError("public_payload_too_large")
    return json.loads(body.decode("utf-8")), len(body)


def check_api_success(payload):
    if not isinstance(payload, dict):
        return
    code = payload.get("retCode", payload.get("code"))
    if code is not None and (isinstance(code, bool) or code not in (0, "0", "00000")):
        # Never reflect arbitrary upstream messages into logs or the UI.
        label = str(code) if str(code).lstrip("-").isdigit() else "unknown"
        raise ValueError("upstream_api_error_" + label[:16])
    if payload.get("label") and payload.get("message"):
        raise ValueError("upstream_api_error")


class PublicMarketHttp:
    def __init__(self, connections_per_host=6):
        self.context = ssl.create_default_context()
        self.pools = {}
        for host in PUBLIC_PATHS:
            pool = queue.LifoQueue(connections_per_host)
            for _ in range(connections_per_host):
                pool.put(None)
            self.pools[host] = pool
        self.lock = threading.Lock()
        self.metrics = OrderedDict()
        self.cooldowns = {}

    def status(self):
        with self.lock:
            return {url: dict(value) for url, value in self.metrics.items()}

    def close(self):
        for pool in self.pools.values():
            idle = []
            while True:
                try:
                    idle.append(pool.get_nowait())
                except queue.Empty:
                    break
            for connection in idle:
                if connection:
                    connection.close()
                pool.put(None)

    def _record(self, url, started, wall, error=None, **extra):
        with self.lock:
            row = self.metrics.pop(url, {})
            row.update(attempts=row.get("attempts", 0) + 1, last_attempt_at=wall,
                       elapsed_seconds=round(time.monotonic()-started, 3), **extra)
            if error:
                row.update(failures=row.get("failures", 0)+1,
                           consecutive_failures=row.get("consecutive_failures", 0)+1,
                           last_error=error, last_failure_at=time.time())
            else:
                row.update(consecutive_failures=0, last_error=None, last_success_at=wall)
            self.metrics[url] = row
            while len(self.metrics) > 256:
                self.metrics.popitem(last=False)

    def get_json(self, url, timeout=1.5, weight_limit=None):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in PUBLIC_PATHS
                or parsed.path not in PUBLIC_PATHS[parsed.hostname] or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment
                or any(ord(char) < 32 for char in url)
                or any(key.lower() in {"signature", "apikey", "api_key", "secret", "token"}
                       for key in parse_qs(parsed.query, keep_blank_values=True))):
            raise ValueError("not_a_public_market_endpoint")
        if not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("invalid_market_timeout")
        started, wall = time.monotonic(), time.time()
        deadline = started + timeout
        host, pool = parsed.hostname, self.pools[parsed.hostname]
        connection, borrowed = None, False

        def remaining():
            left = deadline-time.monotonic()
            if left <= 0:
                raise TimeoutError("market_deadline_exceeded")
            return left

        try:
            with self.lock:
                if self.cooldowns.get(host, 0) > started:
                    raise RuntimeError("upstream_rate_limit_backoff")
            try:
                connection = pool.get(timeout=remaining())
                borrowed = True
            except queue.Empty:
                raise TimeoutError("market_connection_wait_timeout") from None
            path = parsed.path + ("?"+parsed.query if parsed.query else "")
            for attempt in range(2):
                try:
                    if connection is None:
                        connection = http.client.HTTPSConnection(host, timeout=remaining(), context=self.context)
                    connection.timeout = remaining()
                    if connection.sock:
                        connection.sock.settimeout(remaining())
                    connection.request("GET", path, headers={"Accept": "application/json", "Accept-Encoding": "gzip",
                                                            "User-Agent": "ArbiScope/1.0"})
                    if connection.sock:
                        connection.sock.settimeout(remaining())
                    response = connection.getresponse()
                    break
                except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                    connection.close()
                    connection = None
                    if attempt:
                        raise
                    remaining()  # one idempotent retry for an expired keep-alive socket
            if response.status in (418, 429):
                retry = response.getheader("Retry-After", "60")
                try:
                    retry = float(retry)
                except (ValueError, TypeError):
                    retry = 60
                retry = max(60, min(3600, retry)) if math.isfinite(retry) else 60
                with self.lock:
                    self.cooldowns[host] = time.monotonic()+retry
            if response.status != 200:
                raise RuntimeError("upstream_http_"+str(response.status))
            if weight_limit is not None:
                used = response.getheader("X-MBX-USED-WEIGHT-1M", "0")
                try:
                    at_budget = float(used) >= weight_limit
                except (ValueError, TypeError):
                    at_budget = False
                if at_budget:
                    with self.lock:
                        self.cooldowns[host] = time.monotonic()+60
            chunks, length = [], 0
            while True:
                if connection.sock:
                    connection.sock.settimeout(remaining())
                else:
                    remaining()
                chunk = response.read1(65536)
                if not chunk:
                    break
                length += len(chunk)
                if length > MAX_BODY:
                    raise ValueError("public_payload_too_large")
                chunks.append(chunk)
            remaining()
            encoding = response.getheader("Content-Encoding", "").lower()
            payload, decoded_length = decode_json(b"".join(chunks), encoding)
            if isinstance(payload, dict) and payload.get("retCode") in (10006, 10018, 10429):
                with self.lock:
                    self.cooldowns[host] = time.monotonic()+60
            check_api_success(payload)
            remaining()
            self._record(url, started, wall, wire_bytes=length, decoded_bytes=decoded_length, compressed=encoding == "gzip")
            return payload
        except Exception as exc:
            if connection:
                connection.close()
                connection = None
            self._record(url, started, wall, error=type(exc).__name__+": "+str(exc)[:120])
            raise
        finally:
            if borrowed:
                pool.put(connection)

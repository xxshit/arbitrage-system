"""Bounded, public Binance 30-day price references, never account/order data.

Cache a six-hour strip of historical five-minute candle opens. Each reference
is selected at/before now minus 30 days, not a calendar-month candle or a
shorter listing history. This avoids retaining 30 days of full-market ticks.
"""
import math
import re
import threading
from collections import OrderedDict
from urllib.parse import urlencode

BUCKET = 300
WINDOW = 30 * 86400
COVERAGE = 6 * 3600
MAX_POINTS = 99
MAX_SYMBOLS = 2000


def valid_reference_symbol(raw_symbol):
    # exchangeInfo is the identity allowlist in long_trend_targets(). Binance
    # also lists Chinese names; do not rewrite them or reject non-ASCII letters.
    # urlencode below keeps the exact name in a single public query parameter.
    return isinstance(raw_symbol, str) and 0 < len(raw_symbol) <= 64 and raw_symbol.isalnum()


def reference_failure_reason(error):
    """Log known local codes, never arbitrary upstream/DB exception contents."""
    code = str(error)
    safe_codes = {
        "invalid_reference_symbol", "invalid_reference_multiplier", "invalid_reference_response",
        "invalid_reference_candle", "unexpected_reference_timestamp", "invalid_reference_price",
        "duplicate_reference_timestamp", "invalid_reference_cache", "invalid_reference_bounds",
        "invalid_reference_source", "invalid_reference_points", "invalid_reference_timestamp",
        "public_payload_too_large", "unsupported_content_encoding", "upstream_api_error",
        "not_a_public_market_endpoint", "invalid_market_timeout", "upstream_rate_limit_backoff",
        "market_deadline_exceeded", "market_connection_wait_timeout",
    }
    if code in safe_codes or re.fullmatch(r"upstream_(?:http_[0-9]{3}|api_error_-?[0-9]{1,16})", code):
        return code
    return type(error).__name__


def reference_bounds(now):
    target = int(now) // BUCKET * BUCKET - WINDOW
    return target - 2 * BUCKET, target + COVERAGE


def fetch_reference(get_json, raw_symbol, multiplier, now):
    start, end = reference_bounds(now)
    if not valid_reference_symbol(raw_symbol):
        raise ValueError("invalid_reference_symbol")
    multiplier = float(multiplier)
    if not math.isfinite(multiplier) or multiplier <= 0:
        raise ValueError("invalid_reference_multiplier")
    payload = get_json("https://fapi.binance.com/fapi/v1/klines?" + urlencode({
        "symbol": raw_symbol, "interval": "5m", "startTime": start * 1000,
        "endTime": end * 1000 - 1, "limit": MAX_POINTS,
    }), timeout=4, weight_limit=1800)
    if not isinstance(payload, list) or len(payload) > MAX_POINTS:
        raise ValueError("invalid_reference_response")
    points = {}
    for row in payload:
        if not isinstance(row, list) or len(row) < 7:
            raise ValueError("invalid_reference_candle")
        at = int(row[0]) // 1000
        price = float(row[1]) / multiplier  # open at this bucket, not a later close
        if int(row[0]) != at * 1000 or at % BUCKET or not start <= at < end:
            raise ValueError("unexpected_reference_timestamp")
        if not math.isfinite(price) or price <= 0:
            raise ValueError("invalid_reference_price")
        if at in points:
            raise ValueError("duplicate_reference_timestamp")
        points[at] = price
    return {"source_symbol": raw_symbol, "multiplier": multiplier,
            "start": start, "end": end, "checked_at": int(now),
            "points": sorted(points.items())}


class TrendReferenceCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.retries = OrderedDict()

    def put(self, symbol, data):
        """Validate persisted data too; corrupt cache cannot become a price."""
        if not isinstance(data, dict):
            raise ValueError("invalid_reference_cache")
        start, end = int(data["start"]), int(data["end"])
        if start % BUCKET or end % BUCKET or end - start != COVERAGE + 2 * BUCKET:
            raise ValueError("invalid_reference_bounds")
        raw, multiplier = data["source_symbol"], float(data["multiplier"])
        if not valid_reference_symbol(raw) or not math.isfinite(multiplier) or multiplier <= 0:
            raise ValueError("invalid_reference_source")
        rows = data["points"]
        if not isinstance(rows, list) or len(rows) > MAX_POINTS:
            raise ValueError("invalid_reference_points")
        points = {}
        for at, price in rows:
            price = float(price)
            if isinstance(at, bool) or not isinstance(at, int) or at % BUCKET or not start <= at < end:
                raise ValueError("invalid_reference_timestamp")
            if not math.isfinite(price) or price <= 0 or at in points:
                raise ValueError("invalid_reference_price")
            points[at] = price
        with self.lock:
            self.entries.pop(symbol, None)
            self.entries[symbol] = {**data, "points": points}
            self.retries.pop(symbol, None)
            while len(self.entries) > MAX_SYMBOLS:
                self.entries.popitem(last=False)

    def defer(self, symbol, until):
        with self.lock:
            self.retries.pop(symbol, None)
            self.retries[symbol] = until
            while len(self.retries) > MAX_SYMBOLS:
                self.retries.popitem(last=False)

    def due(self, symbol, raw_symbol, multiplier, now):
        with self.lock:
            if self.retries.get(symbol, 0) > now:
                return False
            entry = self.entries.get(symbol)
            return (entry is None or entry["source_symbol"] != raw_symbol
                    or entry["multiplier"] != multiplier
                    or int(now) - WINDOW + 3600 >= entry["end"])

    def price(self, symbol, now):
        target = int(now) // BUCKET * BUCKET - WINDOW
        with self.lock:
            entry = self.entries.get(symbol)
            if not entry or not entry["start"] <= target < entry["end"]:
                return None
            # Up to ten minutes of backward-only tolerance, as for short trends.
            for offset in range(3):
                price = entry["points"].get(target - offset * BUCKET)
                if price is not None:
                    return price
        return None

    def change(self, symbol, current, now, raw_price=False):
        previous = self.price(symbol, now)
        if previous is None or current is None:
            return None
        current = float(current)
        if raw_price:
            with self.lock:
                entry = self.entries.get(symbol)
                if not entry:
                    return None
                current /= entry["multiplier"]
        return (current / previous - 1) * 100 if math.isfinite(current) and current > 0 else None

    def multiplier(self, symbol):
        with self.lock:
            return self.entries.get(symbol, {}).get("multiplier", 1.0)

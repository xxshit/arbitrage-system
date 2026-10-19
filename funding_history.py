"""Public settled funding collection: bounded requests, retries and durable fairness.

No Flask/DB/account access. Rates are percentage points, never projected from a
current quote. Database commits, not these disposable checkpoints, own the facts.
"""
import json
import math
from collections import deque
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode

DAY_MS = 86_400_000


class FundingHistoryError(RuntimeError):
    pass


class RequestGate:
    """Smooth requests; fail fast during cooldown instead of blocking a worker."""
    def __init__(self, spacing, clock=time.monotonic, sleep=time.sleep):
        self.spacing, self.clock, self.sleep = spacing, clock, sleep
        self.next_at = self.blocked_until = 0.
        self.lock = threading.Lock()
        self.queue = deque()

    def wait(self):
        ticket = object()
        with self.lock:
            self.queue.append(ticket)
        try:
            while True:
                with self.lock:
                    now = self.clock()
                    if now < self.blocked_until:
                        raise FundingHistoryError("rate_backoff")
                    delay = self.next_at - now
                    if self.queue[0] is ticket and delay <= 0:
                        self.next_at = now + self.spacing
                        return
                self.sleep(max(.01, min(delay, .05)))
        finally:
            with self.lock:
                self.queue.remove(ticket)

    def pause(self, seconds):
        with self.lock:
            self.blocked_until = max(self.blocked_until, self.clock() + seconds)


class FundingHistoryClient:
    def __init__(self, get_json):
        self.get_json = get_json
        # Binance history shares 500/5min with fundingInfo. <=375 history calls
        # per 5min leaves headroom for the existing 15-second metadata cache.
        self.gates = {venue: RequestGate(spacing) for venue, spacing in
                      (("Binance", .8), ("Bybit", .125), ("OKX", .25))}

    def request(self, exchange, url):
        gate = self.gates[exchange]
        gate.wait()
        try:
            payload = self.get_json(url, timeout=5)
        except HTTPError as exc:
            if exc.code in (403, 418, 429):
                try:
                    delay = float(exc.headers.get("Retry-After", "60"))
                except (TypeError, ValueError, AttributeError):
                    delay = 60
                if not math.isfinite(delay):
                    delay = 60
                gate.pause(max(600 if exc.code == 403 else 60, min(delay, 86400)))
            raise FundingHistoryError("http_" + str(exc.code)) from None
        if isinstance(payload, dict):
            code = str(payload.get("retCode", payload.get("code", "0")))
            if code in {"10006", "10018", "10429", "50011", "-1003"}:
                gate.pause(60)
                raise FundingHistoryError("rate_limit")
            if code in {"10001", "51001", "-1121"}:
                return None  # only verified aliases can be tried by fetch()
            if code != "0":
                raise FundingHistoryError("upstream_rejected")
        return payload

    @staticmethod
    def fact(stamp, rate):
        if stamp in (None, "") or rate in (None, "") or isinstance(rate, bool):
            raise FundingHistoryError("incomplete_settlement")
        stamp, rate = int(stamp), float(rate) * 100
        if stamp <= 0 or not math.isfinite(rate):
            raise FundingHistoryError("invalid_settlement")
        return stamp, rate

    def binance_recent(self, end):
        """Overlapping all-symbol tail; never a global time watermark.

        A full page (including tied timestamps) cannot prove full coverage.
        Per-contract due checks below independently repair anything not present.
        """
        payload = self.request("Binance", "https://fapi.binance.com/fapi/v1/fundingRate?limit=1000")
        if not isinstance(payload, list):
            raise FundingHistoryError("invalid_history_payload")
        rows = []
        for item in payload:
            stamp, rate = self.fact(item.get("fundingTime"), item.get("fundingRate"))
            if end - 31 * DAY_MS <= stamp <= end and isinstance(item.get("symbol"), str):
                rows.append((item["symbol"], stamp, rate))
        return rows

    def fetch(self, exchange, candidates, start, end):
        for raw in candidates:
            rows = self._fetch_raw(exchange, raw, start, end)
            if rows:
                return raw, rows
        return None, []

    def _fetch_raw(self, exchange, raw, start, end):
        output, cursor = {}, None
        limit, pages = (100, 10) if exchange == "OKX" else (200, 5)
        for _ in range(pages):
            if exchange == "Binance":
                params = {"symbol": raw, "limit": limit}
                if cursor is not None:
                    params["endTime"] = cursor - 1
                url = "https://fapi.binance.com/fapi/v1/fundingRate?"
            elif exchange == "Bybit":
                params = {"category": "linear", "symbol": raw, "limit": limit,
                          "endTime": end if cursor is None else cursor - 1}
                url = "https://api.bybit.com/v5/market/funding/history?"
            elif exchange == "OKX":
                if not raw.endswith("USDT"):
                    raise FundingHistoryError("unsupported_contract")
                params = {"instId": raw[:-4] + "-USDT-SWAP", "limit": limit}
                if cursor is not None:
                    params["after"] = cursor
                url = "https://www.okx.com/api/v5/public/funding-rate-history?"
            else:
                raise FundingHistoryError("unsupported_exchange")
            payload = self.request(exchange, url + urlencode(params))
            if payload is None:
                return []
            items = (payload if exchange == "Binance" else payload.get("result", {}).get("list")
                     if exchange == "Bybit" else payload.get("data"))
            if not isinstance(items, list):
                raise FundingHistoryError("invalid_history_payload")
            if not items:
                return sorted(output.items())
            stamps = []
            for item in items:
                stamp, rate = self.fact(
                    item.get("fundingRateTimestamp" if exchange == "Bybit" else "fundingTime"),
                    item.get("realizedRate" if exchange == "OKX" else "fundingRate"))
                stamps.append(stamp)
                if start <= stamp <= end:
                    if stamp in output and output[stamp] != rate:
                        raise FundingHistoryError("conflicting_settlement")
                    output[stamp] = rate
            oldest = min(stamps)
            if oldest <= start or len(items) < limit:
                return sorted(output.items())
            if cursor is not None and oldest >= cursor:
                raise FundingHistoryError("pagination_stalled")
            cursor = oldest
        raise FundingHistoryError("history_page_limit")  # not a successful full audit


def expected_settlement(target, now):
    """Schedule hint only, not evidence that a fee exists. Handles interval changes."""
    interval = target.get("interval")
    if not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 0 < interval <= 24:
        return None
    period = interval * 3600
    anchor = target.get("next_at")
    if not isinstance(anchor, (int, float)) or not math.isfinite(anchor) or abs(anchor-now) > 86400:
        anchor = 0
    return (anchor + math.floor((now - 2 - anchor) / period) * period) * 1000


class FundingSyncSchedule:
    def __init__(self, path):
        self.path = Path(path)
        self.states = {}
        try:
            with self.path.open() as handle:
                data = json.loads(handle.read(2_000_001))
            if data.get("version") == 1 and isinstance(data.get("states"), dict):
                for key, row in list(data["states"].items())[:6000]:
                    if isinstance(key, str) and len(key) <= 80 and isinstance(row, dict):
                        clean = {field: value for field, value in row.items()
                                 if field in {"attempt", "success", "full", "retry", "failures", "missing", "expected"}
                                 and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0}
                        self.states[key] = clean
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    def plan(self, targets, latest, now, limit):
        self.states = {key: row for key, row in self.states.items() if key in targets}
        hot, full = [], []
        for key, target in targets.items():
            state = self.states.get(key, {})
            expected = expected_settlement(target, now)
            if state.get("retry", 0) > now and expected == state.get("expected"):
                continue
            missing = expected is not None and latest.get(key, 0) < expected
            # A newly discovered leg must not spend hours displaying a two-day
            # tail as its 30D history while waiting behind already covered coins.
            initial_repair = not state.get("full") and target.get("earliest", 0) > now*1000 - 30*DAY_MS
            if missing or initial_repair or now - state.get("success", 0) >= 900:
                hot.append((0 if missing else 1 if initial_repair else 2, state.get("attempt", 0), key, initial_repair))
            if now - state.get("full", 0) >= 86400:
                full.append((state.get("full", 0), state.get("attempt", 0), key))
        selected = [(key, initial_repair) for _, _, key, initial_repair in sorted(hot)[:limit]]
        # One reserved repair slot prevents old interior gaps from being starved
        # by a permanently unavailable/delisted contract's latest settlement.
        if full:
            key = min(full)[2]
            if key not in {item[0] for item in selected}:
                selected.append((key, True))
            else:
                # Latest facts take priority; deep repair gets its own later turn.
                other = next((item[2] for item in sorted(full) if item[2] not in {r[0] for r in selected}), None)
                if other:
                    selected.append((other, True))
                elif selected:
                    selected[-1] = (selected[-1][0], True)
        return selected

    def done(self, key, target, now, latest, full=False, error=None):
        state = self.states.setdefault(key, {})
        state["attempt"] = now
        state["expected"] = expected_settlement(target, now)
        if error:
            failures = min(8, state.get("failures", 0) + 1)
            state.update(failures=failures, retry=now + min(300, 15 * 2**failures))
            return
        state.update(success=now, failures=0)
        expected = expected_settlement(target, now)
        missing = expected is not None and (latest or 0) < expected
        misses = min(8, state.get("missing", 0) + 1) if missing else 0
        state.update(missing=misses, retry=now + min(300, 15 * 2**misses) if missing else 0)
        if full:
            state["full"] = now

    def save(self, status):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "states": self.states, "status": status}, separators=(",", ":")))
        temporary.replace(self.path)

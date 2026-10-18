"""Public, unauthenticated market streams for the alert centre only.

No keys, account endpoints, Lark messages or order methods exist here. REST is
used by the existing application for instrument discovery, never as a fake tick.
All channels below are full best-book snapshots, not incremental L2 books.
"""
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import random
import threading
import time

from anomaly_engine import PulseDetector, finite


ENDPOINTS = {
    "bn_spot": "wss://stream.binance.com:9443/ws",
    "bn_future": "wss://fstream.binance.com/ws",
    "bn_mark": "wss://fstream.binance.com/market/ws",
    "gate": "wss://api.gateio.ws/ws/v4/",
    "bitget": "wss://ws.bitget.com/v3/ws/public",
    "bybit": "wss://stream.bybit.com/v5/public/linear",
    "okx": "wss://ws.okx.com:8443/ws/v5/public",
}


def subscription(feed, symbols):
    if feed.startswith("bn_"):
        suffix = "@markPrice@1s" if feed == "bn_mark" else "@bookTicker"
        return {"method": "SUBSCRIBE", "params": [s.lower() + suffix for s in symbols], "id": 1}
    if feed == "gate":
        return {"time": int(time.time()), "channel": "spot.book_ticker", "event": "subscribe", "payload": list(symbols)}
    if feed == "bybit":
        return {"op": "subscribe", "args": ["orderbook.1." + s for s in symbols]}
    if feed == "bitget":
        return {"op": "subscribe", "args": [{"instType": "spot", "topic": "books1", "symbol": s} for s in symbols]}
    return {"op": "subscribe", "args": [{"channel": "bbo-tbt", "instId": s} for s in symbols]}


def parse_frame(feed, message, received):
    """Return whitelisted public fields only. Malformed/empty frames are rejected."""
    if message in ("pong", "ping"):
        return []
    data = json.loads(message) if isinstance(message, (str, bytes)) else message
    if not isinstance(data, dict):
        return []
    if data.get("event") == "error" or data.get("error") or data.get("success") is False or data.get("code") not in (None, 0, "0", "00000"):
        raise ValueError("public_subscription_rejected")
    output = []

    def book(symbol, bid, ask, bid_qty, ask_qty, stamp, seq=None, reset=False):
        output.append(dict(symbol=symbol, bid=float(bid), ask=float(ask), bid_qty=float(bid_qty),
                           ask_qty=float(ask_qty), at=float(stamp) / 1000 if stamp is not None else received,
                           sequence=int(seq) if seq is not None else None, reset=reset,
                           timestamp_source="exchange" if stamp is not None else "receipt"))

    try:
        if feed.startswith("bn_"):
            data = data.get("data", data)
            if feed == "bn_mark" and data.get("e") == "markPriceUpdate":
                output.append(dict(symbol=data["s"], mark=float(data["p"]), index=float(data["i"]),
                                   funding=float(data["r"]) * 100, at=float(data["E"]) / 1000))
            elif "b" in data and "a" in data:
                book(data["s"], data["b"], data["a"], data["B"], data["A"], data.get("E"), data.get("u"))
        elif feed == "gate" and data.get("event") == "update" and data.get("channel") == "spot.book_ticker":
            item = data["result"]
            book(item["s"], item["b"], item["a"], item["B"], item["A"], item["t"], item.get("u"))
        elif feed == "bybit" and data.get("topic", "").startswith("orderbook.1.") and data.get("type") == "snapshot":
            item = data["data"]
            book(item["s"], item["b"][0][0], item["a"][0][0], item["b"][0][1], item["a"][0][1], data["ts"], item.get("u"), item.get("u") == 1)
        elif feed == "bitget" and data.get("arg", {}).get("topic") == "books1" and data.get("action") == "snapshot":
            for item in data.get("data", []):
                book(data["arg"]["symbol"], item["b"][0][0], item["a"][0][0], item["b"][0][1], item["a"][0][1], item["ts"], item.get("seq"))
        elif feed == "okx" and data.get("arg", {}).get("channel") == "bbo-tbt" and data.get("action") in (None, "snapshot"):
            for item in data.get("data", []):
                book(data["arg"]["instId"], item["bids"][0][0], item["asks"][0][0], item["bids"][0][1], item["asks"][0][1], item["ts"], item.get("seqId"))
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return []
    return output


@dataclass(frozen=True)
class Route:
    path: tuple  # strategy, canonical symbol, long venue, short venue
    long_key: tuple  # feed, raw exchange symbol
    short_key: tuple
    long_factor: float = 1
    short_factor: float = 1
    mark_key: tuple | None = None
    divergence_threshold: float = 10


class AlertStreamHub:
    MAX_AGE = 4.0
    MAX_SKEW = 2.0

    def __init__(self):
        self.lock = threading.RLock()
        self.routes, self.by_key, self.books, self.marks = {}, defaultdict(set), {}, {}
        self.detector = PulseDetector()
        self.pending, self.workers, self.fresh = {}, {}, {}
        self.running = False
        self.rejected = 0
        self.persistence_error = False
        self.last_persisted_at = None
        self.overloaded = False

    def _updates(self, updates):
        for update in updates:
            self.pending[update["id"]] = update

    def register(self, strategy, routes, now=None):
        now = time.time() if now is None else now
        with self.lock:
            incoming = {route.path: route for route in routes}
            for path in [p for p in self.routes if p[0] == strategy and incoming.get(p) != self.routes[p]]:
                self._updates(self.detector.reset(path, now, "isolated" if path not in incoming else "data_gap"))
                self.routes.pop(path)
                self.fresh.pop(path, None)
            self.routes.update(incoming)
            self.by_key = defaultdict(set)
            for route in self.routes.values():
                for key in (route.long_key, route.short_key, route.mark_key):
                    if key:
                        self.by_key[key].add(route.path)

    def invalidate(self, keys, now=None):
        now = time.time() if now is None else now
        with self.lock:
            affected = set()
            for key in keys:
                self.books.pop(key, None)
                self.marks.pop(key, None)
                affected.update(self.by_key.get(key, ()))
            for path in affected:
                self.fresh.pop(path, None)
                self._updates(self.detector.reset(path, now))

    def ingest(self, feed, item, received=None):
        now = time.time() if received is None else received
        key = (feed, item["symbol"])
        with self.lock:
            # Fail visibly instead of exhausting RAM during a prolonged DB
            # outage. Already captured pulses stay queued; resume with a gap.
            if len(self.pending) >= 20000:
                self.overloaded = True
                self.fresh.clear()
                return
            if self.overloaded:
                self.overloaded = False
                for path in list(self.routes):
                    self._updates(self.detector.reset(path, now))
            if key not in self.by_key:
                return
            if not finite(item.get("at")) or not -2 <= now - item["at"] <= self.MAX_AGE:
                self.rejected += 1
                return
            cache = self.marks if feed == "bn_mark" else self.books
            previous = cache.get(key)
            if previous and item["at"] < previous["at"]:
                self.rejected += 1
                return
            if feed == "bn_mark":
                if not all(finite(item.get(k)) and item[k] > 0 for k in ("mark", "index")):
                    self.rejected += 1
                    return
                if not finite(item.get("funding")):
                    item = {**item, "funding": None}
            else:
                if not all(finite(item.get(k)) and item[k] > 0 for k in ("bid", "ask", "bid_qty", "ask_qty")) or item["bid"] > item["ask"]:
                    self.rejected += 1
                    return
                if previous and item.get("sequence") is not None and previous.get("sequence") is not None and item["sequence"] < previous["sequence"]:
                    if not item.get("reset"):
                        self.rejected += 1
                        return
                    self.invalidate([key], now)
            cache[key] = {**item, "received": now}
            for path in self.by_key[key]:
                route = self.routes[path]
                long, short = self.books.get(route.long_key), self.books.get(route.short_key)
                if not long or not short or any(now - leg["at"] > self.MAX_AGE for leg in (long, short)) or abs(long["at"] - short["at"]) > self.MAX_SKEW:
                    continue
                la, lb = long["ask"] * route.long_factor, long["bid"] * route.long_factor
                sa, sb = short["ask"] * route.short_factor, short["bid"] * route.short_factor
                lm, sm = (la + lb) / 2, (sa + sb) / 2
                context = dict(open_spread=100 * (sb / la - 1), close_spread=100 * (sa / lb - 1),
                               mid_spread=100 * (sm / lm - 1), long_mid=lm, short_mid=sm,
                               basis=None, funding_rate=None, long_at=long["at"], short_at=short["at"],
                               long_timestamp_source=long.get("timestamp_source", "exchange"),
                               short_timestamp_source=short.get("timestamp_source", "exchange"))
                mark = self.marks.get(route.mark_key)
                if mark and now - mark["at"] <= self.MAX_AGE:
                    context.update(basis=100 * (mark["mark"] / mark["index"] - 1), funding_rate=mark.get("funding"))
                    # Close the REST-discovery race: isolate a freshly divergent
                    # spot path before its next five-second risk-table update.
                    if path[0] == "spot_futures" and context["open_spread"] * context["close_spread"] > 0 and abs(context["open_spread"] - context["close_spread"]) < .6 and min(abs(context["open_spread"]), abs(context["close_spread"])) >= route.divergence_threshold and abs(context["basis"]) <= 1 and abs(100 * (sm / (mark["index"] * route.short_factor) - 1)) <= 2:
                        self._updates(self.detector.reset(path, now, "isolated"))
                        self.fresh.pop(path, None)
                        continue
                self.fresh[path] = now
                # Public, aligned books are observations, NOT a bullish claim.
                for metric, value in (("spread", context["open_spread"]), ("basis", context["basis"]), ("price", 50 * (math.log(lm) + math.log(sm)))):
                    if finite(value):
                        self._updates(self.detector.observe(path, metric, value, now, context))

    def batch(self, limit=500):
        with self.lock:
            # Round-robin: a busy first batch must not starve later pulses when
            # its versions keep advancing while the database is committing.
            keys = list(self.pending)[:limit]
            batch = [self.pending.pop(key) for key in keys]
            self.pending.update((item["id"], item) for item in batch)
            return deepcopy(batch)

    def acknowledge(self, batch):
        with self.lock:
            for item in batch:
                if self.pending.get(item["id"], {}).get("version") == item["version"]:
                    self.pending.pop(item["id"], None)
            self.persistence_error = False
            self.last_persisted_at = time.time()

    def status(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            feeds = defaultdict(lambda: {"connections": 0, "connected": 0, "last_message_at": None, "errors": 0})
            for worker in self.workers.values():
                state = feeds[worker.feed]
                state["connections"] += 1
                state["connected"] += int(worker.connected and now - worker.last_message < 60)
                state["last_message_at"] = max(state["last_message_at"] or 0, worker.last_message)
                state["errors"] += worker.errors
            fresh = sum(now - value <= self.MAX_AGE for path, value in self.fresh.items() if path in self.routes)
            return dict(running=self.running, paths=len(self.routes), fresh_paths=fresh, feeds=dict(feeds),
                        rejected_frames=self.rejected, pending=len(self.pending), persistence_error=self.persistence_error,
                        overloaded=self.overloaded, last_persisted_at=self.last_persisted_at, checked_at=now)

    def reconcile(self):
        """Subscribe the entire discovered universe, not just moving candidates."""
        with self.lock:
            symbols = defaultdict(list)
            for feed, symbol in self.by_key:
                symbols[feed].append(symbol)
            desired = {}
            for feed, names in symbols.items():
                size = 40 if feed == "bitget" else 100
                names.sort()
                for offset in range(0, len(names), size):
                    desired[(feed, offset // size)] = tuple(names[offset:offset + size])
            stop = [self.workers.pop(key) for key in list(self.workers) if key not in desired or self.workers[key].symbols != desired[key]]
            start = []
            for key, names in desired.items():
                if key not in self.workers:
                    worker = PublicWorker(key[0], names, self)
                    self.workers[key] = worker
                    start.append(worker)
        for worker in stop:
            worker.stop.set()
        for worker in start:
            threading.Thread(target=worker.run, daemon=True, name="alert-ws-" + worker.feed).start()


class PublicWorker:
    def __init__(self, feed, symbols, hub):
        self.feed, self.symbols, self.hub = feed, symbols, hub
        self.stop = threading.Event()
        self.connected, self.last_message, self.errors = False, 0, 0

    def run(self):
        import websocket
        backoff = 1
        keys = [(self.feed, symbol) for symbol in self.symbols]
        while not self.stop.is_set():
            socket = None
            connected_at = time.monotonic()
            try:
                socket = websocket.create_connection(ENDPOINTS[self.feed], timeout=5, enable_multithread=True)
                socket.settimeout(2)
                socket.send(json.dumps(subscription(self.feed, self.symbols)))
                self.connected = True
                self.last_message = time.time()
                last_ping = time.monotonic()
                while not self.stop.is_set():
                    now = time.monotonic()
                    if now - connected_at >= 23 * 3600 or time.time() - self.last_message > 60:
                        break
                    if now - last_ping >= 20:
                        if self.feed in ("bitget", "okx"):
                            socket.send("ping")
                        elif self.feed == "bybit":
                            socket.send('{"op":"ping"}')
                        elif self.feed == "gate":
                            socket.send(json.dumps({"time": int(time.time()), "channel": "spot.ping"}))
                        else:
                            socket.ping()
                        last_ping = now
                    try:
                        message = socket.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not message:
                        break
                    self.last_message = time.time()
                    for item in parse_frame(self.feed, message, self.last_message):
                        self.hub.ingest(self.feed, item, self.last_message)
            except Exception:
                # Never log server payloads, headers, or exception URLs.
                self.errors += 1
            finally:
                self.connected = False
                if socket:
                    socket.close()
                self.hub.invalidate(keys)
            backoff = 1 if time.monotonic() - connected_at > 60 else min(60, backoff * 2)
            self.stop.wait(backoff + random.random())

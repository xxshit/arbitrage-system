"""Rate-bounded, unauthenticated BBO confirmation for quiet public streams.

Not a heartbeat/receipt-time renewal of a cached price. Every accepted item is
a newly fetched complete public order book. Original WS detection continues.
No credentials, account endpoints, trading, database or notification methods.
"""
from collections import Counter
import http.client
import json
import ssl
import threading
import time
from urllib.parse import urlencode, urlsplit

from anomaly_engine import finite
from market_transport import PublicMarketHttp


# Per-feed ceilings, not target traffic. No catch-up bursts after a slow call.
LIMITS = {'bn_spot': .5, 'bn_future': 3, 'gate': 6, 'bybit': .5}
BULK_URLS = {'bn_spot': 'https://api.binance.com/api/v3/ticker/bookTicker',
             'bybit': 'https://api.bybit.com/v5/market/tickers?category=linear'}
URLS = {'bn_spot': 'https://api.binance.com/api/v3/depth',
        'bn_future': 'https://fapi.binance.com/fapi/v1/depth',
        'gate': 'https://api.gateio.ws/api/v4/spot/order_book',
        'bybit': 'https://api.bybit.com/v5/market/orderbook'}
MAX_RTT = 1.5


class PublicHTTP:
    """Small keep-alive stdlib client: verified TLS, no netrc/proxy/cookie jar.

    The endpoint is fixed at construction. Only GET public depth is possible;
    http.client never follows redirects or injects environment credentials.
    """
    def __init__(self, feed):
        self.feed = feed
        self.url = urlsplit(URLS[feed])
        self.tls = ssl.create_default_context()
        self.connection = None

    def get(self, symbol):
        if self.connection is None:
            self.connection = http.client.HTTPSConnection(self.url.hostname, timeout=MAX_RTT, context=self.tls)
        self.connection.request('GET', self.url.path+'?'+urlencode(parameters(self.feed, symbol)),
                                headers={'Accept': 'application/json', 'User-Agent': 'ArbiScope-public-quote'})
        return self.connection.getresponse()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def parameters(feed, symbol):
    if feed == 'gate':
        return dict(currency_pair=symbol, limit=1, with_id='true')
    if feed == 'bybit':
        return dict(category='linear', symbol=symbol, limit=1)
    return dict(symbol=symbol, limit=5)


def parse_snapshot(feed, symbol, data, started, received, elapsed):
    """Reject stale/cached/slow/malformed books; never fill absent source times.

    Binance spot has no output timestamp. Its conservative observation time is
    request START (not a fabricated exchange time), bounded by a short RTT and
    accepted only while the independently timed WS connection remains healthy.
    Gate's `current` is snapshot generation; `update` remains last book change.
    """
    try:
        if feed not in URLS or not isinstance(data, dict):
            return None
        if not 0 <= elapsed <= MAX_RTT or not 0 <= received-started <= MAX_RTT+.1:
            return None
        if feed == 'bybit':
            if data.get('retCode') != 0 or data['result']['s'] != symbol:
                return None
            item = data['result']
            bids, asks, stamp, seq = item['b'], item['a'], item['ts']/1000, item['seq']
            changed_at = item.get('cts', item['ts'])/1000
        elif feed == 'gate':
            bids, asks, stamp, seq = data['bids'], data['asks'], data['current']/1000, data['id']
            changed_at = data['update']/1000
        else:
            bids, asks, seq = data['bids'], data['asks'], data['lastUpdateId']
            stamp = started if feed == 'bn_spot' else data['E']/1000
            changed_at = None if feed == 'bn_spot' else data['T']/1000
        result = dict(symbol=symbol, bid=float(bids[0][0]), ask=float(asks[0][0]),
                      bid_qty=float(bids[0][1]), ask_qty=float(asks[0][1]),
                      at=stamp, sequence=int(seq), book_source='rest_snapshot',
                      timestamp_source='request_start' if feed == 'bn_spot' else 'exchange',
                      last_change_at=changed_at)
        if isinstance(seq, bool) or result['sequence'] < 0:
            return None
        if feed == 'bybit':
            # REST orderbook seq and ticker cs are not assumed interchangeable.
            result['sequence_kind'] = 'bybit_orderbook'
        if not finite(stamp) or not -2 <= received-stamp <= 4:
            return None
        if any(not finite(result[name]) or result[name] <= 0 for name in ('bid','ask','bid_qty','ask_qty')) or result['bid'] > result['ask']:
            return None
        if changed_at is not None and (not finite(changed_at) or changed_at > stamp+2):
            return None
        return result
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return None


def parse_bulk_snapshot(feed, data, symbols, started, received, elapsed):
    """A new complete BBO read, never a renewal from another coin's heartbeat."""
    if elapsed < 0 or elapsed > MAX_RTT or not 0 <= received-started <= MAX_RTT+.1:
        return {}
    if feed == 'bn_spot':
        rows, stamp, source = data, started, 'request_start'
        fields = ('bidPrice', 'askPrice', 'bidQty', 'askQty')
    elif feed == 'bybit' and isinstance(data, dict) and data.get('retCode') == 0:
        result = data.get('result') or {}
        if result.get('category') != 'linear':
            return {}
        rows, source = result.get('list'), 'exchange_snapshot'
        try:
            stamp = float(data['time'])/1000
        except (KeyError, ValueError, TypeError):
            return {}
        fields = ('bid1Price', 'ask1Price', 'bid1Size', 'ask1Size')
    else:
        return {}
    if not isinstance(rows, list) or not finite(stamp) or not -2 <= received-stamp <= 4:
        return {}
    output, seen = {}, set()
    for row in rows:
        if not isinstance(row, dict) or row.get('symbol') not in symbols:
            continue
        symbol = row['symbol']
        if symbol in seen:
            output.pop(symbol, None)
            continue
        seen.add(symbol)
        try:
            bid, ask, bq, aq = (float(row[key]) for key in fields)
        except (KeyError, ValueError, TypeError):
            continue
        if not all(finite(value) and value > 0 for value in (bid, ask, bq, aq)) or bid > ask:
            continue
        output[symbol] = dict(symbol=symbol, bid=bid, ask=ask, bid_qty=bq, ask_qty=aq, at=stamp,
                              sequence=None, sequence_kind=feed+'_rest_bulk', timestamp_source=source,
                              last_change_at=None)
    return output


class PublicBookRefresh:
    def __init__(self, hub):
        self.hub = hub
        self.stop = threading.Event()
        self.attempted = {feed: {} for feed in LIMITS}
        self.counts = {feed: Counter() for feed in LIMITS}
        self.cooldown = {feed: 0 for feed in LIMITS}
        self.threads = []

    def start(self):
        if self.threads:
            return
        for feed in LIMITS:
            thread = threading.Thread(target=self.run, args=(feed,), daemon=True, name='quote-confirm-'+feed)
            self.threads.append(thread)
            thread.start()

    def choose(self, feed, now):
        with self.hub.lock:
            known = self.attempted[feed]
            active = {key for key in self.hub.by_key if key[0] == feed}
            for key in known.keys() - active:
                known.pop(key, None)
            choices = []
            for key in active:
                owner = self.hub.owners.get(key)
                book = self.hub.books.get(key)
                if not owner or not owner.connected or not -2 <= now-owner.last_source_at <= 4:
                    continue  # reconnect owns broken sources; do not create a REST storm
                if now-known.get(key, 0) < 2 or (book and now-book['at'] < 1.5):
                    continue
                choices.append((known.get(key, 0), key, owner, book))
            if not choices:
                return None
            _, key, owner, book = min(choices, key=lambda row: (row[0], row[1]))
            known[key] = now
            return key, owner, book

    def accept(self, selected, item, now):
        key, owner, previous = selected
        with self.hub.lock:
            # In-flight REST must never overwrite a newer WS observation, revive
            # a retired connection, re-add a removed path or heal a source outage.
            if not item or item.get('symbol') != key[1] or key not in self.hub.by_key or self.hub.owners.get(key) is not owner or not owner.connected or not -2 <= now-owner.last_source_at <= 4 or self.hub.books.get(key) is not previous:
                return False
            before = self.hub.books.get(key)
            self.hub.ingest(key[0], item, now, owner=owner)
            return self.hub.books.get(key) is not before

    def fetch(self, session, feed, symbol):
        started, began = time.time(), time.monotonic()
        with session.get(symbol) as response:
            if response.status in (418, 429):
                try:
                    delay = float(response.headers.get('Retry-After', 60))
                except (TypeError, ValueError):
                    delay = 60
                self.cooldown[feed] = time.monotonic()+max(60, min(delay, 3600))
            # Stop before the shared Binance IP budget becomes exhausted; leave
            # room for the existing discovery/funding workers, never rotate IPs.
            try:
                weight = int(response.headers.get('X-MBX-USED-WEIGHT-1M', 0))
            except (TypeError, ValueError):
                weight = 0
            if weight >= (3000 if feed == 'bn_spot' else 1600):
                self.cooldown[feed] = max(self.cooldown[feed], time.monotonic()+60)
            if response.status != 200:
                raise ValueError('public_snapshot_http_error')
            chunks, size = [], 0
            while True:
                chunk = response.read(4096)
                if not chunk:
                    break
                size += len(chunk)
                if size > 32768 or time.monotonic()-began > MAX_RTT:
                    raise ValueError('public_snapshot_response_bound')
                chunks.append(chunk)
            data = json.loads(b''.join(chunks))
            if feed == 'bybit' and data.get('retCode') in (10006, 10018, 10429):
                self.cooldown[feed] = time.monotonic()+60
            return parse_snapshot(feed, symbol, data, started, time.time(), time.monotonic()-began)

    def run(self, feed):
        if feed in BULK_URLS:
            return self.run_bulk(feed)
        session = PublicHTTP(feed)
        try:
            errors = 0
            while not self.stop.is_set():
                began = time.monotonic()
                if began < self.cooldown[feed]:
                    self.stop.wait(min(1, self.cooldown[feed]-began))
                    continue
                selected = self.choose(feed, time.time())
                if selected:
                    self.counts[feed]['attempts'] += 1
                    try:
                        item = self.fetch(session, feed, selected[0][1])
                        accepted = self.accept(selected, item, time.time())
                        self.counts[feed]['accepted' if accepted else 'rejected'] += 1
                        errors = 0
                    except Exception as error:
                        session.close()
                        self.counts[feed]['errors'] += 1
                        self.counts[feed]['last_error_type'] = type(error).__name__
                        errors = min(5, errors+1)
                        self.cooldown[feed] = max(self.cooldown[feed], time.monotonic()+min(30, 2**errors))
                self.stop.wait(max(.01, 1/LIMITS[feed]-(time.monotonic()-began)))
        finally:
            session.close()

    def choose_bulk(self, feed, now):
        # Select once under the same lock. A single public snapshot confirms all
        # quiet subscribed books instead of a many-second per-coin REST queue.
        with self.hub.lock:
            selected = []
            for key in self.hub.by_key:
                if key[0] != feed:
                    continue
                owner, book = self.hub.owners.get(key), self.hub.books.get(key)
                if owner and owner.connected and -2 <= now-owner.last_source_at <= 4 and (not book or now-book['at'] >= 1.5):
                    selected.append((key, owner, book))
            return selected

    def confirm_bulk(self, client, feed, selected):
        started, began = time.time(), time.monotonic()
        data = client.get_json(BULK_URLS[feed], timeout=MAX_RTT, weight_limit=3000 if feed == 'bn_spot' else None)
        received = time.time()
        items = parse_bulk_snapshot(feed, data, {key[1] for key, _, _ in selected}, started, received, time.monotonic()-began)
        for item in selected:
            accepted = self.accept(item, items.get(item[0][1]), time.time())
            self.counts[feed]['accepted' if accepted else 'rejected'] += 1

    def run_bulk(self, feed):
        client, errors = PublicMarketHttp(connections_per_host=1), 0
        try:
            while not self.stop.is_set():
                began = time.monotonic()
                if began < self.cooldown[feed]:
                    self.stop.wait(min(1, self.cooldown[feed]-began))
                    continue
                selected = self.choose_bulk(feed, time.time())
                if selected:
                    self.counts[feed]['attempts'] += 1
                    try:
                        self.confirm_bulk(client, feed, selected)
                        errors = 0
                    except Exception as error:
                        self.counts[feed]['errors'] += 1
                        self.counts[feed]['last_error_type'] = type(error).__name__
                        errors = min(5, errors+1)
                        self.cooldown[feed] = time.monotonic()+min(30, 2**errors)
                self.stop.wait(max(.01, 1/LIMITS[feed]-(time.monotonic()-began)))
        finally:
            client.close()

    def status(self):
        now = time.monotonic()
        return {feed: {**dict(self.counts[feed]), 'max_requests_per_second': LIMITS[feed], 'mode': 'bulk_bbo' if feed in BULK_URLS else 'single_depth',
                       'cooldown_seconds': round(max(0, self.cooldown[feed]-now), 1)} for feed in LIMITS}

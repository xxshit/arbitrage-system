"""Bounded research summaries and synchronized public BBO evidence.

Charts retain 5s/minute extrema. Book evidence is one actual paired snapshot per
5min, NOT full depth/ticks nor evidence that every historical extreme was filled.
Shares the existing 15-day shards/checkpoints and 1GiB free-space guard.
"""
from array import array
import math
import threading

from opportunity_pool import number, quote_metrics
from spread_history import decode_chunk, encode_chunk


class BookBuffer:
    def __init__(self, limit=24000):
        self.lock = threading.Lock()
        self.pending = {}
        self.last_bucket = {}
        self.limit = limit
        self.dropped = 0

    def observe(self, quotes, now):
        with self.lock:
            for path, live in quotes.items():
                strategy, symbol, exchange, short = path
                if strategy != "spot_futures" or short != "Binance":
                    continue
                quote = quote_metrics(live, now)
                if not quote:
                    continue
                at = quote["at"]
                bucket = int(at)//300*300
                key = (symbol, exchange, "book", bucket//3600*3600)
                if self.last_bucket.get(path, -1) >= bucket:
                    continue
                long, short_leg = quote["long"], quote["short"]
                quantities = [leg.get(k) for leg in (long, short_leg) for k in ("bid_qty", "ask_qty")]
                sample = [at, long["bid"], long["ask"], short_leg["bid"], short_leg["ask"],
                          *(q if number(q) and q > 0 else None for q in quantities),
                          live["long_at"], live["short_at"]]
                self.pending[(key, bucket)] = sample
                self.last_bucket[path] = bucket
            if len(self.last_bucket) > 4000:
                self.last_bucket = {p: b for p, b in self.last_bucket.items() if b >= now-3600}
            while len(self.pending) > self.limit:
                self.pending.pop(next(iter(self.pending)))
                self.dropped += 1

    def persist(self, store):
        with self.lock:
            pending = list(self.pending.items())
        grouped = {}
        for (key, bucket), sample in pending:
            grouped.setdefault(key, {})[str(bucket)] = sample
        rows = list(grouped.items())
        for offset in range(0, len(rows), 50):
            batch = rows[offset:offset+50]
            for day in sorted({store.day(key[-1]) for key, _ in batch}):
                same_day = [(key, samples) for key, samples in batch if store.day(key[-1]) == day]
                connection = store.connect(day, writable=True)
                try:
                    with connection:
                        for key, samples in same_day:
                            row = connection.execute("SELECT payload FROM history WHERE symbol=? AND exchange=? AND metric=? AND hour_at=?", key).fetchone()
                            existing = decode_chunk(row[0]) if row else {}
                            # First genuine observation per bucket survives restarts.
                            for bucket, sample in samples.items():
                                existing.setdefault(bucket, sample)
                            connection.execute("INSERT INTO history VALUES(?,?,?,?,?) ON CONFLICT(symbol,exchange,metric,hour_at) DO UPDATE SET payload=excluded.payload",
                                               (*key, encode_chunk(existing)))
                    with self.lock:
                        for key, samples in same_day:
                            for bucket in samples:
                                self.pending.pop((key, int(bucket)), None)
                finally:
                    connection.close()
        return len(pending)


def percentile(values, fraction):
    return values[math.ceil((len(values)-1)*fraction)] if values else None


class ProfileCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}
        self.at = 0

    def snapshot(self):
        with self.lock:
            return self.data, self.at

    def refresh(self, store, now):
        start = now-86400
        first_bucket = int(start)//300
        accumulators = {}
        for midnight in range(int(start)//86400*86400, int(now)//86400*86400+1, 86400):
            day = store.day(midnight)
            if not (store.directory/(day+".sqlite3")).exists():
                continue
            connection = store.connect(day)
            try:
                for symbol, exchange, metric, payload in connection.execute(
                    "SELECT symbol,exchange,metric,payload FROM history WHERE metric IN ('open','close') AND hour_at>=? AND hour_at<=? ORDER BY hour_at",
                    (int(start)//3600*3600, int(now)//3600*3600),
                ):
                    key = (symbol, exchange)
                    if key not in accumulators and len(accumulators) >= 4000:
                        continue
                    state = accumulators.setdefault(key, {})
                    acc = state.setdefault(metric, dict(values=array("d", [math.nan])*289,
                                                       low=math.inf, high=-math.inf,
                                                       first=now, last=0))
                    minutes = decode_chunk(payload)
                    for minute in sorted(minutes, key=int):
                        for segment in minutes[minute]:
                            for t, value in segment:
                                if not start < t <= now:
                                    continue
                                acc["values"][int(t)//300-first_bucket] = value
                                acc["low"] = min(acc["low"], value)
                                acc["high"] = max(acc["high"], value)
                                acc["first"] = min(acc["first"], t)
                                acc["last"] = max(acc["last"], t)
            finally:
                connection.close()
        output = {}
        for key, state in accumulators.items():
            opening, closing = state.get("open"), state.get("close")
            if not opening or not opening["last"]:
                continue
            values = sorted(v for v in opening["values"] if math.isfinite(v))
            closes = sorted(v for v in closing["values"] if math.isfinite(v)) if closing else []
            def ready(acc, data):
                return bool(acc and len(data) >= 12 and acc["last"]-acc["first"] >= 3600
                            and now-acc["last"] <= 300
                            and len(data) >= .5*((acc["last"]-acc["first"])/300))
            output[key] = dict(computed_at=now, ready=ready(opening, values),
                               close_ready=ready(closing, closes),
                               first_at=opening["first"], last_at=opening["last"],
                               bins=len(values), median=percentile(values, .5),
                               low=opening["low"], high=opening["high"],
                               range=opening["high"]-opening["low"],
                               max_abs=max(abs(opening["low"]), abs(opening["high"])),
                               exit_q75=percentile(closes, .75))
        with self.lock:
            self.data, self.at = output, now
        return len(output)

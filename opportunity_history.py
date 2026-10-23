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

WINDOWS = {"24h": 86400, "3d": 3*86400, "7d": 7*86400, "15d": 15*86400}


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
        # 24 hourly ranges × 7 doubles per path/day, not 15 days of ticks.
        # At most 24,000 path-days (~32MiB numeric storage); immutable days
        # are reused. HTTP readers only see the final small aggregates.
        self.days = {}

    def snapshot(self):
        with self.lock:
            return self.data, self.at

    def refresh(self, store, now):
        windows = self.window_ranges(store, now)
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
                    # The WITHOUT ROWID primary key already orders hours WITHIN
                    # each path/metric. Global hour ordering sorts every BLOB in
                    # a temporary B-tree and is unnecessary for these accumulators.
                    "SELECT symbol,exchange,metric,payload FROM history WHERE metric IN ('open','close') AND hour_at>=? AND hour_at<=? ORDER BY symbol,exchange,metric,hour_at",
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
        for key, ranges in windows.items():
            output.setdefault(key, dict(computed_at=now, ready=False, close_ready=False))
            output[key]["windows"] = ranges
        with self.lock:
            self.data, self.at = output, now
        return len(output)

    def window_ranges(self, store, now):
        cutoff = now-15*86400
        self.days = {day: item for day, item in self.days.items() if day >= store.day(cutoff)}
        output = {}
        for midnight in range(int(cutoff)//86400*86400, int(now)//86400*86400+1, 86400):
            day = store.day(midnight)
            path = store.directory/(day+".sqlite3")
            if not path.exists():
                continue
            wal = path.with_name(path.name+"-wal")
            signature = (path.stat().st_mtime_ns, path.stat().st_size,
                         wal.stat().st_mtime_ns if wal.exists() else 0,
                         wal.stat().st_size if wal.exists() else 0)
            cached = self.days.get(day)
            connection = None
            try:
                if cached and cached[0] == signature:
                    ranges = cached[1]
                else:
                    connection = store.connect(day)
                    ranges = {}
                    for symbol, exchange, hour, payload in connection.execute(
                            # Each hour writes a distinct numeric-array slot;
                            # no ordering (and no sorting of BLOBs) is needed.
                            "SELECT symbol,exchange,hour_at,payload FROM history WHERE metric='open'"):
                        key = symbol, exchange
                        if key not in ranges and len(ranges) >= 4000:
                            continue
                        data = ranges.setdefault(key, array("d", [math.nan])*168)
                        offset = (int(hour)//3600 % 24)*7
                        values = [p for segments in decode_chunk(payload).values() for segment in segments for p in segment
                                  if len(p) == 2 and number(p[0]) and number(p[1])]
                        if not values:
                            continue
                        low, high = min(values, key=lambda p:p[1]), max(values, key=lambda p:p[1])
                        data[offset:offset+7] = array("d", [low[1], high[1], low[0], high[0],
                            min(p[0] for p in values), max(p[0] for p in values),
                            len({int(p[0])//60 for p in values})])
                    self.days[day] = signature, ranges
                    while sum(len(entry[1]) for entry in self.days.values()) > 24000:
                        self.days.pop(min(self.days))
                for name, seconds in WINDOWS.items():
                    start = now-seconds
                    if midnight+86400 <= start:
                        continue
                    boundary = int(start)//3600*3600
                    # Only the boundary hour needs raw timestamps; whole-hour
                    # extrema cannot answer an exact rolling cutoff.
                    boundary_values = {}
                    if midnight <= boundary < midnight+86400:
                        if connection is None:
                            connection = store.connect(day)
                        for symbol, exchange, payload in connection.execute(
                                "SELECT symbol,exchange,payload FROM history WHERE metric='open' AND hour_at=?", (boundary,)):
                            points = [p for segs in decode_chunk(payload).values() for seg in segs for p in seg
                                      if len(p) == 2 and number(p[0]) and number(p[1]) and start < p[0] <= now]
                            if points:
                                low, high = min(points, key=lambda p:p[1]), max(points, key=lambda p:p[1])
                                boundary_values[(symbol, exchange)] = [low[1],high[1],low[0],high[0],
                                    min(p[0] for p in points),max(p[0] for p in points),
                                    len({int(p[0])//60 for p in points})]
                    for key, data in ranges.items():
                        if key not in output and len(output) >= 4000:
                            continue
                        for hour in range(24):
                            hour_at = midnight+hour*3600
                            if hour_at+3600 <= start or hour_at > now:
                                continue
                            values = boundary_values.get(key) if hour_at == boundary else data[hour*7:hour*7+7]
                            if values is None or not math.isfinite(values[0]) or values[4] > now:
                                continue
                            low, high, low_at, high_at, first, last, bins = values
                            acc = output.setdefault(key, {}).setdefault(name, dict(
                                low=low,high=high,low_at=low_at,high_at=high_at,first_at=first,last_at=last,bins=0))
                            if low < acc["low"]:
                                acc.update(low=low,low_at=low_at)
                            if high > acc["high"]:
                                acc.update(high=high,high_at=high_at)
                            acc.update(first_at=min(first,acc["first_at"]),last_at=max(last,acc["last_at"]),
                                       bins=acc["bins"]+int(bins))
            finally:
                if connection is not None:
                    connection.close()
        for ranges in output.values():
            for name, acc in ranges.items():
                acc.update(range=acc["high"]-acc["low"],max_abs=max(abs(acc["low"]),abs(acc["high"])),
                           coverage=min(1,acc["bins"]/(WINDOWS[name]/60)),
                           peak_at=acc["high_at"] if abs(acc["high"]) >= abs(acc["low"]) else acc["low_at"])
        return output

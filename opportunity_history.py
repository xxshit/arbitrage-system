"""Bounded research summaries and synchronized public BBO evidence.

Charts retain 5s/minute extrema. Book evidence is one actual paired snapshot per
5min, NOT full depth/ticks nor evidence that every historical extreme was filled.
Shares the existing 15-day shards/checkpoints and 1GiB free-space guard.
"""
from array import array
from contextlib import ExitStack, closing
from heapq import merge
from itertools import groupby
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


def _profile_rows(connection, midnight, start, now):
    for symbol, exchange, metric, payload in connection.execute(
        "SELECT symbol,exchange,metric,payload FROM history WHERE metric IN ('open','close') "
        "AND hour_at>=? AND hour_at<=? ORDER BY symbol,exchange,metric,hour_at", (start, now)
    ):
        yield symbol, exchange, midnight, metric, payload


def _consume_profile_hour(acc, payload, start, now, first_bucket):
    # The decoded graph dies on return, before decoding the next hour. Keeping
    # it in the outer loop would overlap the old graph with the next decode.
    minutes = decode_chunk(payload)
    for minute in sorted(minutes, key=int):
        for segment in minutes[minute]:
            for t, value in segment:
                if start < t <= now:
                    acc["values"][int(t)//300-first_bucket] = value
                    acc["low"] = min(acc["low"], value)
                    acc["high"] = max(acc["high"], value)
                    acc["first"] = min(acc["first"], t)
                    acc["last"] = max(acc["last"], t)


def _profile(rows, now):
    """Finish one path, then release its two numeric arrays before the next."""
    start = now-86400
    first_bucket = int(start)//300
    state = {}
    for _, _, _, metric, payload in rows:
        acc = state.get(metric)
        if acc is None:
            acc = state[metric] = dict(values=array("d", [math.nan])*289,
                                      low=math.inf, high=-math.inf, first=now, last=0)
        _consume_profile_hour(acc, payload, start, now, first_bucket)
    opening, closing = state.get("open"), state.get("close")
    if not opening or not opening["last"]:
        return None
    values = sorted(v for v in opening["values"] if math.isfinite(v))
    closes = sorted(v for v in closing["values"] if math.isfinite(v)) if closing else []
    def ready(acc, data):
        return bool(acc and len(data) >= 12 and acc["last"]-acc["first"] >= 3600
                    and now-acc["last"] <= 300
                    and len(data) >= .5*((acc["last"]-acc["first"])/300))
    return dict(computed_at=now, ready=ready(opening, values), close_ready=ready(closing, closes),
                first_at=opening["first"], last_at=opening["last"], bins=len(values),
                median=percentile(values, .5), low=opening["low"], high=opening["high"],
                range=opening["high"]-opening["low"],
                max_abs=max(abs(opening["low"]), abs(opening["high"])),
                exit_q75=percentile(closes, .75))


def _hour_range(payload, start=-math.inf, end=math.inf):
    low = high = low_at = high_at = first = last = None
    minutes = set()
    for segments in decode_chunk(payload).values():
        for segment in segments:
            for point in segment:
                if len(point) != 2 or not number(point[0]) or not number(point[1]):
                    continue
                at, value = point
                if not start < at <= end:
                    continue
                if low is None or value < low:
                    low, low_at = value, at
                if high is None or value > high:
                    high, high_at = value, at
                first = at if first is None else min(first, at)
                last = at if last is None else max(last, at)
                minutes.add(int(at)//60)
    return (low, high, low_at, high_at, first, last, len(minutes)) if low is not None else None


def _day_ranges(connection):
    ranges = {}
    for symbol, exchange, hour, payload in connection.execute(
            "SELECT symbol,exchange,hour_at,payload FROM history WHERE metric='open'"):
        key = symbol, exchange
        if key not in ranges and len(ranges) >= 4000:
            continue
        data = ranges.get(key)
        if data is None:
            data = ranges[key] = array("d", [math.nan])*168
        values = _hour_range(payload)
        if values is not None:
            offset = (int(hour)//3600 % 24)*7
            for index, value in enumerate(values):
                data[offset+index] = value
    return ranges


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
        output = {}
        with ExitStack() as connections:
            streams = []
            bounds = int(start)//3600*3600, int(now)//3600*3600
            for midnight in range(int(start)//86400*86400, int(now)//86400*86400+1, 86400):
                day = store.day(midnight)
                if not (store.directory/(day+".sqlite3")).exists():
                    continue
                connection = connections.enter_context(closing(store.connect(day)))
                connection.execute("BEGIN")
                # Preserve the original day-first 4000-path admission and output
                # order. Both this key query and the row stream use one snapshot.
                if len(output) < 4000:
                    for key in connection.execute(
                        "SELECT DISTINCT symbol,exchange FROM history WHERE metric IN ('open','close') "
                        "AND hour_at>=? AND hour_at<=? ORDER BY symbol,exchange LIMIT 4000", bounds
                    ):
                        if key not in output:
                            output[key] = None
                            if len(output) == 4000:
                                break
                streams.append(_profile_rows(connection, midnight, *bounds))
            # At most two daily cursors and one path's accumulators are live.
            rows = merge(*streams, key=lambda row: row[:4])
            for key, samples in groupby(rows, key=lambda row: row[:2]):
                if key in output:
                    output[key] = _profile(samples, now)
        for key in [key for key, value in output.items() if value is None]:
            del output[key]
        for key, ranges in windows.items():
            profile = output.get(key)
            if profile is None:
                profile = output[key] = dict(computed_at=now, ready=False, close_ready=False)
            profile["windows"] = ranges
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
                    ranges = _day_ranges(connection)
                    self.days[day] = signature, ranges
                    cached = None  # No stale day arrays survive the replacement.
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
                            values = _hour_range(payload, start, now)
                            if values is not None:
                                boundary_values[(symbol, exchange)] = values
                    for key, data in ranges.items():
                        if key not in output and len(output) >= 4000:
                            continue
                        for hour in range(24):
                            hour_at = midnight+hour*3600
                            if hour_at+3600 <= start or hour_at > now:
                                continue
                            if hour_at == boundary:
                                values = boundary_values.get(key)
                                if values is None:
                                    continue
                                low, high, low_at, high_at, first, last, bins = values
                            else:
                                offset = hour*7
                                low = data[offset]
                                first = data[offset+4]
                                if not math.isfinite(low) or first > now:
                                    continue
                                high, low_at, high_at = data[offset+1], data[offset+2], data[offset+3]
                                last, bins = data[offset+5], data[offset+6]
                            path_ranges = output.get(key)
                            if path_ranges is None:
                                path_ranges = output[key] = {}
                            acc = path_ranges.get(name)
                            if acc is None:
                                acc = path_ranges[name] = dict(
                                    low=low,high=high,low_at=low_at,high_at=high_at,first_at=first,last_at=last,bins=0)
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

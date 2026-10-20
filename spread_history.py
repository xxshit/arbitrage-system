"""Bounded, path-local chart history. Values are percentages, never price candles."""
import json
import math
import threading
import zlib
import sqlite3
import shutil
from datetime import datetime, timezone
from pathlib import Path

RETENTION = 15 * 86400
MAX_GAP = 20
PERIODS = {"1h": 3600, "6h": 21600, "24h": 86400, "3d": 259200,
           "7d": 604800, "15d": RETENTION}
VENUES = ("Binance", "Gate", "Bitget")


def extrema(points):
    """Keep actual timestamps, including short-lived peaks, in time order."""
    if not points:
        return []
    selected = (points[0], min(points, key=lambda p: p[1]),
                max(points, key=lambda p: p[1]), points[-1])
    return sorted({p[0]: p for p in selected}.values())


def add_point(segments, point):
    if segments and point[0] <= segments[-1][-1][0]:
        return False
    if not segments or point[0] - segments[-1][-1][0] > MAX_GAP:
        segments.append([point])
    else:
        segments[-1] = extrema([*segments[-1], point])
    return True


def observations(groups, observed, errors, now):
    def stamp(*names):
        stamps = [observed.get(name) for name in names]
        if any(name in errors for name in names) or any(
                not isinstance(t, (float, int)) or not math.isfinite(t)
                or not 0 <= now - t <= 15 for t in stamps):
            return None
        return min(stamps) if max(stamps) - min(stamps) <= 10 else None

    for group in groups:
        symbol = group["symbol"]
        basis_seen = False
        for row in group.get("rows", []):
            exchange = row.get("long_exchange")
            feed = {"Binance": "binance_spot", "Gate": "gate_spot", "Bitget": "bitget_spot"}.get(exchange)
            if feed:
                when = stamp(feed, "futures_books")
                value = row.get("open_spread")
                if when is not None and isinstance(value, (int, float)) and math.isfinite(value):
                    yield (symbol, exchange, "open"), when, value
            if not basis_seen:
                when, value = stamp("funding"), row.get("basis")
                source_time = row.get("basis_source_at")
                if source_time is not None and (not math.isfinite(source_time) or not 0 <= now-source_time <= 15):
                    when = None
                if when is not None and isinstance(value, (int, float)) and math.isfinite(value):
                    yield (symbol, "Binance", "basis"), min(when, source_time or when), value
                    basis_seen = True


class HistoryBuffer:
    """Only unpersisted/minute-active extrema, not fifteen days of Python objects."""
    def __init__(self, max_minutes=24000):
        self.lock = threading.Lock()
        self.minutes = {}
        self.max_minutes = max_minutes
        self.dropped = 0

    def observe(self, samples):
        with self.lock:
            for series, when, value in samples:
                when = round(when, 3)
                key = (*series, int(when) // 60 * 60)
                record = self.minutes.setdefault(key, {"segments": [], "version": 0, "saved": 0})
                if add_point(record["segments"], [when, value]):
                    record["version"] += 1
            if len(self.minutes) > self.max_minutes:
                excess = len(self.minutes) - self.max_minutes
                for key in sorted(self.minutes, key=lambda k: k[-1])[:excess]:
                    record = self.minutes.pop(key)
                    self.dropped += record["version"] != record["saved"]

    def snapshot(self, now):
        chunks = {}
        with self.lock:
            for key, record in list(self.minutes.items()):
                if record["version"] == record["saved"]:
                    if key[-1] < int(now) // 60 * 60:
                        del self.minutes[key]
                    continue
                chunk = (*key[:3], key[-1] // 3600 * 3600)
                chunks.setdefault(chunk, {})[str(key[-1])] = (
                    record["version"], [[p[:] for p in segment] for segment in record["segments"]])
        return chunks

    def acknowledge(self, chunk, minutes):
        with self.lock:
            for minute, (version, _) in minutes.items():
                record = self.minutes.get((*chunk[:3], int(minute)))
                if record:
                    record["saved"] = max(record["saved"], version)


def encode_chunk(minutes):
    return zlib.compress(json.dumps(minutes, separators=(",", ":"), allow_nan=False).encode(), 3)


def decode_chunk(payload):
    if not payload:
        return {}
    decoder = zlib.decompressobj()
    raw = decoder.decompress(payload, 131072)
    if not decoder.eof or decoder.unconsumed_tail:
        raise ValueError("Oversized or incomplete spread history chunk")
    return json.loads(raw)


def merge_minutes(existing, incoming):
    for minute, (_, segments) in incoming.items():
        # A restart can write the same minute: merge observations, not replace it.
        points = {p[0]: p for segment in existing.get(minute, []) for p in segment}
        points.update({p[0]: p for segment in segments for p in segment})
        # Preserve segment boundaries: reduced extrema can be >20s apart even
        # when the underlying minute had no gap.
        ranges = sorted([(s[0][0], s[-1][0]) for s in existing.get(minute, []) + segments])
        merged_ranges = []
        for start, end in ranges:
            if merged_ranges and start - merged_ranges[-1][1] <= MAX_GAP:
                merged_ranges[-1][1] = max(merged_ranges[-1][1], end)
            else:
                merged_ranges.append([start, end])
        ordered = sorted(points.values())
        existing[minute] = [extrema([p for p in ordered if start <= p[0] <= end])
                            for start, end in merged_ranges]
    return existing


def chart_series(chunks, start, end, step):
    """At most 720 buckets × four real extrema. Gaps are never interpolated."""
    bins = {}
    previous_end = None
    first_at = last_at = None
    low = high = None
    for payload in chunks:
        minutes = decode_chunk(payload)
        for minute in sorted(minutes, key=int):
            for segment in minutes[minute]:
                points = [p for p in segment if start <= p[0] <= end]
                if not points:
                    continue
                for index, point in enumerate(points):
                    t, value = point
                    gap = index == 0 and previous_end is not None and t - previous_end > MAX_GAP
                    key = min(max(0, math.ceil((end-start)/step)-1), int((t-start)//step))
                    item = bins.setdefault(key, {"points": [], "gap": False, "break": gap})
                    if gap and item["points"]:
                        item["gap"] = True
                    item["points"] = extrema([*item["points"], point])
                    first_at = t if first_at is None else first_at
                    last_at = t
                    low = value if low is None else min(low, value)
                    high = value if high is None else max(high, value)
                previous_end = points[-1][0]
    output = []
    for item in bins.values():
        for index, (t, value) in enumerate(item["points"]):
            output.append([t, value, bool(item["gap"] or (index == 0 and item["break"]))])
    return {"points": output, "first_at": first_at, "last_at": last_at,
            "low": low, "high": high}


class HistoryStore:
    """Daily SQLite files isolate high-volume charts from core DB/full backups.

    Only this module's strict date filenames are pruned. Each retained day has
    a bounded, hourly refreshed SQLite online-backup checkpoint, not 56 copies.
    """
    def __init__(self, directory):
        self.directory = Path(directory)

    @staticmethod
    def day(epoch):
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")

    def connect(self, day, writable=False):
        path = self.directory / (day + ".sqlite3")
        if writable:
            self.directory.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(self.directory).free < 1024**3:
                raise OSError("Insufficient free space for spread history; keep core database headroom")
            connection = sqlite3.connect(path, timeout=3)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS history (
                symbol TEXT NOT NULL, exchange TEXT NOT NULL, metric TEXT NOT NULL,
                hour_at INTEGER NOT NULL, payload BLOB NOT NULL,
                PRIMARY KEY(symbol,exchange,metric,hour_at)) WITHOUT ROWID""")
        else:
            connection = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=3)
        connection.execute("PRAGMA cache_size=-2048")
        return connection

    def persist(self, buffer, now):
        pending = list(buffer.snapshot(now).items())
        for offset in range(0, len(pending), 50):
            batch = pending[offset:offset+50]
            for day in sorted({self.day(key[-1]) for key, _ in batch}):
                same_day = [(key, minutes) for key, minutes in batch if self.day(key[-1]) == day]
                connection = self.connect(day, writable=True)
                try:
                    with connection:
                        for key, minutes in same_day:
                            row = connection.execute(
                                "SELECT payload FROM history WHERE symbol=? AND exchange=? AND metric=? AND hour_at=?", key).fetchone()
                            payload = encode_chunk(merge_minutes(decode_chunk(row[0] if row else None), minutes))
                            connection.execute("INSERT INTO history VALUES(?,?,?,?,?) ON CONFLICT(symbol,exchange,metric,hour_at) DO UPDATE SET payload=excluded.payload",
                                               (*key, payload))
                    for key, minutes in same_day:
                        buffer.acknowledge(key, minutes)
                finally:
                    connection.close()
        return len(pending)

    def chunks(self, symbol, exchange, metric, start, end):
        first_day = int(start)//86400*86400
        for midnight in range(first_day, int(end)//86400*86400+1, 86400):
            day = self.day(midnight)
            if not (self.directory/(day+".sqlite3")).exists():
                continue
            connection = self.connect(day)
            try:
                cursor = connection.execute("""SELECT payload FROM history
                    WHERE symbol=? AND exchange=? AND metric=? AND hour_at>=? AND hour_at<=?
                    ORDER BY hour_at""", (symbol, exchange, metric, int(start)//3600*3600, int(end)//3600*3600))
                for (payload,) in cursor:
                    yield payload
            finally:
                connection.close()

    def maintain(self, now):
        cutoff = self.day(now-RETENTION)
        checkpoint_dir = self.directory/"checkpoints"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.prune(now)
        for path in self.directory.glob("????-??-??.sqlite3"):
            try:
                datetime.strptime(path.stem, "%Y-%m-%d")
            except ValueError:
                continue
            if path.stem < cutoff:
                continue
            backup = checkpoint_dir/path.name
            wal = path.with_name(path.name+"-wal")
            modified = max(path.stat().st_mtime, wal.stat().st_mtime if wal.exists() else 0)
            if backup.exists() and backup.stat().st_mtime >= modified:
                continue
            if shutil.disk_usage(self.directory).free < 1024**3 + path.stat().st_size:
                raise OSError("Insufficient free space for chart checkpoint")
            source = self.connect(path.stem)
            temporary = backup.with_suffix(".pending")
            destination = sqlite3.connect(temporary)
            try:
                source.backup(destination, pages=256, sleep=.01)
            finally:
                destination.close()
                source.close()
            temporary.replace(backup)
    def prune(self, now):
        cutoff = self.day(now-RETENTION)
        for directory in (self.directory, self.directory/"checkpoints"):
            for path in directory.glob("????-??-??.sqlite3"):
                try:
                    datetime.strptime(path.stem, "%Y-%m-%d")
                except ValueError:
                    continue
                if path.stem < cutoff:
                    # Exact expired date shards only; never touch business DBs.
                    for suffix in ("", "-wal", "-shm"):
                        path.with_name(path.name+suffix).unlink(missing_ok=True)

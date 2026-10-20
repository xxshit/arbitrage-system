"""Observed opening episodes, independent of alarms and raw curve retention.

Only synchronized public quotes enter this ledger. ±1% opens an episode;
returning inside ±0.5% rearms that direction. Missing observations never prove
convergence. Old minute extrema cannot reconstruct these counts.
"""
import json
import shutil
import sqlite3
import threading
from pathlib import Path

from opportunity_pool import quote_metrics

WINDOWS = {"24h": 86400, "3d": 3*86400, "7d": 7*86400, "15d": 15*86400}
RETENTION = 90*86400


class ActivityLedger:
    def __init__(self, directory, limit=4000):
        self.directory = Path(directory)
        self.lock = threading.Lock()
        self.states = {}
        self.dirty = {}
        self.pending = {}
        self.limit = limit
        self.ready = False
        self.summaries = {}

    def connect(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.directory).free < 1024**3:
            raise OSError("Insufficient free space for opportunity episode ledger")
        conn = sqlite3.connect(self.directory/"opportunity-episodes.sqlite3", timeout=3)
        conn.execute("PRAGMA cache_size=-1024")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA journal_size_limit=1048576")
        # A separate bounded database; never pruned by the raw-curve day scanner.
        conn.execute("PRAGMA max_page_count=8192")
        conn.execute("CREATE TABLE IF NOT EXISTS states (symbol TEXT, exchange TEXT, payload TEXT, PRIMARY KEY(symbol,exchange)) WITHOUT ROWID")
        conn.execute("""CREATE TABLE IF NOT EXISTS episodes (
            symbol TEXT, exchange TEXT, started REAL, direction INTEGER,
            peak REAL, peak_at REAL, ended REAL, continuous INTEGER,
            PRIMARY KEY(symbol,exchange,started,direction)) WITHOUT ROWID""")
        return conn

    def restore(self):
        with self.connect() as conn:
            states = {(s, e): json.loads(payload) for s, e, payload in
                      conn.execute("SELECT symbol,exchange,payload FROM states LIMIT ?", (self.limit,))}
        conn.close()
        with self.lock:
            self.states = states
            self.ready = True

    def observe(self, quotes, now):
        with self.lock:
            if not self.ready:
                return
            for path, live in quotes.items():
                strategy, symbol, exchange, short = path
                if strategy != "spot_futures" or short != "Binance":
                    continue
                quote = quote_metrics(live, now)
                if not quote:
                    continue
                key = symbol, exchange
                if key not in self.states and len(self.states) >= self.limit:
                    continue
                at, value = quote["at"], quote["open_spread"]
                state = self.states.setdefault(key, dict(first=at, last=0, value=None, active=None))
                if at <= state["last"]:
                    continue
                contiguous = 0 < at-state["last"] <= 20
                event = state["active"]
                if event:
                    direction = event[1]
                    if not contiguous:
                        event[5] = False
                    if direction*value > direction*event[2]:
                        event[2:4] = [value, at]
                    if direction*value <= .5+1e-9:
                        event[4] = at
                        state["active"] = None
                    self.pending[(*key, event[0], direction)] = event[:]
                if state["active"] is None and abs(value) >= 1-1e-9:
                    direction = 1 if value > 0 else -1
                    # First observation outside the threshold is left-censored.
                    event = [at, direction, value, at, None,
                             bool(contiguous and state["value"] is not None
                                  and direction*state["value"] < 1-1e-9)]
                    state["active"] = event
                    self.pending[(*key, at, direction)] = event[:]
                state.update(last=at, value=value)
                self.dirty[key] = json.dumps(state, separators=(",", ":"), allow_nan=False)
            # Outages remain bounded. Counts are explicitly observed, not total.
            while len(self.pending) > 24000:
                self.pending.pop(next(iter(self.pending)))

    def persist(self, now):
        with self.lock:
            expired = [key for key, state in self.states.items() if state["last"] < now-RETENTION]
            for key in expired:
                self.states.pop(key)
                self.dirty.pop(key, None)
            dirty, pending = dict(self.dirty), dict(self.pending)
        conn = self.connect()
        try:
            with conn:
                conn.executemany("DELETE FROM states WHERE symbol=? AND exchange=?", expired)
                conn.executemany("INSERT OR REPLACE INTO states VALUES(?,?,?)",
                                 [(*key, value) for key, value in dirty.items()])
                conn.executemany("INSERT OR REPLACE INTO episodes VALUES(?,?,?,?,?,?,?,?)",
                                 [(*key[:2], *event) for key, event in pending.items()])
                conn.execute("DELETE FROM episodes WHERE started<?", (now-RETENTION,))
                # Hard row cap independent of market activity. New observations
                # stay a lower bound when older summaries have expired.
                conn.execute("""DELETE FROM episodes WHERE (symbol,exchange,started,direction) IN
                    (SELECT symbol,exchange,started,direction FROM episodes
                     ORDER BY started DESC LIMIT -1 OFFSET 100000)""")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            with self.lock:
                for key, value in dirty.items():
                    if self.dirty.get(key) == value:
                        self.dirty.pop(key, None)
                for key, value in pending.items():
                    if self.pending.get(key) == value:
                        self.pending.pop(key, None)
            output = {}
            for symbol, exchange, started, direction, peak, peak_at, ended, continuous in conn.execute(
                    "SELECT * FROM episodes WHERE started>? ORDER BY started DESC", (now-RETENTION,)):
                profile = output.setdefault((symbol, exchange), dict(
                    count=0, returned=0, peak=0, peak_at=None, last_at=started,
                    windows={name: dict(positive=0, negative=0, recent=[]) for name in WINDOWS}))
                profile["count"] += 1
                profile["returned"] += bool(ended is not None and continuous)
                if abs(peak) > abs(profile["peak"]):
                    profile.update(peak=peak, peak_at=peak_at)
                for name, seconds in WINDOWS.items():
                    if started <= now-seconds:
                        continue
                    window = profile["windows"][name]
                    window["positive" if direction > 0 else "negative"] += 1
                    if len(window["recent"]) < 5:
                        window["recent"].append(dict(started=started, peak=peak, peak_at=peak_at,
                                                     ended=ended, returned=bool(ended and continuous)))
            with self.lock:
                for key, state in self.states.items():
                    profile = output.setdefault(key, dict(count=0, returned=0, peak=0, peak_at=None,
                        last_at=None, windows={name: dict(positive=0, negative=0, recent=[]) for name in WINDOWS}))
                    profile.update(since=state["first"], observed_at=state["last"], computed_at=now)
                self.summaries = output
        finally:
            conn.close()

    def snapshot(self):
        with self.lock:
            return self.summaries

    def checkpoint(self):
        """Small online backup alongside the existing hourly curve checkpoints."""
        path = self.directory/"opportunity-episodes.sqlite3"
        if not path.exists():
            return
        if shutil.disk_usage(self.directory).free < 1024**3+path.stat().st_size:
            raise OSError("Insufficient free space for episode checkpoint")
        target = self.directory/"checkpoints"
        target.mkdir(exist_ok=True)
        temporary = target/"opportunity-episodes.pending"
        source = self.connect()
        destination = sqlite3.connect(temporary)
        try:
            source.backup(destination, pages=128, sleep=.01)
        finally:
            destination.close()
            source.close()
        temporary.replace(target/path.name)

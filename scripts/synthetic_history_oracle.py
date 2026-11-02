"""Bounded verification helpers for generated history workloads only.

This module never imports the app or opens a business database. Expected full
value hashes and reminder revisions live in an exclusively created SQLite
ledger; only one generated batch is materialized at a time.
"""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


class PulseOracle:
    def __init__(self, path):
        self.path = Path(path).resolve()
        with self.path.open("xb"):
            pass  # Exclusive ownership: never reuse a prior test ledger.
        self.lock = threading.Lock()
        self.count = self.max_batch_rows = self.max_previous_rows = 0
        with closing(self.connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE expected (id TEXT PRIMARY KEY, hash TEXT NOT NULL, revision TEXT NOT NULL) WITHOUT ROWID")

    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.execute("PRAGMA cache_size=-256")
        return conn

    def previous(self, identities):
        identities = tuple(identities)
        assert len(identities) <= 4096, "unbounded synthetic batch"
        if not identities:
            return {}
        values = {}
        with closing(self.connect()) as conn:
            for start in range(0, len(identities), 250):
                batch = identities[start:start+250]
                sql = "SELECT id,revision FROM expected WHERE id IN ("+",".join("?" for _ in batch)+")"
                values.update((identity, json.loads(raw)) for identity, raw in conn.execute(sql, batch))
        with self.lock:
            self.max_previous_rows = max(self.max_previous_rows, len(values))
        return values

    def commit(self, saved):
        assert len(saved) <= 4096, "unbounded synthetic batch"
        rows = [(identity, hashlib.sha256(canonical(value).encode()).hexdigest(),
                 canonical({key: value[key] for key in ("reminder", "version", "peak_at")}))
                for identity, value in saved.items()]
        with self.lock, closing(self.connect()) as conn:
            with conn:
                conn.executemany("INSERT INTO expected VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET hash=excluded.hash,revision=excluded.revision", rows)
            self.count = conn.execute("SELECT COUNT(*) FROM expected").fetchone()[0]
            self.max_batch_rows = max(self.max_batch_rows, len(rows))

    def batches(self, size=250):
        assert 1 <= size <= 500
        after = None
        while True:
            with closing(self.connect()) as conn:
                rows = (conn.execute("SELECT id,hash FROM expected ORDER BY id LIMIT ?", (size,)).fetchall()
                        if after is None else
                        conn.execute("SELECT id,hash FROM expected WHERE id>? ORDER BY id LIMIT ?", (after, size)).fetchall())
            if not rows:
                return
            after = rows[-1][0]
            yield rows

    def digest(self):
        result = hashlib.sha256(b"[")
        count = 0
        for batch in self.batches():
            for identity, value_hash in batch:
                if count:
                    result.update(b",")
                result.update(canonical([identity, value_hash]).encode())
                count += 1
        result.update(b"]")
        assert count == len(self)
        return result.hexdigest()

    def status(self):
        with self.lock:
            return dict(rows=self.count, max_batch_rows=self.max_batch_rows,
                        max_previous_rows=self.max_previous_rows, storage="sqlite_full_hash_and_revision")

    def verify(self):
        with closing(self.connect()) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT COUNT(*) FROM expected").fetchone()[0] == len(self)
        return dict(**self.status(), exact_hash=self.digest(), integrity="ok")

    def __len__(self):
        with self.lock:
            return self.count


@dataclass(frozen=True)
class TurnoverCohort:
    """Historical seeds cross the REAL 24h cutoff; no runtime clock is changed."""
    total: int
    first_epoch: int
    per_second: int = 4
    symbols: int = 500

    def __post_init__(self):
        assert self.total > 0 and self.per_second > 0 and self.symbols > 0
        assert isinstance(self.first_epoch, int)

    def epoch(self, index):
        assert 0 <= index < self.total
        return self.first_epoch + index // self.per_second

    def bounds(self, start, now):
        assert math.isfinite(start) and math.isfinite(now) and start <= now
        lower = max(0, min(self.total, math.ceil(start-self.first_epoch)*self.per_second))
        upper = max(0, min(self.total, (math.floor(now-self.first_epoch)+1)*self.per_second))
        return lower, max(lower, upper)

    def count(self, start, now, symbol_index=None):
        lower, upper = self.bounds(start, now)
        if symbol_index is None:
            return upper-lower
        assert 0 <= symbol_index < self.symbols
        return (upper-1-symbol_index)//self.symbols - (lower-1-symbol_index)//self.symbols

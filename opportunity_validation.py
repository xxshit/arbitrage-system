"""Prospective, path-local score cohorts. No orders, hindsight or notifications.

Freeze the standard 1000-USDT/72H score once per path, observe for seven days,
then permit a new cohort. Fixed-horizon BBO scenarios are not executed returns.
Only bounded aggregates are retained; existing market history is never changed.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import statistics
import threading

from opportunity_pool import POLICY, number, quote_metrics

HORIZONS = (3600, 86400, 3*86400, 7*86400)
GRACE = 120
NOTIONAL = 1000
HOLD_HOURS = 72
FILE = "opportunity-validation.sqlite3"


def path_key(item):
    return ("spot", item["symbol"], item["long_exchange"], item["short_exchange"])


def packed(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def close_scenario(entry, quote):
    """Equal BASE quantity P/L; never subtract percentages with different bases."""
    quantity = entry["notional"]/entry["spot_ask"]
    long, short = quote["long"], quote["short"]
    if any(not number(leg.get(field)) or leg[field]*.5 < quantity
           for leg, field in ((long, "bid_qty"), (short, "ask_qty"))):
        return None
    spot, future = long["bid"], short["ask"]
    gross = 100*((spot-entry["spot_ask"])+(entry["future_bid"]-future))/entry["spot_ask"]
    fees = 1.1*(entry["policy"]["spot_fee"]*(entry["spot_ask"]+spot)
                + entry["policy"]["futures_fee"]*(entry["future_bid"]+future))/entry["spot_ask"]
    if not all(number(v) for v in (gross, fees, gross-fees-entry["policy"]["slippage"])):
        return None
    return dict(gross=gross, fees=fees, slippage=entry["policy"]["slippage"],
                net=gross-fees-entry["policy"]["slippage"],
                spot_bid=spot, future_ask=future, quoted_at=quote["at"])


class ValidationLedger:
    def __init__(self, directory, limit=2000, max_records=10000):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.active = {}
        self.limit, self.max_records = limit, max_records
        self.ready = False
        self.total = 0
        self.last_flush = 0
        self.capacity_limited = False
        self.storage_bytes = 0
        self.last_enrollment_at = None

    def connect(self, write=False):
        path = self.directory/FILE
        if write:
            self.directory.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(self.directory).free < 1024**3:
                raise OSError("Insufficient free space for validation ledger")
            conn = sqlite3.connect(path, timeout=3)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA journal_size_limit=1048576")
            conn.execute("PRAGMA max_page_count=32768")  # 128MiB, plus bounded WAL/checkpoint.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS cohorts (
                  id TEXT PRIMARY KEY, strategy TEXT, symbol TEXT, long_exchange TEXT,
                  short_exchange TEXT, entered REAL, score INTEGER, model INTEGER,
                  entry TEXT, progress TEXT, finished INTEGER NOT NULL DEFAULT 0);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_path
                  ON cohorts(strategy,symbol,long_exchange,short_exchange) WHERE finished=0;
                CREATE INDEX IF NOT EXISTS cohort_path_time
                  ON cohorts(strategy,symbol,long_exchange,short_exchange,entered);
                CREATE INDEX IF NOT EXISTS cohort_model_time ON cohorts(model,entered);
                CREATE TABLE IF NOT EXISTS outcomes (
                  id TEXT, horizon INTEGER, status TEXT, net REAL, payload TEXT,
                  PRIMARY KEY(id,horizon)) WITHOUT ROWID;
            """)
        else:
            if not path.exists():
                return None
            conn = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=2)
            conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA cache_size=-1024")
        return conn

    def restore(self):
        conn = self.connect(write=True)
        try:
            records = {}
            for row in conn.execute("SELECT id,strategy,symbol,long_exchange,short_exchange,entered,score,model,entry,progress FROM cohorts WHERE finished=0 LIMIT ?", (self.limit,)):
                identity, strategy, symbol, long, short, entered, score, model, entry, progress = row
                records[(strategy, symbol, long, short)] = dict(id=identity, path=(strategy,symbol,long,short),
                    entered=entered, score=score, model=model, entry=json.loads(entry),
                    progress=json.loads(progress), revision=0, saved_revision=0, urgent_revision=0, new=False)
            total = conn.execute("SELECT COUNT(*) FROM cohorts").fetchone()[0]
        finally:
            conn.close()
        with self.lock:
            self.active, self.total, self.ready = records, total, True
            self.storage_bytes = (self.directory/FILE).stat().st_size

    def enroll(self, items, now):
        accepted = 0
        with self.lock:
            if not self.ready:
                return 0
            self.last_enrollment_at = now
            self.capacity_limited = len(self.active) >= self.limit or self.total >= self.max_records or self.storage_bytes >= 64*1024**2
            # Alphabetic order, never a high-score-only sample selection.
            for item in sorted(items, key=path_key):
                key = path_key(item)
                if key in self.active or not item.get("candidate", True):
                    continue
                # Reserve half the physical ceiling for active outcomes/index growth.
                # Reaching admission capacity must not prevent finishing existing cohorts.
                if len(self.active) >= self.limit or self.total >= self.max_records or self.storage_bytes >= 64*1024**2:
                    self.capacity_limited = True
                    break
                quote = item.get("_validation_quote")
                source = item.get("_validation_source")
                if (not quote or not source or not number(item.get("score"))
                        or not number(quote.get("at")) or not 0 <= now-quote["at"] <= 4
                        or quote.get("capacity") is None or quote["capacity"] < NOTIONAL):
                    continue
                entry = dict(notional=NOTIONAL, score_hold_hours=HOLD_HOURS,
                    policy={k: POLICY[k] for k in ("spot_fee","futures_fee","slippage")},
                    spot_ask=quote["long"]["ask"], future_bid=quote["short"]["bid"],
                    quoted_at=quote["at"], source=deepcopy(source),
                    raw_symbol=item["_validation_raw"], interval=item.get("short_interval"),
                    score_parts=deepcopy(item.get("score_parts", {})),
                    risks=list(item.get("risks", [])), drivers=list(item.get("drivers", [])),
                    status=item.get("status"), scenario=deepcopy(item.get("scenario", {})))
                progress = dict(last_at=now, windows={str(h): dict(covered=0, max_gap=0,
                    low=None, high=None, samples=0) for h in HORIZONS}, results={})
                identity = hashlib.sha256(packed([key, now, item["score_version"]]).encode()).hexdigest()[:32]
                self.active[key] = dict(id=identity, path=key, entered=now, score=item["score"],
                    model=item["score_version"], entry=entry, progress=progress,
                    revision=1, saved_revision=0, urgent_revision=1, new=True)
                self.total += 1
                accepted += 1
        return accepted

    @staticmethod
    def finished(record):
        results = record["progress"]["results"]
        return len(results) == len(HORIZONS) and all(r.get("funding_final") for r in results.values())

    def observe(self, quotes, now, identities=None):
        with self.lock:
            if not self.ready:
                return
            for record in self.active.values():
                _, symbol, long, short = record["path"]
                path = ("spot_futures", symbol, long, short)
                progress, entry = record["progress"], record["entry"]
                quote = quote_metrics(quotes.get(path), now)
                changed_source = (identities is not None and path in identities
                                  and identities[path] != entry["source"])
                if changed_source:
                    quote = None
                value = close_scenario(entry, quote) if quote else None
                at = quote["at"] if value else None
                for horizon in HORIZONS:
                    key, due = str(horizon), record["entered"]+horizon
                    if key in progress["results"]:
                        continue
                    window = progress["windows"][key]
                    if value and at > progress["last_at"]:
                        elapsed = max(0, min(at,due)-min(progress["last_at"],due))
                        if at-progress["last_at"] <= 20:
                            window["covered"] += elapsed
                        else:
                            window["max_gap"] = max(window["max_gap"], elapsed)
                        if at <= due:
                            window["samples"] += 1
                            window["low"] = min(window["low"], value["net"]) if window["low"] is not None else value["net"]
                            window["high"] = max(window["high"], value["net"]) if window["high"] is not None else value["net"]
                    terminal = value is not None and due <= at <= due+GRACE
                    if terminal or now > due+GRACE or changed_source:
                        if not terminal:
                            window["max_gap"] = max(window["max_gap"], max(0,min(now,due)-progress["last_at"]))
                        coverage = min(1,window["covered"]/horizon)
                        valid = bool(terminal and coverage >= .8 and window["max_gap"] <= 300)
                        reason = ("配对发生变更" if changed_source else "到期缺少足量同步首档" if not terminal
                                  else "观测覆盖不足" if not valid else None)
                        progress["results"][key] = dict(due_at=due, observed_at=at if terminal else None,
                            status="observed" if valid else "insufficient", reason=reason,
                            net=value["net"] if valid else None,
                            terminal=deepcopy(value) if terminal else None,
                            coverage=coverage, max_gap=window["max_gap"], samples=window["samples"],
                            low=window["low"], high=window["high"], funding=None, funding_final=changed_source)
                        record["revision"] += 1
                        record["urgent_revision"] = record["revision"]
                if value and at > progress["last_at"]:
                    progress["last_at"] = at
                    record["revision"] += 1

    def funding_requests(self, now):
        with self.lock:
            return [(r["id"], int(h), r["entry"]["raw_symbol"], r["entered"], outcome["due_at"])
                    for r in self.active.values() for h, outcome in r["progress"]["results"].items()
                    if not outcome.get("funding_final") and now >= outcome["due_at"]+120]

    def set_funding(self, identity, horizon, evidence, now):
        with self.lock:
            record = next((r for r in self.active.values() if r["id"] == identity), None)
            if not record:
                return
            outcome = record["progress"]["results"].get(str(horizon))
            if not outcome or outcome.get("funding_final"):
                return
            outcome.update(funding=deepcopy(evidence), funding_checked_at=now,
                           funding_final=bool(evidence.get("complete") or now-outcome["due_at"] >= 3600))
            record["revision"] += 1
            record["urgent_revision"] = record["revision"]

    def persist(self, now):
        with self.lock:
            # Newly enrolled/terminal records are durable promptly. Ordinary
            # min/max/coverage updates use one five-minute checkpoint, not 5s I/O.
            records = [deepcopy(r) for r in self.active.values() if r["revision"] != r["saved_revision"]
                       and (r["new"] or r["urgent_revision"] > r["saved_revision"] or now-self.last_flush >= 300)]
        if not records:
            return 0
        conn = self.connect(write=True)
        try:
            with conn:
                for r in records:
                    conn.execute("INSERT OR IGNORE INTO cohorts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (r["id"], *r["path"], r["entered"], r["score"], r["model"], packed(r["entry"]),
                         packed(r["progress"]), int(self.finished(r))))
                    # Frozen entry, score, version and entered time NEVER UPDATE.
                    conn.execute("UPDATE cohorts SET progress=?,finished=? WHERE id=?",
                                 (packed(r["progress"]), int(self.finished(r)), r["id"]))
                    for horizon, result in r["progress"]["results"].items():
                        conn.execute("INSERT INTO outcomes VALUES(?,?,?,?,?) ON CONFLICT(id,horizon) DO UPDATE SET payload=excluded.payload",
                            (r["id"], int(horizon), result["status"], result["net"], packed(result)))
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        with self.lock:
            self.storage_bytes = (self.directory/FILE).stat().st_size
            for saved in records:
                live = self.active.get(saved["path"])
                if live and live["id"] == saved["id"]:
                    live.update(saved_revision=saved["revision"], new=False)
                    if self.finished(saved) and live["revision"] == saved["revision"]:
                        self.active.pop(saved["path"])
            if now-self.last_flush >= 300:
                self.last_flush = now
        return len(records)

    def read(self, now, symbol=None, exchange=None, horizon=86400, model=2):
        response = dict(model=model, horizon=horizon, as_of=now, notional=NOTIONAL,
            score_hold_hours=HOLD_HOURS, cohorts=[], bands=[], total=0, pending=0,
            observed=0, insufficient=0, awaiting_collection=0, capacity_limited=self.capacity_limited,
            last_enrollment_at=self.last_enrollment_at,
            note="上线后标准1000 USDT前瞻样本；同路径7天一轮。首档测算不等于成交或胜率，资费率累计未计入价差净测算。")
        conn = self.connect()
        if conn is None:
            return response
        try:
            where = "c.model=? AND c.entered>=?"
            params = [model, now-90*86400]
            if symbol is not None:
                where += " AND c.strategy='spot' AND c.symbol=? AND c.long_exchange=? AND c.short_exchange='Binance'"
                params += [symbol,exchange]
            buckets = {label: dict(label=label, total=0,pending=0,observed=0,insufficient=0,
                                  awaiting_collection=0, positive=0, values=[]) for label in ("0–39","40–59","60–79","80–100")}
            for score, entered, status, net in conn.execute(
                    f"SELECT c.score,c.entered,o.status,o.net FROM cohorts c LEFT JOIN outcomes o ON o.id=c.id AND o.horizon=? WHERE {where}", [horizon,*params]):
                label = "0–39" if score < 40 else "40–59" if score < 60 else "60–79" if score < 80 else "80–100"
                bucket = buckets[label]
                bucket["total"] += 1
                state = status or ("pending" if now < entered+horizon else "awaiting_collection")
                bucket[state] += 1
                if state == "observed" and number(net):
                    bucket["positive"] += net > 0
                    bucket["values"].append(net)
            for bucket in buckets.values():
                values = bucket.pop("values")
                bucket["median_net"] = statistics.median(values) if values else None
                for field in ("total","pending","observed","insufficient","awaiting_collection"):
                    response[field] += bucket[field]
                response["bands"].append(bucket)
            for identity, strategy, coin, long, short, entered, score, version, entry in conn.execute(
                    f"SELECT c.id,c.strategy,c.symbol,c.long_exchange,c.short_exchange,c.entered,c.score,c.model,c.entry FROM cohorts c WHERE {where} ORDER BY c.entered DESC LIMIT 12", params):
                outcomes = {str(h): json.loads(payload) for h,payload in conn.execute(
                    "SELECT horizon,payload FROM outcomes WHERE id=?", (identity,))}
                response["cohorts"].append(dict(id=identity,strategy=strategy,symbol=coin,long_exchange=long,
                    short_exchange=short,entered_at=entered,score=score,model=version,entry=json.loads(entry),outcomes=outcomes))
        finally:
            conn.close()
        return response

    def checkpoint(self):
        path = self.directory/FILE
        if not path.exists():
            return
        if shutil.disk_usage(self.directory).free < 1024**3+path.stat().st_size:
            raise OSError("Insufficient free space for validation checkpoint")
        target = self.directory/"checkpoints"
        target.mkdir(exist_ok=True)
        temporary = target/(FILE+".pending")
        source, destination = self.connect(), sqlite3.connect(temporary)
        try:
            source.backup(destination, pages=128, sleep=.01)
        finally:
            destination.close()
            source.close()
        temporary.replace(target/FILE)

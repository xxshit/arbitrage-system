"""Offline execution of the application's real recurring scheduling functions.

Only AST-selected pool declarations/functions are loaded, with generated network
values and thread-local in-memory publication sinks. No app initialization, SQL,
credentials, sockets, production state, pushes or fake production observations.
"""
import ast
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace


LIMITS = {"index": 15, "price-history": 2, "major-analysis": 3, "momentum": 10,
          "sudden-pump": 4, "funding-binance": 4, "funding-bybit": 4, "funding-okx": 4}
FUNCTIONS = {"background_http_batch", "refresh_index_components", "backfill_price_history",
             "fetch_major_market_overview", "load_momentum_scores",
             "scan_intraday_sudden_pumps", "run_funding_sync_batch"}


class Column:
    def in_(self, values): return None
    def __ge__(self, value): return None


class Query:
    def __init__(self, rows=()): self.rows = rows
    def with_entities(self, *args): return self
    def filter(self, *args): return self
    def filter_by(self, **kwargs): return self
    def all(self): return list(self.rows)
    def first(self): return None


class Table:
    def __init__(self, name, rows=()): self.name, self.query = name, Query(rows)
    def __getattr__(self, name): return Column()
    def __call__(self, **values): return SimpleNamespace(table=self.name, **values)


class Session:
    def __init__(self): self.local = threading.local()
    def query(self, *args): return Query()
    def rollback(self): pass
    def remove(self): pass
    def commit(self): pass
    def add(self, row):
        if not hasattr(self.local, "rows"): self.local.rows = []
        self.local.rows.append(row)
    def take(self):
        rows = getattr(self.local, "rows", [])
        self.local.rows = []
        return rows


class Schedule:
    def __init__(self): self.last_cleanup, self.done_count = time.time(), 0
    def plan(self, targets, *args): return [(key, False) for key in targets]
    def done(self, key, target, now, latest, **kwargs):
        assert "error" not in kwargs
        self.done_count += 1
    def save(self, status): self.status = status


class BackgroundExecutorWorkload:
    def __init__(self, repo):
        path = Path(repo) / "app.py"
        selected = []
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
                    t.id == "BACKGROUND_HTTP_EXECUTORS" for t in node.targets):
                selected.append(node)
            elif isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
                selected.append(node)
        assert len(selected) == len(FUNCTIONS) + 1
        self.ns = dict(ThreadPoolExecutor=ThreadPoolExecutor, as_completed=as_completed,
                       wait=wait, contextmanager=contextmanager, datetime=datetime,
                       time=time, threading=threading, SimpleNamespace=SimpleNamespace, json=json)
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), self.ns)
        self.lock = threading.Lock()
        self.active, self.peak, self.calls, self.rounds = Counter(), Counter(), Counter(), Counter()
        self.threads = defaultdict(set)
        self.tick = self.cycles = 0
        self.session = Session()
        self.callers = ThreadPoolExecutor(max_workers=8, thread_name_prefix="soak-background-callers")
        self.ns.update(
            db=SimpleNamespace(session=self.session),
            LatestDualFuturesSnapshot=Table("latest", [(f"I{i}/USDT", "Bybit", "OKX") for i in range(10)]),
            IndexComponentSnapshot=Table("index"), FuturesPriceHistory=Table("price"),
            INDEX_COMPONENT_CURSOR=0, PRICE_HISTORY_BUCKET_SECONDS=300,
            PRICE_HISTORY_RETENTION_SECONDS=8*86400,
            price_history_integrity=lambda groups: {"missing_symbols": ["P0/USDT", "P1/USDT"]},
            fetch_index_components=lambda venue, symbol: self.network("index", symbol, self.components),
            fetch_price_history_from_binance=lambda symbol: self.network("price-history", symbol, (symbol, self.prices)),
            MAJOR_MARKET_SYMBOLS=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            MAJOR_MARKET_CACHE={"items": [], "ts": 0}, MAJOR_MARKET_CACHE_SECONDS=30,
            fetch_major_market_symbol=lambda raw: self.network("major-analysis", raw,
                {"symbol": raw[:-4]+"/USDT", "change_24h": 1, "price": 100+self.tick/4}),
            MOMENTUM_SCORE_CACHE={"ts": 0}, MOMENTUM_SCORE_CACHE_SECONDS=300,
            MOMENTUM_SCORE_LOCK=threading.Lock(),
            momentum_price_candidates=lambda: [{"symbol": f"M{i}/USDT", "candidate_score": i} for i in range(20)],
            fetch_momentum_structure=lambda item: self.network("momentum", item["symbol"], dict(item, score=item["candidate_score"])),
            momentum_score_label=lambda score: "synthetic", momentum_score_reason=lambda item: "synthetic",
            canonical_market_symbol=lambda symbol: symbol, utc_now_naive=datetime.now,
            format_shanghai_time=lambda value, fmt: value.strftime(fmt),
            SUDDEN_PUMP_SCAN_LOCK=threading.Lock(), SUDDEN_PUMP_FAST_SCAN_UNTIL=0,
            SUDDEN_PUMP_FAST_SCAN_HOLD_SECONDS=30, SUDDEN_PUMP_CONFIRMATIONS={},
            load_latest_market_snapshot=lambda: {"symbols": []}, runtime_number=lambda *args: 15,
            sudden_pump_rough_candidates=lambda *args: [f"S{i}/USDT" for i in range(8)],
            fetch_sudden_pump_context=lambda symbol, threshold: self.network("sudden-pump", symbol, None),
            funding_sync_catalog=lambda venue: {f"F{i}": {"candidates": [f"F{i}"]} for i in range({"Binance": 8, "Bybit": 64, "OKX": 48}[venue])},
            funding_sync_latest=lambda *args: {}, DAY_MS=86400000,
            FUNDING_HISTORY_CLIENT=SimpleNamespace(fetch=self.funding_fetch),
            store_settled_funding=self.store_funding, expected_settlement=lambda *args: 0,
            FundingHistoryError=type("FundingHistoryError", (Exception,), {}),
        )
        self.components = [{"name": "synthetic", "source": "synthetic", "weight": 1.0}]
        self.prices = [(1800000000+i*300, 100+i/4) for i in range(20)]

    def network(self, lane, identity, result):
        with self.lock:
            self.active[lane] += 1
            self.peak[lane] = max(self.peak[lane], self.active[lane])
            self.threads[lane].add(threading.current_thread())
            assert self.active[lane] <= LIMITS[lane]
            assert len(self.threads[lane]) <= LIMITS[lane], "workers recreated between rounds"
            self.calls[lane] += 1
        try:
            # Vary temporary JSON allocation size without retaining a history of
            # payloads. Only the exact generated result is returned to the caller.
            size = 65536 + ((self.tick*131071 + len(identity)*8191) % 524288)
            value = json.loads(json.dumps({"identity": identity, "padding": "x"*size, "tick": self.tick}))
            assert value["identity"] == identity and len(value["padding"]) == size
            time.sleep(.005)
            return result
        finally:
            with self.lock: self.active[lane] -= 1

    def funding_fetch(self, venue, candidates, start, end):
        return self.network("funding-"+venue.lower(), candidates[0],
                            (candidates[0], [(1800000000000, .005)]))

    def store_funding(self, venue, rows):
        assert len(rows) == 1 and rows[0][1:] == (1800000000000, .005)
        return 1

    def run(self, lane):
        ns = self.ns
        if lane.startswith("funding-"):
            venue = {"binance": "Binance", "bybit": "Bybit", "okx": "OKX"}[lane[8:]]
            schedule = Schedule()
            result = ns["run_funding_sync_batch"](venue, schedule)
            expected = {"Binance": 8, "Bybit": 64, "OKX": 48}[venue]
            assert result["attempted"] == result["inserted"] == schedule.done_count == expected
            assert result["errors"] == 0
        elif lane == "major-analysis":
            result = ns["fetch_major_market_overview"](True)
            assert [x["symbol"] for x in result] == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
            assert all(x["price"] == 100+self.tick/4 for x in result)
        elif lane == "momentum":
            result = ns["load_momentum_scores"](True)
            assert [x["score"] for x in result["items"]] == list(range(19, 9, -1))
            assert len(result["by_symbol"]) == 20
        elif lane == "sudden-pump":
            assert ns["scan_intraday_sudden_pumps"]() == 0
        elif lane == "index":
            ns["refresh_index_components"]()
            rows = self.session.take()
            assert len(rows) == 20 and all(json.loads(x.components_json) == self.components for x in rows)
        elif lane == "price-history":
            ns["backfill_price_history"]([])
            rows = self.session.take()
            assert len(rows) == 40
            assert sorted((x.bucket_at, x.price) for x in rows if x.symbol == "P0/USDT") == self.prices
        else:
            raise AssertionError(lane)
        with self.lock: self.rounds[lane] += 1

    def step(self):
        # Each caller waits for its own batch, just like the real scheduler.
        due = []
        if self.tick % 2 == 0:
            due += ["sudden-pump", "funding-binance", "funding-bybit", "funding-okx"]
        if self.tick % 6 == 0: due += ["major-analysis"]
        if self.tick % 24 == 0: due += ["price-history"]
        if self.tick % 60 == 0: due += ["index", "momentum"]
        for future in [self.callers.submit(self.run, lane) for lane in due]:
            future.result(timeout=30)
        assert not any(self.active.values()), "background batch left outstanding work"
        self.tick += 1
        self.cycles += 1

    def status(self):
        with self.lock:
            return dict(cycles=self.cycles, rounds=dict(self.rounds), calls=dict(self.calls),
                        max_active=dict(self.peak), thread_counts={k: len(v) for k,v in self.threads.items()},
                        active=sum(self.active.values()))

    def verify(self):
        out = self.status()
        assert self.cycles > 0 and set(self.rounds) == set(LIMITS)
        per_round = {"index": 20, "price-history": 2, "major-analysis": 3, "momentum": 20,
                     "sudden-pump": 8, "funding-binance": 8, "funding-bybit": 64, "funding-okx": 48}
        assert all(self.calls[lane] == count*self.rounds[lane] for lane,count in per_round.items())
        assert out["active"] == 0
        return dict(out, exact_values=True, network=False, database=False)

    def close(self):
        self.callers.shutdown(wait=True)
        for pool in self.ns["BACKGROUND_HTTP_EXECUTORS"].values():
            pool.shutdown(wait=True)

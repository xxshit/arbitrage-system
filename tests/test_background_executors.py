"""Run the real scheduling functions with synthetic I/O; never import the app."""
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from contextlib import contextmanager
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest


def load_scheduling_functions():
    path = Path(__file__).resolve().parents[1] / "app.py"
    selected = []
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in
            {"BACKGROUND_HTTP_EXECUTOR", "BACKGROUND_HTTP_EXECUTORS"}
            for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in {
            "background_http_batch", "run_funding_sync_batch",
            "fetch_major_market_overview", "scan_intraday_sudden_pumps",
        }:
            selected.append(node)
    namespace = dict(ThreadPoolExecutor=ThreadPoolExecutor, as_completed=as_completed,
                     wait=wait, contextmanager=contextmanager, time=time,
                     threading=threading, SimpleNamespace=SimpleNamespace)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class Schedule:
    def __init__(self):
        self.last_cleanup = time.time()
        self.completed = []

    def plan(self, targets, *args):
        return [(key, False) for key in targets]

    def done(self, key, target, now, latest, **kwargs):
        self.completed.append((key, latest, kwargs))

    def save(self, status):
        self.status = status


class BackgroundExecutorTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_scheduling_functions()
        self.ns.update(
            db=SimpleNamespace(session=SimpleNamespace(rollback=lambda: None)),
            funding_sync_catalog=lambda exchange: {
                str(i): {"candidates": [str(i)]} for i in range(64)},
            funding_sync_latest=lambda *args: {},
            store_settled_funding=lambda *args: 0,
            expected_settlement=lambda *args: 0,
            DAY_MS=86400000,
            FundingHistoryError=type("FundingHistoryError", (Exception,), {}),
            app=SimpleNamespace(logger=SimpleNamespace(warning=lambda *a: None)),
            MAJOR_MARKET_SYMBOLS=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            MAJOR_MARKET_CACHE={"items": [], "ts": 0},
            MAJOR_MARKET_CACHE_SECONDS=30,
            fetch_major_market_symbol=lambda raw: {
                "symbol": raw[:-4] + "/USDT", "change_24h": 1},
        )
        self.addCleanup(self.close_pools)

    def close_pools(self):
        pools = self.ns.get("BACKGROUND_HTTP_EXECUTORS", {})
        if "BACKGROUND_HTTP_EXECUTOR" in self.ns:
            pools = {"shared": self.ns["BACKGROUND_HTTP_EXECUTOR"]}
        for pool in pools.values():
            pool.shutdown(wait=True)

    def test_slow_funding_keeps_four_worker_limit_and_does_not_block_analysis(self):
        release, entered = threading.Event(), threading.Condition()
        running = 0
        peak = 0

        def fetch(venue, candidates, start, end):
            nonlocal running, peak
            with entered:
                running += 1
                peak = max(peak, running)
                entered.notify_all()
            self.assertTrue(release.wait(5))
            with entered:
                running -= 1
            return candidates[0], []

        self.ns["FUNDING_HISTORY_CLIENT"] = SimpleNamespace(fetch=fetch)
        schedule = Schedule()
        with ThreadPoolExecutor(max_workers=2) as callers:
            funding = callers.submit(self.ns["run_funding_sync_batch"], "Bybit", schedule)
            try:
                with entered:
                    self.assertTrue(entered.wait_for(lambda: running >= 4, timeout=2))
                analysis = callers.submit(self.ns["fetch_major_market_overview"], True)
                done, _ = wait([analysis], timeout=1)
                self.assertTrue(done, "slow funding blocked independent analysis")
                self.assertLessEqual(peak, 4, "one exchange exceeded its original four workers")
                self.assertEqual([x["symbol"] for x in analysis.result()],
                                 ["BTC/USDT", "ETH/USDT", "SOL/USDT"])
            finally:
                release.set()
            self.assertEqual(funding.result(timeout=3)["attempted"], 64)
        self.assertEqual(len(schedule.completed), 64)

    def test_slow_exchange_does_not_block_another_exchange(self):
        blocked, release = threading.Event(), threading.Event()

        def fetch(venue, candidates, start, end):
            if venue == "Bybit":
                blocked.set()
                self.assertTrue(release.wait(5))
            return candidates[0], []

        self.ns["FUNDING_HISTORY_CLIENT"] = SimpleNamespace(fetch=fetch)
        with ThreadPoolExecutor(max_workers=2) as callers:
            bybit = callers.submit(self.ns["run_funding_sync_batch"], "Bybit", Schedule())
            try:
                self.assertTrue(blocked.wait(2))
                okx = callers.submit(self.ns["run_funding_sync_batch"], "OKX", Schedule())
                done, _ = wait([okx], timeout=1)
                self.assertTrue(done, "an exchange's backfill blocked another exchange")
                self.assertEqual(okx.result()["attempted"], 64)
            finally:
                release.set()
            bybit.result(timeout=3)

    def test_funding_reuses_the_same_worker_objects_between_batches(self):
        self.ns["funding_sync_catalog"] = lambda exchange: {
            str(i): {"candidates": [str(i)]} for i in range(4)}
        barrier = threading.Barrier(4)
        observed = []

        def fetch(venue, candidates, start, end):
            observed.append(threading.current_thread())
            barrier.wait(timeout=2)
            return candidates[0], []

        self.ns["FUNDING_HISTORY_CLIENT"] = SimpleNamespace(fetch=fetch)
        for _ in range(3):
            self.ns["run_funding_sync_batch"]("Binance", Schedule())
        first = set(observed[:4])
        self.assertEqual(len(first), 4)
        self.assertEqual(first, set(observed[4:8]))
        self.assertEqual(first, set(observed[8:12]))

    def test_failed_scan_drains_siblings_before_releasing_scan_lock(self):
        blocked, failed, release = threading.Event(), threading.Event(), threading.Event()
        scan_lock = threading.Lock()

        def fetch(symbol, threshold):
            if symbol == "BAD":
                self.assertTrue(blocked.wait(2))
                failed.set()
                raise ValueError("synthetic failure")
            blocked.set()
            self.assertTrue(release.wait(5))
            return None

        self.ns.update(
            SUDDEN_PUMP_SCAN_LOCK=scan_lock, SUDDEN_PUMP_FAST_SCAN_UNTIL=0,
            SUDDEN_PUMP_FAST_SCAN_HOLD_SECONDS=30, SUDDEN_PUMP_CONFIRMATIONS={},
            load_latest_market_snapshot=lambda: {"symbols": []},
            runtime_number=lambda *args: 15,
            sudden_pump_rough_candidates=lambda *args: ["BAD", "WAIT"],
            fetch_sudden_pump_context=fetch,
        )
        with ThreadPoolExecutor(max_workers=1) as caller:
            scan = caller.submit(self.ns["scan_intraday_sudden_pumps"])
            try:
                self.assertTrue(failed.wait(2))
                done, _ = wait([scan], timeout=.1)
                self.assertFalse(done, "scan returned while old requests were still active")
                self.assertTrue(scan_lock.locked())
            finally:
                release.set()
            with self.assertRaisesRegex(ValueError, "synthetic failure"):
                scan.result(timeout=2)
        self.assertFalse(scan_lock.locked())


if __name__ == "__main__":
    unittest.main()

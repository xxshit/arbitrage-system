"""Bounded, non-secret runtime diagnostics and recovery for daemon workers.

Only restart a target after it has actually exited. A slow/live worker is never
duplicated, and shutdown signals (BaseException) are deliberately not swallowed.
"""
from functools import wraps
from pathlib import Path
import ctypes
import gc
import os
import sys
import threading
import time


class WorkerRuntime:
    def __init__(self):
        self.lock = threading.Lock()
        self.workers = {}
        self.connections = {}
        self.longest_checkout = 0

    def update(self, **values):
        name = threading.current_thread().name
        with self.lock:
            if name in self.workers:
                self.workers[name].update(values)

    def progress(self, state):
        now = time.time()
        self.update(last_progress_at=now, phase=state,
                    **({"last_success_at": now} if state == "success" else {}))

    def guard(self, target, logger):
        @wraps(target)
        def run(*args, **kwargs):
            name = threading.current_thread().name
            delay = 5
            with self.lock:
                self.workers[name] = dict(started_at=time.time(), restarts=0,
                                          phase="starting", last_success_at=None)
            while True:
                started = time.monotonic()
                self.update(phase="running", retry_at=None)
                try:
                    target(*args, **kwargs)
                    reason = "unexpected_return"
                except Exception as error:
                    # DB errors may contain SQL/parameters/URLs: never log them.
                    reason = type(error).__name__
                if time.monotonic() - started >= 300:
                    delay = 5
                with self.lock:
                    state = self.workers[name]
                    state.update(restarts=state["restarts"]+1, phase="retrying",
                                 error_type=reason, failed_at=time.time(), retry_at=time.time()+delay)
                logger.warning("background_worker_retry worker=%s reason=%s delay=%s", name, reason, delay)
                time.sleep(delay)
                delay = min(60, delay * 2)
        return run

    def checkout(self, connection, record, proxy):
        with self.lock:
            self.connections[id(record)] = (time.monotonic(), threading.current_thread().name)

    def checkin(self, connection, record):
        with self.lock:
            entry = self.connections.pop(id(record), None)
            if entry:
                self.longest_checkout = max(self.longest_checkout, time.monotonic()-entry[0])

    def snapshot(self):
        now = time.monotonic()
        alive = {thread.name for thread in threading.enumerate() if thread.is_alive()}
        with self.lock:
            return dict(workers={name: {**state, "alive": name in alive} for name, state in self.workers.items()},
                        db_checked_out=len(self.connections),
                        db_longest_checkout_seconds=round(self.longest_checkout, 2),
                        db_holders=[dict(worker=name, seconds=round(now-at, 2))
                                    for at, name in self.connections.values()])


def mysql_engine_options(uri):
    if not uri.startswith(("mysql", "mariadb")):
        return {}
    # Retain the original 15-connection ceiling; fix holders, not unlimited pools.
    return dict(pool_size=5, max_overflow=10, pool_timeout=5, pool_recycle=1800,
                pool_pre_ping=True, hide_parameters=True,
                connect_args=dict(connect_timeout=5, read_timeout=30, write_timeout=30))


def cgroup_memory_limits(root):
    """Service-local limits, in bytes; unlimited/unreadable is not zero free."""
    limits = {}
    for filename, key in (("memory.max", "memory_limit_bytes"),
                          ("memory.high", "memory_high_bytes")):
        try:
            value = int((root / filename).read_text().strip())
            limits[key] = value if value > 0 else None
        except (OSError, ValueError):
            limits[key] = None
    return limits


_THREAD_ROLES = frozenset({
    "MainThread", "runtime-health", "alert-pulse-writer", "alert-history-reader",
    "alert-stream-io", "dashboard-quotes", "dashboard-major-analysis",
    "dashboard-momentum", "dashboard-oi-market-cap", "spot-market-refresh",
    "spot-spread-history", "dual-market-refresh", "funding-history-binance",
    "funding-history-bybit", "funding-history-okx", "binance-settlement-tail",
    "funding-alerts", "price-history-backfill", "long-trend-references",
    "index-component-sync", "onchain-opportunity-refresh", "announcement-scan",
    "daily-horn-scan", "intraday-sudden-pump-scan", "intraday-early-trend-scan",
    "transfer-network-sync", "thought-analysis-push", "hei-risk-watch",
    "trend-horizon-validation", "push-signal-validation", "chat-retention-cleanup",
    "turnover-basis-watch", "quote-confirm-bn_spot", "quote-confirm-bn_future",
    "quote-confirm-gate", "quote-confirm-bybit",
})
_POOL_ROLES = frozenset({
    "market-http", "equity-metadata", "dashboard-quotes", "dashboard-http",
    "chat-web-push", "background-funding-binance", "background-funding-bybit",
    "background-funding-okx", "background-index", "background-price-history",
    "background-major-analysis", "background-momentum", "background-sudden-pump",
    "asyncio",
})


def thread_memory_counters():
    """Bounded kernel counters and fixed role labels, never frames or values.

    Read only this process's /proc task metadata. TID + start ticks bind a
    sample across thread reuse; minor faults identify first touches, not malloc
    ownership. No thread objects, per-sample history or unknown names retained.
    """
    rows, skipped = [], 0
    threads = threading.enumerate()
    for thread in threads[:256]:
        tid = thread.native_id
        if not isinstance(tid, int) or tid <= 0:
            skipped += 1
            continue
        name = thread.name
        prefix, _, suffix = name.rpartition("_")
        role = name if name in _THREAD_ROLES else (
            prefix if prefix in _POOL_ROLES and suffix.isdecimal() else "other")
        try:
            fields = Path(f"/proc/self/task/{tid}/stat").read_text().rsplit(")", 1)[1].split()
            rows.append(dict(tid=tid, start_ticks=int(fields[19]), role=role,
                             minor_faults=int(fields[7]),
                             cpu_ticks=int(fields[11])+int(fields[12])))
        except (OSError, ValueError, IndexError):
            skipped += 1  # exited threads and unsupported platforms are normal
    return dict(rows=rows, enumerated=len(threads), skipped=skipped,
                truncated=max(0, len(threads)-256))


def memory_status():
    result = {"pid": os.getpid()}
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        result["rss_bytes"] = pages * os.sysconf("SC_PAGE_SIZE")
        result["thread_counters"] = thread_memory_counters()
        result["python_allocated_blocks"] = sys.getallocatedblocks()
        result["gc_counts"] = gc.get_count()
        result["gc_collections"] = [generation["collections"] for generation in gc.get_stats()]
        relative = next(line.split("::", 1)[1] for line in Path("/proc/self/cgroup").read_text().splitlines()
                        if line.startswith("0::"))
        root = Path("/sys/fs/cgroup") / relative.lstrip("/")
        result["cgroup_bytes"] = int((root / "memory.current").read_text())
        result.update(cgroup_memory_limits(root))
        result["memory_sampled_at_epoch"] = time.time()
        result["memory_events"] = {k: int(v) for k, v in
                                   (line.split() for line in (root / "memory.events").read_text().splitlines())}
        stats = dict(line.split() for line in (root / "memory.stat").read_text().splitlines())
        result["anonymous_bytes"] = int(stats["anon"])
        result["file_cache_bytes"] = int(stats["file"])
        for line in (root / "memory.pressure").read_text().splitlines():
            if line.startswith("some "):
                result["memory_stall_microseconds"] = int(dict(p.split("=") for p in line.split()[1:])["total"])
        # GNU's read-only aggregate allocator statistics. No heap contents,
        # pointer addresses, process injection, trimming or tuning here.
        class MallInfo(ctypes.Structure):
            _fields_ = [(name, ctypes.c_size_t) for name in (
                "arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks", "fsmblks", "uordblks", "fordblks", "keepcost")]
        try:
            read_info = ctypes.CDLL(None).mallinfo2
            read_info.argtypes = []
            read_info.restype = MallInfo
            info = read_info()
            result["allocator_used_bytes"] = info.uordblks
            result["allocator_free_bytes"] = info.fordblks
            result["allocator_mmap_bytes"] = info.hblkhd
            result["allocator_arena_bytes"] = info.arena
        except (AttributeError, OSError):
            pass
    except (OSError, ValueError, IndexError, KeyError, StopIteration):
        pass  # macOS/tests/non-cgroup hosts still expose a PID.
    return result

"""Bounded, non-secret runtime diagnostics and recovery for daemon workers.

Only restart a target after it has actually exited. A slow/live worker is never
duplicated, and shutdown signals (BaseException) are deliberately not swallowed.
"""
from functools import wraps
from pathlib import Path
import os
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


def memory_status():
    result = {"pid": os.getpid()}
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        result["rss_bytes"] = pages * os.sysconf("SC_PAGE_SIZE")
        relative = next(line.split("::", 1)[1] for line in Path("/proc/self/cgroup").read_text().splitlines()
                        if line.startswith("0::"))
        root = Path("/sys/fs/cgroup") / relative.lstrip("/")
        result["cgroup_bytes"] = int((root / "memory.current").read_text())
        result["memory_events"] = {k: int(v) for k, v in
                                   (line.split() for line in (root / "memory.events").read_text().splitlines())}
    except (OSError, ValueError, IndexError, StopIteration):
        pass  # macOS/tests/non-cgroup hosts still expose a PID.
    return result

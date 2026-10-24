"""Offline variable-shape write soak; own SQLite or guarded disposable MySQL.

Never imports app or reads production settings. MySQL requires the independently
created --skip-networking, server-id=83024 instance under /tmp. Each run creates
a fresh synthetic schema; never reuses/deletes an existing schema or history.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, func, select, text
from sqlalchemy.orm import Session
from background_runtime import memory_status
from scripts.snapshot_write_workload import SnapshotWriteWorkload, synthetic_tables


def isolated_mysql_url(socket_path):
    path = Path(socket_path)
    assert path.parent.parent == Path("/tmp") and path.parent.name.startswith("arbiscope-read-mysql.")
    assert path.name == "mysql.sock" and path.is_socket()
    base = f"mysql+pymysql://root@localhost/{{}}?unix_socket={path}"
    engine = create_engine(base.format(""))
    try:
        with engine.connect() as conn:
            actual = conn.execute(text("SELECT @@datadir, @@skip_networking, @@server_id")).one()
            assert Path(actual[0]) == path.parent / "data" and actual[1:] == (1, 83024)
            name = "snapshot_soak_" + uuid.uuid4().hex
            conn.execute(text("CREATE DATABASE " + name))
        return base.format(name)
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=7200)
    parser.add_argument("--mysql-socket")
    args = parser.parse_args()
    assert args.seconds > 0
    # Defense in depth: no TCP even if an unexpected dependency tries to dial.
    real_socket = socket.socket
    class UnixOnlySocket(real_socket):
        def __init__(self, family=socket.AF_INET, *values, **kwargs):
            if family != socket.AF_UNIX:
                raise AssertionError("synthetic soak permits Unix sockets only")
            super().__init__(family, *values, **kwargs)
    socket.socket = UnixOnlySocket
    files = {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in (
        "snapshot_persistence.py", "scripts/snapshot_write_workload.py", "scripts/soak_snapshot_writes.py")}
    def report(**data):
        print(json.dumps(dict(at_utc=datetime.now(timezone.utc).isoformat(), **data), sort_keys=True), flush=True)
    with ExitStack() as cleanup:
        if args.mysql_socket:
            url = isolated_mysql_url(args.mysql_socket)
        else:
            directory = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="arbiscope-snapshot-soak-"))
            url = "sqlite:///" + directory + "/synthetic.sqlite3"
        engine = create_engine(url, pool_size=5, max_overflow=10)
        cleanup.callback(engine.dispose)
        metadata = MetaData()
        quotes, equity = synthetic_tables(metadata)
        guard = Table("synthetic_history_guard", metadata, Column("id", Integer, primary_key=True, autoincrement=False))
        metadata.create_all(engine)
        with Session(engine) as session:
            workload = SnapshotWriteWorkload(session, quotes, equity)
            session.execute(guard.insert(), [dict(id=i) for i in range(50)])
            session.commit()
        def read_counts():
            with engine.connect() as conn:
                assert conn.scalar(select(func.count()).select_from(guard)) == 50
                for table in quotes:
                    assert conn.scalar(select(func.count()).select_from(table)) == 100
        started, last_report = time.monotonic(), 0
        durations, rss_samples = [], []
        max_connections = 0
        report(event="ready", pid=os.getpid(), dialect=engine.dialect.name, files=files)
        with ThreadPoolExecutor(max_workers=2) as readers, Session(engine) as session:
            while time.monotonic() - started < args.seconds:
                cycle = time.monotonic()
                futures = [readers.submit(read_counts) for _ in range(2)]
                workload.step(session)
                session.commit()
                max_connections = max(max_connections, engine.pool.checkedout())
                for future in futures:
                    future.result(timeout=10)
                assert engine.pool.checkedout() == 0
                # Inserts and the fixed SELECT shapes are reusable; variable
                # CASE/JSON shapes must never fill the engine cache.
                assert len(engine._compiled_cache) < 30
                durations.append(time.monotonic() - cycle)
                elapsed = time.monotonic() - started
                if elapsed - last_report >= 60 or workload.cycles == 1:
                    memory = memory_status()
                    rss_samples.append(memory["rss_bytes"])
                    report(event="progress", seconds=round(elapsed, 3), cycles=workload.cycles,
                           row_updates=workload.row_updates, cache_entries=len(engine._compiled_cache),
                           checked_out=engine.pool.checkedout(), memory=memory,
                           cycle_last_seconds=round(durations[-1], 4), cycle_max_seconds=round(max(durations), 4))
                    last_report = elapsed
                time.sleep(max(0, 5 - (time.monotonic() - cycle)))
            checked = workload.verify(session)
            assert list(session.scalars(select(guard.c.id).order_by(guard.c.id))) == list(range(50))
            session.rollback()
        with engine.connect() as conn:
            if engine.dialect.name == "sqlite":
                assert conn.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
            else:
                for table in metadata.sorted_tables:
                    result = conn.execute(text("CHECK TABLE " + table.name)).all()
                    assert result and result[-1][-2:] == ("status", "OK"), table.name
        assert engine.pool.checkedout() == 0
        assert all(hashlib.sha256((REPO / name).read_bytes()).hexdigest() == digest for name, digest in files.items())
        report(event="passed", seconds=round(time.monotonic() - started, 3), files=files,
               values=checked, history_unchanged=True, integrity="ok", checked_out=0,
               cache_entries=len(engine._compiled_cache), max_sample_connections=max_connections,
               rss_first=rss_samples[0], rss_last=rss_samples[-1], rss_max=max(rss_samples),
               cycle_p95_seconds=round(sorted(durations)[int(.95 * (len(durations) - 1))], 4),
               cycle_max_seconds=round(max(durations), 4))


if __name__ == "__main__":
    main()

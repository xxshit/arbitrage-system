"""Opt-in synthetic probe; ONLY an isolated --skip-networking MySQL instance.

Usage: python tests/mysql_snapshot_probe.py /tmp/arbiscope-mysql-probe.XXX/mysql.sock
Creates two synthetic schemas in that disposable instance. No production reads.
"""
import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from sqlalchemy import Column, DateTime, Float, Integer, JSON, MetaData, Table, create_engine, text, update
from sqlalchemy.orm import Session

from snapshot_persistence import changed_values, equity_payload_update, update_snapshots


def main(socket):
    socket = Path(socket)
    assert socket.name == "mysql.sock" and socket.parent.name.startswith("arbiscope-mysql-probe.")
    assert socket.parent.parent == Path("/tmp") and socket.is_socket()
    base = f"mysql+pymysql://root@localhost/{{}}?unix_socket={socket}"
    admin = create_engine(base.format(""))
    with admin.connect() as conn:
        actual = conn.execute(text("SELECT @@datadir, @@skip_networking, @@server_id")).one()
        assert Path(actual[0]) == socket.parent / "data" and actual[1:] == (1, 83021), actual
        for schema in ("snapshot_probe_source", "snapshot_probe_restore"):
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {schema}"))
    metadata = MetaData()
    spot = Table("latest_market_snapshot", metadata, Column("id", Integer, primary_key=True, autoincrement=False),
                 Column("price", Float), Column("rate", Float), Column("captured_at", DateTime))
    dual = Table("latest_dual_futures_snapshot", metadata, Column("id", Integer, primary_key=True, autoincrement=False),
                 Column("price", Float), Column("rate", Float), Column("captured_at", DateTime))
    equity = Table("equity_spot_snapshot", metadata, Column("id", Integer, primary_key=True),
                   Column("payload", JSON), Column("updated_at", DateTime))
    source = create_engine(base.format("snapshot_probe_source"))
    restore = create_engine(base.format("snapshot_probe_restore"))
    # Only the three synthetic tables, AFTER the isolated-instance guard.
    metadata.drop_all(source)
    metadata.drop_all(restore)
    metadata.create_all(source)
    metadata.create_all(restore)
    with admin.connect() as conn:
        start = conn.execute(text("SHOW MASTER STATUS")).one()[:2]
    at = datetime(2026,10,21)
    payload = dict(rows=[dict(id=i, price=1.01, rate=.005, evidence="official identity"*15) for i in range(300)],
                   errors={}, metadata="frozen issuer reference"*100)
    with Session(source) as session:
        session.execute(text("SET SESSION binlog_transaction_compression=ON"))
        session.commit()
        for table in (spot, dual):
            session.execute(table.insert(), [dict(id=i, price=i+.1, rate=.005, captured_at=at) for i in range(300)])
        session.execute(equity.insert(), dict(id=1, payload=payload, updated_at=at))
        session.commit()
        for tick in range(1, 9):
            for table in (spot, dual):
                updates = []
                for old in session.execute(table.select()).mappings():
                    current = dict(price=old["id"] + tick/100, rate=None if old["id"]%3==0 else .005,
                                   captured_at=at+timedelta(seconds=tick*15))
                    updates.append(dict(id=old["id"], **changed_values(table, SimpleNamespace(**old), current, "mysql")))
                update_snapshots(session, table, updates)
            previous = copy.deepcopy(payload)
            payload["rows"][tick]["price"] = tick/10
            payload["rows"][tick]["rate"] = None
            payload["errors"] = {'odd"key': True if tick%2 else 1}
            equity_payload_update(session, equity, previous, payload, at+timedelta(seconds=tick*15))
            session.commit()
            actual_payload = session.execute(equity.select()).mappings().one()["payload"]
            assert actual_payload == payload, dict(tick=tick, actual=actual_payload["rows"][tick], expected=payload["rows"][tick], errors=actual_payload.get("errors"))
        before = [dict(row) for row in session.execute(spot.select().order_by(spot.c.id)).mappings()]
        update_snapshots(session, spot, [dict(id=1, price=999)])
        session.rollback()
        assert [dict(row) for row in session.execute(spot.select().order_by(spot.c.id)).mappings()] == before
        # Dense list replacement and removal are also lossless.
        previous = copy.deepcopy(payload)
        payload["rows"].pop(0)
        del payload["errors"]
        equity_payload_update(session, equity, previous, payload, at+timedelta(seconds=200))
        session.commit()
        assert session.execute(equity.select()).mappings().one()["payload"] == payload
    with admin.connect() as conn:
        end = conn.execute(text("SHOW MASTER STATUS")).one()[:2]
        stats = [dict(row) for row in conn.execute(text("SELECT COMPRESSION_TYPE, TRANSACTION_COUNTER, COMPRESSED_BYTES_COUNTER, UNCOMPRESSED_BYTES_COUNTER FROM performance_schema.binary_log_transaction_compression_stats")).mappings()]
    assert start[0] == end[0]
    commands = ["mysqlbinlog", f"--start-position={start[1]}", f"--stop-position={end[1]}",
                "--rewrite-db=snapshot_probe_source->snapshot_probe_restore", str(socket.parent / "data" / start[0])]
    # Data stays in a server-local pipe. Replay through MySQL, not a mock decoder.
    reader = subprocess.Popen(commands, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    writer = subprocess.run(["mysql", "--protocol=socket", f"--socket={socket}", "-u", "root"], stdin=reader.stdout,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    reader.stdout.close()
    reader_error = reader.stderr.read()
    assert reader.wait() == 0, reader_error.decode()
    assert writer.returncode == 0, writer.stderr.decode()
    with source.connect() as left, restore.connect() as right:
        for table in metadata.sorted_tables:
            original = left.execute(table.select().order_by(table.c.id)).all()
            replayed = right.execute(table.select().order_by(table.c.id)).all()
            assert original == replayed, table.name
    assert any(row["COMPRESSION_TYPE"] == "ZSTD" and row["TRANSACTION_COUNTER"] >= 10 for row in stats)
    print(json.dumps(dict(result="PASS", replayed_tables=3, quote_rows=600, equity_rows=299,
                          rollback="verified", bytes=end[1]-start[1], compression=stats), default=str))
    source.dispose()
    restore.dispose()
    admin.dispose()


if __name__ == "__main__":
    main(sys.argv[1])

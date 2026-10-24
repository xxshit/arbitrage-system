"""Synthetic probe in a guarded, socket-only disposable MySQL instance.

Usage: python tests/mysql_read_probe.py /tmp/arbiscope-read-mysql.XXX/mysql.sock
Never imports the production app's schema/seed initialization or reads accounts.
"""
import ast
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

from sqlalchemy import create_engine, text


def main(socket):
    socket = Path(socket)
    assert socket.parent.parent == Path("/tmp") and socket.parent.name.startswith("arbiscope-read-mysql.")
    assert socket.name == "mysql.sock" and socket.is_socket()
    base = f"mysql+pymysql://root@localhost/{{}}?unix_socket={socket}"
    admin = create_engine(base.format(""))
    with admin.connect() as conn:
        location = conn.execute(text("SELECT @@datadir, @@skip_networking, @@server_id")).one()
        assert Path(location[0]) == socket.parent / "data" and location[1:] == (1, 83024), location
        conn.execute(text("CREATE DATABASE IF NOT EXISTS public_read_probe"))
    os.environ.update(DATABASE_URL=base.format("public_read_probe"), SECRET_KEY="synthetic-read-probe",
                      CHAT_ENCRYPTION_KEY="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    # Keep model/engine construction but skip the known top-level bootstrap.
    tree = ast.parse((repo/"app.py").read_text())
    skipped = [node for node in tree.body if isinstance(node, ast.With) and any(isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "create_database_schema" for child in ast.walk(node))]
    assert len(skipped) == 1
    tree.body = [node for node in tree.body if node not in skipped]
    import types
    module = types.ModuleType("read_probe_app")
    module.__file__ = str(repo/"app.py")
    sys.modules[module.__name__] = module
    exec(compile(tree, str(repo/"app.py"), "exec"), module.__dict__)
    from read_performance import install_read_indexes
    with module.app.app_context():
        assert module.db.engine.url.database == "public_read_probe"
        # This disposable schema is wholly synthetic; repeat failed probes
        # without modifying the separate production instance.
        module.db.drop_all()
        for index in module.READ_PERFORMANCE_INDEXES:
            index.table.indexes.remove(index)
        module.db.create_all()
        now = datetime.now()
        for first in range(0, 50000, 500):
            module.db.session.execute(module.BasisTracking.__table__.insert(), [dict(id=i+1, symbol=f"COIN{i%500}/USDT", strategy="spot_futures", direction="positive", started_at=now-timedelta(hours=i//500), opening_basis=1, max_basis=2, max_abs_basis=2, max_at=now, resolved_at=None if i < 20 else now) for i in range(first, first+500)])
            module.db.session.commit()
        module.store_transfer_networks("Gate", {f"COIN{i}/USDT": [{"name": "ETH", "deposit_open": True, "withdraw_open": True}] for i in range(10000)})
        module.db.session.remove()
        queries = {
            "active_basis": "SELECT id FROM basis_tracking WHERE resolved_at IS NULL ORDER BY max_abs_basis DESC LIMIT 50",
            "wallet_pair": "SELECT id FROM transfer_network_snapshot WHERE exchange='Gate' AND UPPER(symbol) IN ('COIN1/USDT','COIN2/USDT')",
        }
        def plans():
            with module.db.engine.connect() as conn:
                return {key: [dict(row) for row in conn.execute(text("EXPLAIN " + sql)).mappings()] for key, sql in queries.items()}
        before = plans()
        with module.db.engine.begin() as conn:
            added = install_read_indexes(conn, module.db.metadata)
        with module.db.engine.begin() as conn:
            assert install_read_indexes(conn, module.db.metadata) == []
        from read_performance import wallet_symbol_case_insensitive
        assert wallet_symbol_case_insensitive(module.db.engine)
        queries["wallet_pair"] = "SELECT id FROM transfer_network_snapshot WHERE exchange='Gate' AND symbol IN ('COIN1/USDT','COIN2/USDT')"
        after = plans()
        assert after["active_basis"][0]["key"] == "ix_basis_active_peak", after
        assert after["wallet_pair"][0]["key"] == "uq_transfer_network_exchange_symbol" and after["wallet_pair"][0]["rows"] <= 2, after
        groups = [{"symbol": f"COIN{i}/USDT"} for i in range(500)]
        began = time.monotonic()
        module.enrich_basis_openings(groups, "spot_futures")
        latest_seconds = time.monotonic()-began
        assert all(group["basis_opening"]["opened_basis"] == 1 for group in groups)
        module.db.session.remove()
        for action in (lambda: module.runtime_interval("missing", 5), module.symbol_alias_rows,
                       module.active_thought_symbols, lambda: module.thought_watch_config("SYNTHETIC/USDT")):
            action()
            assert module.BACKGROUND_RUNTIME.snapshot()["db_checked_out"] == 0
        statements = []
        def record(conn, cursor, sql, parameters, context, many):
            if sql.startswith("UPDATE"):
                statements.append(sql)
        module.sa_event.listen(module.db.engine, "before_cursor_execute", record)
        began = time.monotonic()
        module.store_transfer_networks("Gate", {f"COIN{i}/USDT": [{"name": "ETH", "deposit_open": True, "withdraw_open": True}] for i in range(10000)})
        wallet_seconds = time.monotonic()-began
        module.sa_event.remove(module.db.engine, "before_cursor_execute", record)
        assert len(statements) == 40 and all("chains_json" not in sql for sql in statements)
        assert module.BACKGROUND_RUNTIME.snapshot()["db_checked_out"] == 0
        assert module.BasisTracking.query.count() == 50000
        assert module.TransferNetworkSnapshot.query.count() == 10000
        assert module.UserAccount.query.count() == module.HedgePosition.query.count() == 0
        module.db.session.remove()
        print(json.dumps(dict(passed=True, indexes=added, before=before, after=after, latest_500_seconds=round(latest_seconds,3), wallet_10000_seconds=round(wallet_seconds,3), heartbeat_statements=len(statements), db=module.BACKGROUND_RUNTIME.snapshot()), default=str))


if __name__ == "__main__":
    main(sys.argv[1])

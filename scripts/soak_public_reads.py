"""Offline sustained read/write test in its own synthetic database.

No credentials, production database, upstream requests, push or trading workers.
Run from repo: .venv/bin/python scripts/soak_public_reads.py --seconds 7200
Default SQLite is temporary. --mysql-socket requires the guarded socket-only
disposable MySQL instance and creates a fresh schema; never a production URL.
The JSONL report is stdout; the runner is responsible for durable report storage.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time


def synthetic_database_url(directory, mysql_socket=None):
    if mysql_socket is not None:
        from scripts.soak_snapshot_writes import isolated_mysql_url
        return isolated_mysql_url(mysql_socket)
    return "sqlite:///" + str(directory) + "/synthetic.sqlite3"


def check_integrity(session, metadata, dialect):
    from sqlalchemy import text
    if dialect == "sqlite":
        assert session.execute(text("PRAGMA integrity_check")).scalar() == "ok"
    else:
        assert dialect == "mysql"
        for table in metadata.sorted_tables:
            rows = session.execute(text("CHECK TABLE `" + table.name + "`")).all()
            assert rows and rows[-1][-2:] == ("status", "OK"), table.name


@contextmanager
def background_task(target, stop, *, name, join_timeout=30):
    """Stop the synthetic worker even when a request/assertion fails.

    The daemon flag is only a last exit bound for this disposable test process;
    normal/error teardown still requests a stop and joins before DB cleanup.
    It does not change the application's worker lifecycle.
    """
    worker = threading.Thread(target=target, name=name, daemon=True)
    worker.start()
    try:
        yield worker
    finally:
        stop.set()
        worker.join(join_timeout)
        if worker.is_alive():
            raise RuntimeError("synthetic_history_worker_did_not_stop")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=7200)
    parser.add_argument("--rows", type=int, default=100000)
    parser.add_argument("--mysql-socket")
    parser.add_argument("--exercise-background", action="store_true")
    parser.add_argument("--accept-gzip", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    os.environ["TZ"] = "UTC"
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    time.tzset()
    with tempfile.TemporaryDirectory(prefix="arbiscope-public-soak-") as directory, ExitStack() as cleanup:
        database_url = synthetic_database_url(directory, args.mysql_socket)
        os.environ.update(DATABASE_URL=database_url, SECRET_KEY="synthetic-soak-only",
                          CHAT_ENCRYPTION_KEY="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
        import app as module
        from tests.test_alert_quality import historical_pulse
        from tests.test_alert_screening import quotes, reference
        from scripts.snapshot_write_workload import SnapshotWriteWorkload
        from scripts.maintenance_workload import MaintenanceWorkload
        from scripts.public_decode_workload import PublicDecodeWorkload
        from scripts.background_executor_workload import BackgroundExecutorWorkload
        from scripts.thought_lock_workload import ThoughtLockWorkload
        from sqlalchemy import text
        def no_network(*args, **kwargs):
            raise AssertionError("offline soak cannot call upstream")
        module.get_json = module.get_onchain_json = no_network
        module.app.config.update(TESTING=True)
        module.is_rwa_stock_pair = lambda symbol: False
        symbols = [f"SOAK{i}/USDT" for i in range(500)]
        now = datetime.now()
        def evidence(identity, at, live=False):
            item = historical_pulse(trigger=1.5, peak=1.5, identifier=f"soak{identity}")
            item["path"] = ["spot_futures", symbols[identity % len(symbols)], "Gate", "Binance"]
            for key in ("baseline_at", "triggered_at", "peak_at", "current_at", "noticed_at", "retrace_at"):
                item[key] += at - 1003
            item["screen_reference"] = {**reference(), "to_at": at-20}
            item["quality"] = {"synthetic_quote_evidence": "public quote provenance; " * 100}
            item["reminder"] = {"policy": 1, "notices": [{"key": f"soak:{identity}:{at}", "at": at, "value": 1.5, "kind": "breakout"}] if live else []}
            return json.dumps(item)
        with module.app.app_context():
            # LIFO: the history task is joined before its engine and temp DB go
            # away, including the request timeout / failed assertion paths.
            cleanup.callback(module.db.engine.dispose)
            if module.db.engine.dialect.name == "sqlite":
                with module.db.engine.connect() as conn:
                    journal_mode = os.environ.get("SOAK_SQLITE_JOURNAL_MODE", "WAL").upper()
                    assert journal_mode in {"WAL", "DELETE", "TRUNCATE", "MEMORY"}
                    conn.execute(text("PRAGMA journal_mode=" + journal_mode))
            else:
                assert module.db.engine.url.database.startswith("snapshot_soak_")
                assert module.db.engine.url.query.get("unix_socket") == args.mysql_socket
            module.db.session.add(module.UserAccount(username="synthetic-soak", password_hash="unused", role="admin", account_level="lv3", active_session_hash=module.session_digest("synthetic-token")))
            module.db.session.commit()
            user_id = module.UserAccount.query.filter_by(username="synthetic-soak").one().id
            for first in range(1, args.rows+1, 500):
                events, pulses = [], []
                for identity in range(first, min(first+500, args.rows+1)):
                    stamp = now-timedelta(seconds=3600+identity%36000)
                    events.append(dict(id=identity, symbol=symbols[identity%500], strategy="spot_futures", alert_type="rapid_spread", message="SYNTHETIC public pulse", open_spread=1.5, close_spread=1.52, basis=.2, created_at=stamp, long_exchange="Gate", short_exchange="Binance"))
                    pulses.append(dict(id=f"soak{identity}", alert_event_id=identity, evidence_json=evidence(identity, stamp.replace(tzinfo=timezone.utc).timestamp()), updated_at=stamp))
                module.db.session.execute(module.AlertEvent.__table__.insert(), events)
                module.db.session.execute(module.AlertPulse.__table__.insert(), pulses)
                module.db.session.commit()
            wallets = {symbol: [{"name": "ETH", "deposit_open": True, "withdraw_open": True}] for symbol in symbols}
            module.store_transfer_networks("Gate", wallets)
            module.store_transfer_networks("Binance", wallets)
            for index, symbol in enumerate(symbols):
                for age in range(100):
                    module.db.session.add(module.BasisTracking(symbol=symbol, strategy="spot_futures", direction="positive", started_at=now-timedelta(hours=age), opening_basis=1, max_basis=2, max_abs_basis=2, max_at=now, resolved_at=now if age else None))
                if index%50 == 0:
                    module.db.session.commit()
            module.db.session.commit()
            snapshot_load = SnapshotWriteWorkload(module.db.session,
                (module.LatestMarketSnapshot.__table__, module.LatestDualFuturesSnapshot.__table__),
                module.EquitySpotSnapshot.__table__)
            module.db.session.commit()
            maintenance_load = MaintenanceWorkload(module)
        decode_load = PublicDecodeWorkload()
        lock_load = ThoughtLockWorkload(repo)
        cleanup.callback(lock_load.close)
        background_load = BackgroundExecutorWorkload(repo) if args.exercise_background else None
        if background_load:
            cleanup.callback(background_load.close)
        fingerprints = {name: hashlib.sha256((repo/name).read_bytes()).hexdigest() for name in (
            "app.py", "alert_history_index.py", "alert_read_payload.py", "read_performance.py", "static/app.js",
            "snapshot_persistence.py", "market_transport.py", "scripts/snapshot_write_workload.py", "scripts/soak_public_reads.py",
            "scripts/soak_snapshot_writes.py", "background_runtime.py", "scripts/maintenance_workload.py",
            "scripts/public_decode_workload.py", "scripts/background_executor_workload.py",
            "scripts/thought_lock_workload.py")}
        print(json.dumps(dict(event="ready", at_utc=datetime.now(timezone.utc).isoformat(), rows=args.rows, pid=os.getpid(), files=fingerprints,
                             exercise_background=args.exercise_background, accept_gzip=args.accept_gzip)), flush=True)
        stop, background_errors = threading.Event(), []
        def refresh_history():
            while not stop.is_set():
                began = time.monotonic()
                try:
                    with module.app.app_context():
                        stamp = datetime.now()
                        module.read_alert_history(stamp-timedelta(hours=24), stamp, refresh_only=True)
                except Exception as error:
                    background_errors.append(type(error).__name__)
                    stop.set()
                stop.wait(max(.25, 5-(time.monotonic()-began)))
        module.BACKGROUND_WORKERS_STARTED = True
        history_thread = cleanup.enter_context(background_task(refresh_history, stop, name="soak-history"))
        measurements = {key: [] for key in ("summary", "detail", "active", "spot", "dual")}
        compressed_responses = {key: 0 for key in measurements}
        compression_lock = threading.Lock()
        paths = dict(summary="/api/alerts?view=summary", detail="/api/alerts?view=detail&symbol=SOAK1/USDT&page=2", active="/api/alerts?view=active", spot="/api/spot-futures", dual="/api/dual-futures")
        def request(key):
            began = time.monotonic()
            client = module.app.test_client()
            with client.session_transaction() as session:
                session.update(user_id=user_id, auth_token="synthetic-token")
            response = client.get(paths[key], headers={"Accept-Encoding": "gzip"} if args.accept_gzip else {})
            assert response.status_code == 200, (key, response.status_code, response.get_data(as_text=True)[:150])
            raw = response.get_data()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
                with compression_lock:
                    compressed_responses[key] += 1
            data = json.loads(raw)
            if key == "summary" and not data.get("history_loading"):
                assert data["total_observations"] == args.rows
            if key == "detail":
                assert len(data["items"]) == 40
            if key in ("spot", "dual"):
                assert data["captured_at_epoch"] == quote_epoch
            return key, time.monotonic()-began
        def update_quotes(tick):
            nonlocal quote_epoch
            quote_epoch = time.time()
            spot, dual = [], []
            for symbol in symbols:
                base = dict(long_exchange="Gate", short_exchange="Binance", long_ask=100, long_bid=99.99, short_bid=101.5, short_ask=101.52, open_spread=1.5, close_spread=1.53, captured_at_epoch=quote_epoch)
                spot.append(dict(symbol=symbol, rows=[dict(base, basis=.2, funding_rate=.01, funding_interval_hours=4, next_funding_time=int((quote_epoch+3600)*1000), spot_volume=1000000, futures_volume=2000000, futures_open_interest=1000000)]))
                dual.append(dict(symbol=symbol, rows=[dict(base, long_basis=.1, short_basis=.2, long_index=100, short_index=100, long_volume=1000000, short_volume=2000000, long_open_interest=1000000, short_open_interest=1000000, funding_difference=.005, long_funding_rate=.005, short_funding_rate=.01, long_funding_interval_hours=4, short_funding_interval_hours=4, long_next_funding_time=int((quote_epoch+3600)*1000), short_next_funding_time=int((quote_epoch+3600)*1000))]))
            for cache, groups in ((module.SPOT_FUTURES_CACHE, spot), (module.DUAL_FUTURES_CACHE, dual)):
                cache["snapshot"] = dict(symbols=groups, errors={}, updated_at=str(tick), captured_at_epoch=quote_epoch, next_refresh_in_seconds=5)
            module.ALERT_STREAM_HUB.screening_quotes = lambda paths, at: {path: quotes(long_at=at, short_at=at, metadata_at=at) for path in paths}
        quote_epoch = 0
        # Verify cold summary returns live reminders instead of waiting for the
        # full read. Detail UI cannot select a coin until the summary is ready.
        update_quotes(0)
        request("summary")
        warm_started = time.monotonic()
        while not module.ALERT_HISTORY_INDEX.ready():
            assert not background_errors, background_errors
            assert time.monotonic()-warm_started < 60
            time.sleep(.1)
        print(json.dumps(dict(event="warm", at_utc=datetime.now(timezone.utc).isoformat(), seconds=round(time.monotonic()-warm_started,3))), flush=True)
        started, last_report, tick, rss_samples = time.monotonic(), 0, 0, []
        started_wall = last_cycle_wall = time.time()
        max_cycle_gap = 0
        decode_seconds = []
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="soak-http") as executor, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="soak-decode-caller") as decoder, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="soak-background-driver") as background:
            while time.monotonic()-started < args.seconds:
                cycle = time.monotonic()
                wall_now = time.time()
                max_cycle_gap = max(max_cycle_gap, wall_now-last_cycle_wall)
                assert max_cycle_gap < 30, "sustained workload interrupted by a wall-clock gap"
                assert abs((wall_now-started_wall)-(cycle-started)) < 10, "wall/monotonic clock divergence"
                last_cycle_wall = wall_now
                decode_future = decoder.submit(decode_load.step)
                background_future = background.submit(background_load.step) if background_load else None
                update_quotes(tick)
                with module.app.app_context():
                    snapshot_load.step(module.db.session)
                    for identity in range(1, 21):
                        row = module.db.session.get(module.AlertPulse, f"soak{identity}")
                        row.evidence_json = evidence(identity, time.time(), True)
                        row.updated_at = datetime.now()
                    module.db.session.commit()
                    if tick%12 == 0:
                        module.store_transfer_networks("Gate", wallets)
                        maintenance_load.step()
                        lock_load.step()
                futures = [executor.submit(request, key) for key in paths]
                for future in futures:
                    key, elapsed = future.result(timeout=45)
                    measurements[key].append(elapsed)
                decode_seconds.append(decode_future.result(timeout=30))
                if background_future:
                    background_future.result(timeout=30)
                state = module.BACKGROUND_RUNTIME.snapshot()
                assert not background_errors, background_errors
                assert all(holder["worker"] == "soak-history" and holder["seconds"] < 5 for holder in state["db_holders"]), state
                elapsed = time.monotonic()-started
                if elapsed-last_report >= 60 or tick == 0:
                    raw_rss = int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], text=True).strip())
                    # `ps -o rss` reports KiB on both supported runners.
                    rss = raw_rss * 1024
                    assert 0 < rss < 640*1048576, "synthetic combined RSS exceeded its original bound"
                    rss_samples.append(rss)
                    print(json.dumps(dict(event="progress", at_utc=datetime.now(timezone.utc).isoformat(), seconds=round(elapsed), wall_seconds=round(time.time()-started_wall,3), max_cycle_gap_seconds=round(max_cycle_gap,3), cycles=tick+1, requests=sum(map(len, measurements.values())), rss_bytes=rss, snapshot_rows_updated=snapshot_load.row_updates, decode_responses=decode_load.responses, decode_last_seconds=round(decode_seconds[-1],3), decode_max_seconds=round(max(decode_seconds),3), index=module.ALERT_HISTORY_INDEX.status(), db=state, compressed_responses=dict(compressed_responses), background=background_load.status() if background_load else None, named_lock=lock_load.status(), latency={key: dict(last=round(values[-1],3), p95=round(sorted(values)[int(.95*(len(values)-1))],3), max=round(max(values),3)) for key, values in measurements.items()})), flush=True)
                    last_report = elapsed
                tick += 1
                time.sleep(max(0, 5-(time.monotonic()-cycle)))
        # A sleep during the final interval can end the loop without another
        # iteration. Check it too before accepting a continuous workload.
        assert time.time()-last_cycle_wall < 30, "final workload interval interrupted by a wall-clock gap"
        assert abs((time.time()-started_wall)-(time.monotonic()-started)) < 10, "final wall/monotonic clock divergence"
        assert all(hashlib.sha256((repo/name).read_bytes()).hexdigest() == digest for name, digest in fingerprints.items()), "candidate changed during soak"
        stop.set()
        history_thread.join(30)
        assert not history_thread.is_alive()
        assert not background_errors, background_errors
        with module.app.app_context():
            assert module.AlertEvent.query.count() == args.rows
            assert module.AlertPulse.query.count() == args.rows
            assert module.BasisTracking.query.count() == 50000
            snapshot_check = snapshot_load.verify(module.db.session)
            maintenance_check = maintenance_load.verify()
            check_integrity(module.db.session, module.db.metadata, module.db.engine.dialect.name)
            module.db.session.remove()
        decode_check = decode_load.verify()
        lock_check = lock_load.verify()
        assert lock_check["cycles"] == (tick + 11) // 12
        assert module.BACKGROUND_RUNTIME.snapshot()["db_checked_out"] == 0
        assert decode_check["cycles"] == tick
        background_check = background_load.verify() if background_load else None
        if background_check:
            assert background_check["cycles"] == tick
        if args.accept_gzip:
            assert compressed_responses["summary"] >= tick and compressed_responses["detail"] == tick
        for values in measurements.values():
            assert sorted(values)[int(.95*(len(values)-1))] < 5 and max(values) < 10, "HTTP latency outside original bounds"
        print(json.dumps(dict(event="passed", at_utc=datetime.now(timezone.utc).isoformat(), seconds=round(time.monotonic()-started), wall_seconds=round(time.time()-started_wall,3), max_cycle_gap_seconds=round(max_cycle_gap,3), cycles=tick, requests=sum(map(len, measurements.values())), rss_first=rss_samples[0], rss_last=rss_samples[-1], rss_max=max(rss_samples), files=fingerprints, snapshot_check=snapshot_check, maintenance_check=maintenance_check, decode_check=decode_check, background_check=background_check, lock_check=lock_check, compressed_responses=compressed_responses, source_counts_unchanged=True, integrity="ok")), flush=True)


if __name__ == "__main__":
    main()

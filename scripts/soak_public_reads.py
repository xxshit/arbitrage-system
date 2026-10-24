"""Offline sustained read/write test. ONLY creates its own synthetic SQLite DB.

No credentials, production database, upstream requests, push or trading workers.
Run from repo: .venv/bin/python scripts/soak_public_reads.py --seconds 7200
The temp DB/cache is removed on normal exit; the JSONL report is stdout.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=7200)
    parser.add_argument("--rows", type=int, default=100000)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    os.environ["TZ"] = "UTC"
    time.tzset()
    with tempfile.TemporaryDirectory(prefix="arbiscope-public-soak-") as directory:
        os.environ.update(DATABASE_URL="sqlite:///" + directory + "/synthetic.sqlite3", SECRET_KEY="synthetic-soak-only",
                          CHAT_ENCRYPTION_KEY="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
        import app as module
        from tests.test_alert_quality import historical_pulse
        from tests.test_alert_screening import quotes, reference
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
            with module.db.engine.connect() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL"))
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
        fingerprints = {name: hashlib.sha256((repo/name).read_bytes()).hexdigest() for name in ("app.py", "alert_history_index.py", "alert_read_payload.py", "read_performance.py", "static/app.js")}
        print(json.dumps(dict(event="ready", rows=args.rows, pid=os.getpid(), files=fingerprints)), flush=True)
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
        history_thread = threading.Thread(target=refresh_history, name="soak-history")
        history_thread.start()
        measurements = {key: [] for key in ("summary", "detail", "active", "spot", "dual")}
        paths = dict(summary="/api/alerts?view=summary", detail="/api/alerts?view=detail&symbol=SOAK1/USDT&page=2", active="/api/alerts?view=active", spot="/api/spot-futures", dual="/api/dual-futures")
        def request(key):
            began = time.monotonic()
            client = module.app.test_client()
            with client.session_transaction() as session:
                session.update(user_id=user_id, auth_token="synthetic-token")
            response = client.get(paths[key])
            assert response.status_code == 200, (key, response.status_code, response.get_data(as_text=True)[:150])
            data = response.get_json()
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
        print(json.dumps(dict(event="warm", seconds=round(time.monotonic()-warm_started,3))), flush=True)
        started, last_report, tick, rss_samples = time.monotonic(), 0, 0, []
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="soak-http") as executor:
            while time.monotonic()-started < args.seconds:
                cycle = time.monotonic()
                update_quotes(tick)
                with module.app.app_context():
                    for identity in range(1, 21):
                        row = module.db.session.get(module.AlertPulse, f"soak{identity}")
                        row.evidence_json = evidence(identity, time.time(), True)
                        row.updated_at = datetime.now()
                    module.db.session.commit()
                    if tick%12 == 0:
                        module.store_transfer_networks("Gate", wallets)
                futures = [executor.submit(request, key) for key in paths]
                for future in futures:
                    key, elapsed = future.result(timeout=45)
                    measurements[key].append(elapsed)
                state = module.BACKGROUND_RUNTIME.snapshot()
                assert not background_errors, background_errors
                assert all(holder["worker"] == "soak-history" and holder["seconds"] < 5 for holder in state["db_holders"]), state
                elapsed = time.monotonic()-started
                if elapsed-last_report >= 60 or tick == 0:
                    rss = int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], text=True).strip())*1024
                    rss_samples.append(rss)
                    print(json.dumps(dict(event="progress", seconds=round(elapsed), cycles=tick+1, requests=sum(map(len, measurements.values())), rss_bytes=rss, index=module.ALERT_HISTORY_INDEX.status(), db=state, latency={key: dict(last=round(values[-1],3), p95=round(sorted(values)[int(.95*(len(values)-1))],3), max=round(max(values),3)) for key, values in measurements.items()})), flush=True)
                    last_report = elapsed
                tick += 1
                time.sleep(max(0, 5-(time.monotonic()-cycle)))
        assert all(hashlib.sha256((repo/name).read_bytes()).hexdigest() == digest for name, digest in fingerprints.items()), "candidate changed during soak"
        stop.set()
        history_thread.join(30)
        assert not history_thread.is_alive()
        assert not background_errors, background_errors
        with module.app.app_context():
            assert module.AlertEvent.query.count() == args.rows
            assert module.AlertPulse.query.count() == args.rows
            assert module.db.session.execute(text("PRAGMA integrity_check")).scalar() == "ok"
            module.db.session.remove()
            module.db.engine.dispose()
        print(json.dumps(dict(event="passed", seconds=round(time.monotonic()-started), cycles=tick, requests=sum(map(len, measurements.values())), rss_first=rss_samples[0], rss_last=rss_samples[-1], rss_max=max(rss_samples), files=fingerprints)), flush=True)


if __name__ == "__main__":
    main()

"""Explicit standalone worker. Never imports app or starts market jobs.

python -m binance_hedge.worker --install-schema  # fresh DB backup required
python -m binance_hedge.worker --serve           # query-only unless explicitly enabled
"""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import secrets
import time

from dotenv import load_dotenv
from sqlalchemy import create_engine

from .client import BinanceClient
from .executor import Executor
from .planner import GuardError
from .store import Store
from .web import configured_vault


def run(store, vault, *, once=False):
    # MySQL GET_LOCK holds an extra connection; use a private local flock for
    # this single-host executor instead. Row locks still protect confirmations.
    lock_path = Path(os.getenv("BINANCE_EXECUTOR_LOCK", "/run/arbitrage-binance/executor.lock"))
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        boot = secrets.token_hex(16)
        # Recover once on startup by querying only. Never repeat a reduction.
        for item in store.unfinished():
            process(store, vault, item, recover=True)
        last_beat = 0
        while True:
            if time.monotonic() - last_beat >= 5:
                store.beat(boot)
                last_beat = time.monotonic()
            item = store.claim(allow_orders=os.getenv("BINANCE_EXECUTION_ENABLED") == "1")
            if item:
                process(store, vault, item, recover=item["state"] == "QUERY_QUEUED")
            if once:
                return
            time.sleep(1)


def process(store, vault, item, *, recover=False):
    client = None
    try:
        row = store.account(item["user_id"])
        if not row or row["version"] != item["account_version"]:
            raise GuardError("账户密钥版本已变化，停止执行")
        creds = vault.open(item["user_id"], row["sealed"])
        client = BinanceClient(creds["key"], creds["secret"], allow_orders=not recover)
        client.rate_guard, client.rate_pause = store.check_rate, store.pause_rate
        creds.clear()
        executor = Executor(store, client)
        if recover:
            executor.reconcile(item, item["result"])
        else:
            uid = item["plan"]["account_uid"]
            if hashlib.sha256(uid.encode()).hexdigest() != row["uid_hash"]:
                raise GuardError("币安账户身份不匹配")
            executor.execute(item, uid)
    except Exception:
        # A DB failure may itself prevent this write. A restart still sees the
        # last durable state and recovers through query-only, never placement.
        current = store.get(item["user_id"], item["id"])
        result = current["result"]
        result["reason"] = "执行中断，需按原订单号核验；禁止重复下单"
        store.state(item["id"], "UNKNOWN", result)
    finally:
        if client:
            client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-schema", action="store_true")
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    url = os.environ["DATABASE_URL"]
    engine = create_engine(url, pool_size=1, max_overflow=0, pool_pre_ping=True, pool_recycle=300, hide_parameters=True)
    try:
        store = Store(engine)
        if args.install_schema and not args.serve:
            store.install()
        elif args.serve and not args.install_schema:
            run(store, configured_vault())
        else:
            raise SystemExit("Explicit schema install or enabled executor required")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

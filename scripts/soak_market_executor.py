"""Isolated high-frequency market executor allocation soak.

This is an offline source-shape test, not a production diagnostic: it imports
the app against an in-memory SQLite URL, replaces every public HTTP call with
fresh JSON-shaped payloads, and never opens a socket.  It exercises the same
5-second payload and dashboard fan-out that previously created short-lived
ThreadPoolExecutors.  The report is JSONL on stdout.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import sys
import threading
import time
from unittest.mock import patch


SPOT_URLS = {
    "futures_info": "https://fapi.binance.com/fapi/v1/exchangeInfo",
    "futures_books": "https://fapi.binance.com/fapi/v1/ticker/bookTicker",
    "futures_24h": "https://fapi.binance.com/fapi/v1/ticker/24hr",
    "funding": "https://fapi.binance.com/fapi/v1/premiumIndex",
    "funding_info": "https://fapi.binance.com/fapi/v1/fundingInfo",
    "binance_spot": "https://api.binance.com/api/v3/ticker/bookTicker",
    "binance_spot_24h": "https://api.binance.com/api/v3/ticker/24hr",
    "gate_spot": "https://api.gateio.ws/api/v4/spot/tickers",
    "bitget_spot": "https://api.bitget.com/api/v2/spot/market/tickers",
}
DUAL_URLS = {
    "binance_books": SPOT_URLS["futures_books"],
    "binance_24h": SPOT_URLS["futures_24h"],
    "binance_funding": SPOT_URLS["funding"],
    "binance_funding_info": SPOT_URLS["funding_info"],
    "binance_info": SPOT_URLS["futures_info"],
    "bybit_instruments": "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000",
    "bybit_tickers": "https://api.bybit.com/v5/market/tickers?category=linear",
    "okx_instruments": "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
    "okx_tickers": "https://www.okx.com/api/v5/market/tickers?instType=SWAP",
    "okx_mark_prices": "https://www.okx.com/api/v5/public/mark-price?instType=SWAP",
    "okx_index_tickers": "https://www.okx.com/api/v5/market/index-tickers?quoteCcy=USDT",
    "okx_open_interest": "https://www.okx.com/api/v5/public/open-interest?instType=SWAP",
}


def rss_bytes():
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB; macOS reports bytes.  The test runs on both.
    return int(usage * 1024 if usage < 10**8 else usage)


def source_fingerprints(repo):
    return {
        name: hashlib.sha256((repo / name).read_bytes()).hexdigest()
        for name in ("app.py", "market_transport.py", "scripts/soak_market_executor.py")
    }


def json_round_trip(value):
    # Force the same temporary bytes/string/object lifecycle as a public JSON
    # response, while remaining entirely offline.
    return json.loads(json.dumps(value, separators=(",", ":")))


def payload_for(url, tick, rows=1800):
    symbols = [f"S{i:05d}USDT" for i in range(rows)]
    if "exchangeInfo" in url or "instruments-info" in url or "public/instruments" in url:
        items = [{"symbol": symbol, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "settleCoin": "USDT", "state": "live", "underlying": "x" * (32 + tick % 97)} for symbol in symbols]
        if "instruments-info" in url:
            return json_round_trip({"retCode": 0, "result": {"list": items}})
        if "public/instruments" in url:
            return json_round_trip({"code": "0", "data": items})
        return json_round_trip({"symbols": items})
    if "bookTicker" in url:
        return json_round_trip([{"symbol": symbol, "askPrice": "100.1", "bidPrice": "100.0", "ask1Price": "100.1", "bid1Price": "100.0", "lowest_ask": "100.1", "highest_bid": "100.0", "x": "b" * (32 + tick % 97)} for symbol in symbols])
    if "premiumIndex" in url or "mark-price" in url:
        rows_out = [{"symbol": symbol, "markPrice": "100.1", "indexPrice": "100.0", "lastFundingRate": "0.0001", "nextFundingTime": 1000, "time": 1000, "x": "p" * (32 + tick % 97)} for symbol in symbols]
        return json_round_trip(rows_out if "premiumIndex" in url else {"code": "0", "data": rows_out})
    if "fundingInfo" in url:
        return json_round_trip([{"symbol": symbol, "fundingIntervalHours": 8, "x": "f" * (32 + tick % 97)} for symbol in symbols])
    if "ticker/24hr" in url or "market/tickers" in url or "index-tickers" in url or "open-interest" in url:
        rows_out = [{"symbol": symbol, "quoteVolume": "1000000", "turnover24h": "1000000", "markPrice": "100.1", "indexPrice": "100.0", "x": "t" * (32 + tick % 97)} for symbol in symbols]
        if "api.bybit.com" in url:
            return json_round_trip({"retCode": 0, "result": {"list": rows_out}})
        if "www.okx.com" in url:
            return json_round_trip({"code": "0", "data": rows_out})
        return json_round_trip(rows_out)
    return json_round_trip({"retCode": 0, "data": [{"symbol": symbol, "x": "s" * (32 + tick % 97)} for symbol in symbols]})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--rows", type=int, default=1800)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    os.environ.update(DATABASE_URL="sqlite://", SECRET_KEY="executor-soak-only", CHAT_ENCRYPTION_KEY="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
    sys.path.insert(0, str(repo))
    import app as module

    cache = {}
    module.MARKET_PAYLOAD_CACHE = cache
    module.MARKET_PAYLOAD_CACHE_LOCK = threading.Lock()
    source = source_fingerprints(repo)
    tick = 0

    def market_get_json(url, timeout=1.5):
        return payload_for(url, tick, args.rows)

    def dashboard_get_json(url, timeout=1.5):
        if "binance" in url:
            return {"lastPrice": "100.0", "priceChangePercent": "1.0", "quoteVolume": "1000000"}
        if "okx" in url:
            return {"data": [{"last": "100.1"}]}
        return {"result": {"list": [{"lastPrice": "100.2"}]}}

    def run_lane():
        module.fetch_market_payloads(SPOT_URLS, {"futures_info": 0, "funding_info": 0, "futures_24h": 0, "binance_spot_24h": 0})
        module.fetch_market_payloads(DUAL_URLS, {"binance_24h": 0, "binance_funding_info": 0, "binance_info": 0, "bybit_instruments": 0, "okx_instruments": 0})

    measurements = []
    rss_samples = []
    started = time.monotonic()
    print(json.dumps({"event": "ready", "at_utc": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), "source": source, "rows": args.rows, "seconds": args.seconds}), flush=True)
    with patch.object(module.MARKET_HTTP, "get_json", side_effect=market_get_json), patch.object(module, "get_json", side_effect=dashboard_get_json), ThreadPoolExecutor(max_workers=2, thread_name_prefix="executor-soak-lanes") as lanes:
        while time.monotonic() - started < args.seconds:
            cycle = time.monotonic()
            current_tick = tick
            tick += 1
            futures = [lanes.submit(run_lane) for _ in range(2)]
            module.refresh_dashboard_opportunities()
            for future in futures:
                future.result(timeout=30)
            elapsed = time.monotonic() - cycle
            measurements.append(elapsed)
            if not measurements or len(measurements) == 1 or len(measurements) % 12 == 0:
                rss = rss_bytes()
                rss_samples.append(rss)
                print(json.dumps({"event": "progress", "seconds": round(time.monotonic() - started, 3), "cycles": len(measurements), "rss_bytes": rss, "allocator": module.memory_status(), "threads": {"market": len(module.MARKET_HTTP_EXECUTOR._threads), "dashboard": len(module.DASHBOARD_EXECUTOR._threads), "dashboard_http": len(module.DASHBOARD_HTTP_EXECUTOR._threads)}, "cycle_seconds": round(elapsed, 3)}), flush=True)
            time.sleep(max(0, args.interval - (time.monotonic() - cycle)))
    assert source == source_fingerprints(repo), "source changed during soak"
    assert measurements and max(measurements) < 30, max(measurements)
    print(json.dumps({"event": "passed", "seconds": round(time.monotonic() - started, 3), "cycles": len(measurements), "rss_first": rss_samples[0], "rss_last": rss_samples[-1], "rss_max": max(rss_samples), "rss_medians": [statistics.median(rss_samples[max(0, i - 3):i]) for i in range(3, len(rss_samples) + 1, 3)], "latency_p95": sorted(measurements)[max(0, int(.95 * (len(measurements) - 1)))], "source": source}), flush=True)


if __name__ == "__main__":
    main()

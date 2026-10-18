"""Read-only public stream smoke check; does not import the Flask application."""
import concurrent.futures
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anomaly_streams import ENDPOINTS, parse_frame, subscription
import websocket


def probe(feed):
    symbol = "BTC_USDT" if feed == "gate" else "BTC-USDT-SWAP" if feed == "okx" else "BTCUSDT"
    started = time.monotonic()
    sock = None
    try:
        sock = websocket.create_connection(ENDPOINTS[feed], timeout=10)
        sock.send(json.dumps(subscription(feed, [symbol])))
        while time.monotonic() - started < 20:
            frame = sock.recv()
            now = time.time()
            items = parse_frame(feed, frame, now)
            if items:
                item = items[0]
                return dict(feed=feed, ok=True, lag_ms=round((now-item["at"])*1000), timestamp_source=item.get("timestamp_source", "exchange"), fields=sorted(item))
        return dict(feed=feed, ok=False, error="no_public_data")
    except Exception as error:
        return dict(feed=feed, ok=False, error=type(error).__name__)
    finally:
        if sock:
            sock.close()


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        results = list(pool.map(probe, ENDPOINTS))
    print(json.dumps(results, ensure_ascii=False))
    raise SystemExit(0 if all(item["ok"] for item in results) else 1)

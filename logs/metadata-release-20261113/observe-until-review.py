"""Bounded, local read-only follow-through in this chat. No server timer or load."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import subprocess
import sys
import time

here = Path(__file__).resolve().parent
release = json.loads((here / 'release/result.json').read_text())
deadline = datetime.fromisoformat(release['completed_at_utc']) + timedelta(hours=2, seconds=25)
(here / 'observation-followthrough-once.lock').open('x').close()
print(json.dumps({'event': 'read_only_followthrough', 'until_utc': deadline.isoformat(),
                  'server_changes': False, 'synthetic_requests': 0}), flush=True)
while True:
    wait_until = min(datetime.now(timezone.utc) + timedelta(minutes=5), deadline)
    while datetime.now(timezone.utc) < wait_until:
        if (here / 'stop-observation-followthrough').exists():
            print(json.dumps({'event': 'stopped_by_local_control'}), flush=True)
            sys.exit(0)
        time.sleep(min(1, max(0, (wait_until - datetime.now(timezone.utc)).total_seconds())))
    run = subprocess.run([sys.executable, str(here / 'collect-observation.py')], capture_output=True, text=True, timeout=85)
    if run.returncode:
        print(json.dumps({'event': 'observation_failed_no_retry', 'exit': run.returncode,
                          'collector_output': run.stdout, 'stderr_bytes': len(run.stderr.encode())}), flush=True)
        (here / 'followthrough-failure.stderr').write_text(run.stderr)
        sys.exit(run.returncode)
    summary = subprocess.run([sys.executable, str(here / 'summarize-observations.py')], capture_output=True, text=True, timeout=15)
    print(summary.stdout.strip(), flush=True)
    if summary.returncode:
        (here / 'followthrough-summary-failure.stderr').write_text(summary.stderr)
        sys.exit(summary.returncode)
    if datetime.now(timezone.utc) >= deadline:
        print(json.dumps({'event': 'review_window_collected', 'at_utc': datetime.now(timezone.utc).isoformat(),
                          'memory_verdict': 'manual review required'}), flush=True)
        break

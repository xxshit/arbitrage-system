"""Collect one read-only production observation; not a load test or scheduler."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import subprocess
import sys

here = Path(__file__).resolve().parent
tag = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
output = here / ('observation-' + tag + '.json')
error = output.with_suffix('.stderr')
with output.open('xb') as out, error.open('xb') as err:
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'lewan-game',
         '/opt/arbitrage-hub/.venv/bin/python -B -'],
        input=(here / 'observe-metadata-readonly.py').read_bytes(),
        stdout=out, stderr=err, timeout=70)
receipt = {'collected_at_utc': datetime.now(timezone.utc).isoformat(),
           'exit': result.returncode, 'report': str(output.relative_to(here)),
           'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
           'bytes': output.stat().st_size, 'stderr_bytes': error.stat().st_size}
output.with_suffix('.receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt), flush=True)
if result.returncode == 0:
    data = json.loads(output.read_text())
    print(json.dumps({k: data[k] for k in (
        'finished_at_utc', 'since_release_seconds', 'identity_matches_release',
        'identity_unchanged', 'source_matches_release', 'health_count', 'health_last',
        'errors', 'boot_workers', 'progress_before', 'progress_after',
        'transactions', 'connections', 'quarter_hour_buckets')}, sort_keys=True), flush=True)
sys.exit(result.returncode)

"""Summarize collected production records only; never infer an automatic pass."""
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import statistics

here = Path(__file__).resolve().parent
release = json.loads((here / 'release/result.json').read_text())
expected_pid = release['identity']['worker']
samples = {}
reports = []
for path in sorted(here.glob('observation-*.json')):
    if path.name.endswith('.receipt.json'):
        continue
    receipt = path.with_suffix('.receipt.json')
    if not receipt.exists():
        continue
    meta = json.loads(receipt.read_text())
    if meta['exit'] != 0 or meta['stderr_bytes'] != 0:
        continue
    assert hashlib.sha256(path.read_bytes()).hexdigest() == meta['sha256']
    data = json.loads(path.read_text())
    assert data['identity_matches_release'] and data['identity_unchanged']
    assert data['source_matches_release']
    reports.append(data)
    for sample in data['health_records']:
        assert sample['pid'] == expected_pid
        stamp = sample['journal_utc_us']
        if stamp in samples:
            assert samples[stamp] == sample
        samples[stamp] = sample
assert samples
rows = [samples[k] for k in sorted(samples)]
by_quarter = defaultdict(list)
for row in rows:
    by_quarter[row['journal_utc_us'] // 900000000].append(row)
last_time = datetime.fromisoformat(reports[-1]['finished_at_utc']).timestamp()
start_time = datetime.fromisoformat(release['start_requested_at_utc']).timestamp()
buckets = []
fields = ('rss_bytes', 'anonymous_bytes', 'allocator_used_bytes', 'allocator_free_bytes',
          'file_cache_bytes', 'db_checked_out', 'db_longest_checkout_seconds')
for bucket, records in sorted(by_quarter.items()):
    item = {'start_utc': datetime.fromtimestamp(bucket * 900, timezone.utc).isoformat(),
            'samples': len(records),
            'complete': bucket * 900 >= start_time and (bucket + 1) * 900 <= last_time and len(records) >= 14}
    for field in fields:
        values = [r[field] for r in records if field in r]
        if values:
            item[field] = {'median': statistics.median(values), 'min': min(values), 'max': max(values)}
    buckets.append(item)
late = [r for r in rows if r['journal_utc_us'] / 1e6 >= start_time + 1800]
slope = None
if len(late) >= 2:
    x = [(r['journal_utc_us'] / 1e6 - start_time) / 3600 for r in late]
    y = [r['rss_bytes'] / 1048576 for r in late]
    xm, ym = statistics.mean(x), statistics.mean(y)
    slope = sum((a - xm) * (b - ym) for a, b in zip(x, y)) / sum((a - xm) ** 2 for a in x)
summary = {'created_at_utc': datetime.now(timezone.utc).isoformat(),
           'last_collected_at_utc': reports[-1]['finished_at_utc'],
           'worker': expected_pid, 'invocation': release['identity']['services']['arbitrage-private']['InvocationID'],
           'elapsed_since_initial_acceptance_seconds': last_time - datetime.fromisoformat(release['completed_at_utc']).timestamp(),
           'unique_health_samples': len(rows), 'first_health': rows[0], 'last_health': rows[-1],
           'health_max_gap_seconds': max((b['journal_utc_us'] - a['journal_utc_us']) / 1e6 for a, b in zip(rows, rows[1:])) if len(rows) > 1 else None,
           'startup_incomplete_samples': sum(r['workers'] != 30 for r in rows),
           'dead_or_restart_samples': sum(bool(r.get('dead')) or bool(r.get('restarts')) for r in rows),
           'last_error_counts': reports[-1]['errors'], 'boot_workers': reports[-1]['boot_workers'],
           'quarter_hour_buckets': buckets, 'rss_ols_mib_per_hour_after_30min': slope,
           'first_progress': reports[0]['progress_before'], 'last_progress': reports[-1]['progress_after'],
           'latest_connections': reports[-1]['connections'], 'latest_transactions': reports[-1]['transactions'],
           'boundary': 'Descriptive actual-production samples, not synthetic tests. Slope alone is not a pass. Manual late-window and functionality review required; preexisting game outage remains separate.'}
path = here / 'observation-summary.json'
path.write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
print(json.dumps({'summary': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                  'elapsed_seconds': summary['elapsed_since_initial_acceptance_seconds'],
                  'samples': len(rows), 'last_rss_mib': rows[-1]['rss_bytes'] / 1048576,
                  'late_rss_mib_per_hour': slope,
                  'quarters': [{'utc': b['start_utc'], 'complete': b['complete'], 'samples': b['samples'],
                                'rss_median_mib': b['rss_bytes']['median'] / 1048576} for b in buckets]}))

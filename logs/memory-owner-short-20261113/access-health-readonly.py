"""Aggregate existing access/health logs only. Discard addresses/queries/users."""
import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

invocation = '25e853ed5afe41bbabb2a7a1d6f380a6'
expected_pid = 2383603
assert subprocess.check_output(['systemctl', 'show', 'arbitrage-private', '-p', 'InvocationID', '--value'], text=True).strip() == invocation
result = {'started_at_utc': datetime.now(timezone.utc).isoformat(), 'invocation': invocation, 'expected_pid': expected_pid}
health = []
with subprocess.Popen(['journalctl', '-u', 'arbitrage-private', '_SYSTEMD_INVOCATION_ID='+invocation, '--no-pager', '-o', 'json'], stdout=subprocess.PIPE, text=True) as proc:
    for line in proc.stdout:
        entry = json.loads(line)
        message = entry.get('MESSAGE', '')
        if not isinstance(message, str) or 'runtime_health ' not in message:
            continue
        h = json.loads(message.split('runtime_health ', 1)[1])
        assert h['pid'] == expected_pid
        health.append({'utc_us': int(entry['__REALTIME_TIMESTAMP']), **{k: h.get(k) for k in ('rss_bytes', 'allocator_used_bytes', 'allocator_free_bytes')}, 'index_rows': h.get('alert_history', {}).get('rows')})
    assert proc.wait(timeout=5) == 0
start = health[0]['utc_us'] / 1e6
end = health[-1]['utc_us'] / 1e6
pattern = re.compile(rb'\[([^\]]+)\] "([A-Z]+) ([^ ]+) HTTP/[^\"]+" (\d{3}) (\d+|-)')
totals = Counter()
minutes = defaultdict(Counter)
files = []
for name in ('access.log.1', 'access.log'):
    path = Path('/var/log/nginx') / name
    if not path.exists():
        continue
    size = path.stat().st_size
    assert size <= 5*1024*1024, 'bounded log read'
    metadata = {'file': name, 'bytes_at_start': size, 'parsed': 0, 'unparsed': 0}
    oldest = newest = None
    with path.open('rb') as stream:
        for line in stream:
            match = pattern.search(line)
            if not match:
                metadata['unparsed'] += 1
                continue
            stamp, method, target, status, byte_count = match.groups()
            at = datetime.strptime(stamp.decode('ascii'), '%d/%b/%Y:%H:%M:%S %z').timestamp()
            metadata['parsed'] += 1
            oldest = at if oldest is None else min(oldest, at)
            newest = at if newest is None else max(newest, at)
            if not start <= at <= end:
                continue
            route = target.split(b'?', 1)[0]
            labels = ['all_http']
            if route.startswith(b'/api/'):
                labels.append('all_api')
                if 200 <= int(status) < 300:
                    labels.append('all_api_2xx')
                if route == b'/api/alerts':
                    labels.append('alerts')
                    if 200 <= int(status) < 300:
                        labels.append('alerts_2xx')
                if any(piece in route for piece in (b'history', b'opportunit', b'thinking')):
                    labels.append('history_or_opportunity')
            for label in labels:
                totals[label] += 1
                minutes[int(at)//60][label] += 1
    metadata.update(oldest_utc=datetime.fromtimestamp(oldest, timezone.utc).isoformat() if oldest else None,
                    newest_utc=datetime.fromtimestamp(newest, timezone.utc).isoformat() if newest else None)
    files.append(metadata)
result.update(window_start_utc=datetime.fromtimestamp(start, timezone.utc).isoformat(),
              window_end_utc=datetime.fromtimestamp(end, timezone.utc).isoformat(),
              files=files, counts=dict(totals), health=health,
              request_minutes=[{'minute_epoch': minute*60, **dict(counts)} for minute, counts in sorted(minutes.items())])
result['identity_unchanged'] = subprocess.check_output(['systemctl', 'show', 'arbitrage-private', '-p', 'InvocationID', '--value'], text=True).strip() == invocation
result['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
print(json.dumps(result, sort_keys=True))

"""Read-only OS/journal/SQL aggregates. No app import, attach or heap reads."""
import hashlib
import json
import re
import shutil
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def identity():
    services = {}
    for name in ('arbitrage-private', 'mysql', 'lewan-game'):
        services[name] = dict(line.split('=', 1) for line in subprocess.check_output(
            ['systemctl', 'show', name, '-p', 'MainPID', '-p', 'ActiveState',
             '-p', 'InvocationID', '-p', 'NRestarts'], text=True).splitlines())
    master = int(services['arbitrage-private']['MainPID'])
    children = list(map(int, Path(f'/proc/{master}/task/{master}/children').read_text().split()))
    assert len(children) == 1, 'Unexpected worker count'
    worker = children[0]
    return {'services': services, 'worker': worker, 'worker_start_ticks':
            int(Path(f'/proc/{worker}/stat').read_text().rsplit(')', 1)[1].split()[19])}


def memory(pid):
    result = {'at_utc': now()}
    for filename in ('status', 'smaps_rollup'):
        values = {}
        for line in Path(f'/proc/{pid}/{filename}').read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit() and (
                parts[0].startswith(('Vm', 'Rss', 'Pss', 'Private_', 'Shared_', 'Anonymous:', 'Swap'))
                or parts[0] == 'Threads:'
            ):
                values[parts[0].rstrip(':')] = int(parts[1])
        result[filename] = values
    relative = next(line.split('::', 1)[1] for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines() if line.startswith('0::'))
    root = Path('/sys/fs/cgroup') / relative.lstrip('/')
    for name in ('memory.current', 'memory.max', 'memory.high', 'memory.events', 'memory.pressure'):
        result[name] = (root / name).read_text().strip()
    return result


def select(query):
    prefix = "SET SESSION time_zone='SYSTEM'; SET SESSION TRANSACTION READ ONLY; SET SESSION max_execution_time=5000; "
    lines = subprocess.check_output(['mysql', '--batch', '--raw', '--database=arbitrage_hub', '-e', prefix + query], text=True, timeout=15).splitlines()
    return [dict(zip(lines[0].split('\t'), row.split('\t'))) for row in lines[1:]]


def progress():
    return select("SELECT 'spot' AS kind,MAX(captured_at) AS latest_at FROM latest_market_snapshot UNION ALL SELECT 'dual',MAX(captured_at) FROM latest_dual_futures_snapshot UNION ALL SELECT 'alarm',MAX(updated_at) FROM alert_pulse;")


r = {'started_at_utc': now(), 'identity': identity()}
pid = r['identity']['worker']
invocation = r['identity']['services']['arbitrage-private']['InvocationID']
r['memory_before'] = memory(pid)
r['progress_before'] = progress()
r['clock'] = select('SELECT UTC_TIMESTAMP(6) AS utc_clock,@@transaction_read_only AS read_only,@@session.time_zone AS session_zone;')
r['refresh_rules'] = select("SELECT rule_key,enabled,value FROM runtime_rule WHERE rule_key IN ('spot_market_refresh','dual_market_refresh') ORDER BY rule_key;")
r['transactions'] = select("SELECT COUNT(*) AS count,COALESCE(MAX(TIMESTAMPDIFF(SECOND,t.trx_started,NOW())),0) AS max_seconds,COALESCE(SUM(t.trx_rows_locked),0) AS locked_rows FROM information_schema.INNODB_TRX t JOIN information_schema.PROCESSLIST p ON p.ID=t.trx_mysql_thread_id WHERE p.DB='arbitrage_hub' AND p.ID<>CONNECTION_ID();")
r['connections'] = select("SELECT COMMAND,COUNT(*) AS count,MAX(TIME) AS max_seconds FROM information_schema.PROCESSLIST WHERE DB='arbitrage_hub' AND ID<>CONNECTION_ID() GROUP BY COMMAND;")
r['source_sha256'] = {name: hashlib.sha256((Path('/opt/arbitrage-hub') / name).read_bytes()).hexdigest() for name in (
    'app.py', 'alert_history_index.py', 'anomaly_engine.py', 'background_runtime.py',
    'market_transport.py', 'spread_history.py', 'opportunity_history.py',
    'opportunity_activity.py', 'opportunity_validation.py', 'funding_history.py')}
buckets = defaultdict(list)
errors = Counter()
boots = []
first = last = None
records = 0
command = ['journalctl', '-u', 'arbitrage-private', '_SYSTEMD_INVOCATION_ID=' + invocation,
           '--no-pager', '-o', 'json']
with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as proc:
    for line in proc.stdout:
        entry = json.loads(line)
        message = entry.get('MESSAGE', '')
        if not isinstance(message, str):
            continue
        boot = re.search(r'Booting worker with pid: (\d+)', message)
        if boot:
            boots.append({'pid': int(boot[1]), 'utc_us': entry['__REALTIME_TIMESTAMP']})
        for label in ('WORKER TIMEOUT', 'Traceback (most recent call last)', 'background_worker_retry', '[ERROR]', 'oom-kill'):
            if label in message:
                errors[label] += 1
        if 'runtime_health ' not in message:
            continue
        health = json.loads(message.split('runtime_health ', 1)[1])
        health['journal_utc_us'] = int(entry['__REALTIME_TIMESTAMP'])
        records += 1
        if first is None:
            first = health
        last = health
        buckets[(health.get('pid'), health['journal_utc_us'] // 900000000)].append(health)
    assert proc.wait(timeout=5) == 0
r['health_count'] = records
r['health_first'] = first
r['health_last'] = last
r['boot_workers'] = boots
r['errors'] = dict(errors)
r['quarter_hour_buckets'] = []
for (worker, bucket), rows in sorted(buckets.items()):
    record = {'pid': worker, 'start_utc': datetime.fromtimestamp(bucket * 900, timezone.utc).isoformat(), 'samples': len(rows)}
    for key in ('rss_bytes', 'allocator_used_bytes', 'allocator_free_bytes', 'anonymous_bytes', 'file_cache_bytes', 'cgroup_bytes', 'db_checked_out', 'workers'):
        values = [row[key] for row in rows if key in row]
        if values:
            record[key] = {'median': statistics.median(values), 'min': min(values), 'max': max(values)}
    record['alert_rows'] = {'first': rows[0].get('alert_history', {}).get('rows'), 'last': rows[-1].get('alert_history', {}).get('rows')}
    r['quarter_hour_buckets'].append(record)
time.sleep(20)
r['progress_after'] = progress()
r['memory_after'] = memory(pid)
r['identity_after'] = identity()
r['identity_unchanged'] = r['identity'] == r['identity_after']
r['disk_free_bytes'] = shutil.disk_usage('/').free
r['host_memory_pressure'] = Path('/proc/pressure/memory').read_text().strip()
r['finished_at_utc'] = now()
print(json.dumps(r, sort_keys=True))

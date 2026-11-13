"""One bounded observation of kernel residency/counters; no process-memory attach."""
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

PID = 2392786
MASTER = 2392782
START = 734225447
INVOCATION = '02fcf4b56a07420f891d0c161f63c8bd'
DURATION = 130


def utc():
    return datetime.now(timezone.utc).isoformat()


def stat_fields(path):
    return path.read_text().rsplit(')', 1)[1].split()


def identity():
    props = dict(line.split('=', 1) for line in subprocess.check_output(
        ['systemctl', 'show', 'arbitrage-private', '-p', 'MainPID', '-p', 'InvocationID'],
        text=True, timeout=5).splitlines())
    assert props['InvocationID'] == INVOCATION and int(props['MainPID']) == MASTER
    assert Path(f'/proc/{MASTER}/task/{MASTER}/children').read_text().split() == [str(PID)]
    assert int(stat_fields(Path(f'/proc/{PID}/stat'))[19]) == START
    return dict(master=MASTER, worker=PID, start_ticks=START, invocation=INVOCATION)


def guard():
    pressure = dict(word.split('=') for word in Path('/proc/pressure/memory').read_text().splitlines()[0].split()[1:])
    available = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))) * 1024
    assert float(pressure['avg10']) <= 5 and available >= 512*1024**2, 'resource guard'
    prefix = "SET SESSION time_zone='SYSTEM'; SET SESSION TRANSACTION READ ONLY; SET SESSION max_execution_time=3000; "
    query = "SELECT TIMESTAMPDIFF(SECOND,MAX(captured_at),UTC_TIMESTAMP()) FROM latest_market_snapshot UNION ALL SELECT TIMESTAMPDIFF(SECOND,MAX(captured_at),UTC_TIMESTAMP()) FROM latest_dual_futures_snapshot;"
    ages = list(map(int, subprocess.check_output(['mysql', '-N', '-B', '--database=arbitrage_hub', '-e', prefix+query], text=True, timeout=5).split()))
    assert len(ages) == 2 and max(ages) <= 30 and min(ages) >= -1, 'quote age guard'
    return dict(at_utc=utc(), host_pressure_avg10=float(pressure['avg10']), available_bytes=available, quote_age_seconds=ages)


def all_threads():
    out = {}
    for task in Path(f'/proc/{PID}/task').iterdir():
        try:
            row = stat_fields(task/'stat')
            out[int(task.name)] = (int(row[19]), int(row[7]), int(row[11])+int(row[12]))
        except FileNotFoundError:
            pass
    return out


def precise_memory():
    out = {}
    for line in Path(f'/proc/{PID}/smaps_rollup').read_text().splitlines()[1:]:
        key, _, val = line.partition(':')
        if key in ('Rss', 'Anonymous', 'Private_Dirty', 'Swap'):
            out[key+'_bytes'] = int(val.split()[0])*1024
    proc = stat_fields(Path(f'/proc/{PID}/stat'))
    assert int(proc[19]) == START
    out['stat_rss_bytes'] = int(proc[21])*os.sysconf('SC_PAGE_SIZE')
    out['process_minor'] = int(proc[7])
    return out


result = dict(started_at_utc=utc(), guards=[], samples=[], page_bytes=os.sysconf('SC_PAGE_SIZE'),
    boundary='Kernel metadata only. One-second overlap is correlation, not malloc ownership or a Python function identity. No process-memory attach, heap, stack, locals, SQL parameters, or account contents.')
start = time.monotonic()
try:
    result['identity'] = identity()
    result['guards'].append(guard())
    previous = all_threads()
    result['baseline'] = dict(at_utc=utc(), memory=precise_memory(), threads=len(previous))
    start = time.monotonic()
    for n in range(1, DURATION+1):
        time.sleep(max(0, start+n-time.monotonic()))
        tick = time.monotonic()
        current = all_threads()
        memory = precise_memory()
        changes = []
        for tid, (stamp, fault, cpu) in current.items():
            old = previous.get(tid)
            if old and old[0] == stamp:
                delta, cput = fault-old[1], cpu-old[2]
                if delta or cput:
                    changes.append(dict(tid=tid, start_ticks=stamp, minor=delta, cpu_ticks=cput))
        seconds = time.monotonic()-tick
        result['samples'].append(dict(at_utc=utc(), elapsed_seconds=time.monotonic()-start,
            read_seconds=seconds, memory=memory, thread_deltas=sorted(changes, key=lambda d:d['minor'], reverse=True),
            new_thread_count=len(set(current)-set(previous)), exited_thread_count=len(set(previous)-set(current))))
        assert seconds <= 0.25, 'read overhead guard'
        previous = current
        if n % 15 == 0:
            identity()
            result['guards'].append(guard())
    result['identity_after'] = identity()
    result['guards'].append(guard())
    result['status'] = 'completed'
except Exception as exc:
    result['status'] = 'aborted'
    result['error_type'] = type(exc).__name__
result['finished_at_utc'] = utc()
result['elapsed_seconds'] = time.monotonic()-start
print(json.dumps(result, separators=(',', ':')))
raise SystemExit(0 if result['status'] == 'completed' else 2)

"""Bounded kernel counters and open-file categories; no process-memory reads.

FD categories are process-wide, not attributed to a thread. Correlation does
not establish allocation ownership. Nothing from fd names is exported.
"""
import json
import os
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PID = 2383603
TID = 2383615
START = 727482204
TSTART = 727482286
INVOCATION = '25e853ed5afe41bbabb2a7a1d6f380a6'
DURATION = 135


def utc():
    return datetime.now(timezone.utc).isoformat()


def fields(path):
    return path.read_text().rsplit(')', 1)[1].split()


def identity():
    props = dict(line.split('=', 1) for line in subprocess.check_output(
        ['systemctl', 'show', 'arbitrage-private', '-p', 'MainPID', '-p', 'InvocationID'],
        text=True, timeout=5).splitlines())
    master = int(props['MainPID'])
    assert props['InvocationID'] == INVOCATION
    assert Path(f'/proc/{master}/task/{master}/children').read_text().split() == [str(PID)]
    assert int(fields(Path(f'/proc/{PID}/stat'))[19]) == START
    assert int(fields(Path(f'/proc/{PID}/task/{TID}/stat'))[19]) == TSTART
    return dict(master=master, worker=PID, start_ticks=START, invocation=INVOCATION,
                history_thread=TID, thread_start_ticks=TSTART)


def guard():
    pressure = dict(word.split('=') for word in Path('/proc/pressure/memory').read_text().splitlines()[0].split()[1:])
    available = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))) * 1024
    assert float(pressure['avg10']) <= 5 and available >= 512*1024**2, 'resource guard'
    prefix = "SET SESSION time_zone='SYSTEM'; SET SESSION TRANSACTION READ ONLY; SET SESSION max_execution_time=3000; "
    query = "SELECT TIMESTAMPDIFF(SECOND,MAX(captured_at),UTC_TIMESTAMP()) FROM latest_market_snapshot UNION ALL SELECT TIMESTAMPDIFF(SECOND,MAX(captured_at),UTC_TIMESTAMP()) FROM latest_dual_futures_snapshot;"
    ages = list(map(int, subprocess.check_output(['mysql', '-N', '-B', '--database=arbitrage_hub', '-e', prefix+query], text=True, timeout=5).split()))
    assert len(ages) == 2 and max(ages) <= 30 and min(ages) >= 0, 'quote age guard'
    return dict(at_utc=utc(), host_pressure_avg10=float(pressure['avg10']), available_bytes=available, quote_age_seconds=ages)


def fd_categories():
    total = Counter()
    for fd in Path(f'/proc/{PID}/fd').iterdir():
        try:
            target = os.readlink(fd)
        except FileNotFoundError:
            continue
        if '/spread-history/' not in target:
            continue
        name = target.rsplit('/', 1)[-1]
        base = name.removesuffix('-wal').removesuffix('-shm')
        category = ('day' if re.fullmatch(r'\d{4}-\d{2}-\d{2}\.sqlite3', base) else
                    'activity' if base == 'opportunity-episodes.sqlite3' else
                    'validation' if 'validation' in base else 'other_history')
        suffix = 'wal' if name.endswith('-wal') else 'shm' if name.endswith('-shm') else 'db'
        total[category+'_'+suffix] += 1
    return dict(total)


def sample():
    proc = fields(Path(f'/proc/{PID}/stat'))
    thread = fields(Path(f'/proc/{PID}/task/{TID}/stat'))
    assert int(proc[19]) == START and int(thread[19]) == TSTART
    io = {key: int(value) for key, value in (line.split(': ', 1) for line in Path(f'/proc/{PID}/task/{TID}/io').read_text().splitlines())}
    return dict(at_utc=utc(), rss_bytes=int(proc[21])*os.sysconf('SC_PAGE_SIZE'),
                process_minor=int(proc[7]), history_minor=int(thread[7]),
                history_cpu_ticks=int(thread[11])+int(thread[12]),
                history_read_chars=io['rchar'], history_write_chars=io['wchar'],
                history_read_syscalls=io['syscr'], history_write_syscalls=io['syscw'],
                open_fd_categories=fd_categories())



def all_threads():
    out={}
    for task in Path(f'/proc/{PID}/task').iterdir():
        try:
            row=fields(task/'stat')
            out[int(task.name)]=(int(row[19]),int(row[7]),int(row[11])+int(row[12]))
        except FileNotFoundError:
            pass
    return out

def precise_memory():
    out={}
    for line in Path(f'/proc/{PID}/smaps_rollup').read_text().splitlines()[1:]:
        key,_,val=line.partition(':')
        if key in ('Rss','Anonymous','Private_Dirty','Swap'):
            out[key+'_bytes']=int(val.split()[0])*1024
    proc=fields(Path(f'/proc/{PID}/stat'))
    out['stat_rss_bytes']=int(proc[21])*os.sysconf('SC_PAGE_SIZE')
    out['process_minor']=int(proc[7])
    return out

result=dict(started_at_utc=utc(),identity=identity(),guards=[guard()],samples=[],
    boundary='Kernel metadata only. smaps_rollup gives more precise residency than batched stat RSS. One-second overlap is correlation, not malloc ownership. No process-memory attach or contents.')
start=time.monotonic();previous=all_threads();before=precise_memory()
result['baseline']=dict(at_utc=utc(),memory=before,threads=len(previous))
try:
    for n in range(1,131):
        time.sleep(max(0,start+n-time.monotonic()))
        tick=time.monotonic()
        current=all_threads();memory=precise_memory()
        changes=[]
        for tid,(stamp,fault,cpu) in current.items():
            old=previous.get(tid)
            if old and old[0]==stamp:
                delta=fault-old[1];cput=cpu-old[2]
                if delta or cput:
                    changes.append(dict(tid=tid,start_ticks=stamp,minor=delta,cpu_ticks=cput))
        result['samples'].append(dict(at_utc=utc(),elapsed_seconds=time.monotonic()-start,
            read_seconds=time.monotonic()-tick,memory=memory,
            thread_deltas=sorted(changes,key=lambda d:d['minor'],reverse=True),
            new_thread_count=len(set(current)-set(previous)),exited_thread_count=len(set(previous)-set(current))))
        previous=current
        if n%15==0:
            identity();result['guards'].append(guard())
    result['identity_after']=identity();result['status']='completed'
except Exception as exc:
    result['status']='aborted';result['error_type']=type(exc).__name__
result['finished_at_utc']=utc();result['elapsed_seconds']=time.monotonic()-start
print(json.dumps(result,separators=(',',':')))

"""One bounded kernel-metadata comparison. Never attach or read process memory.

Only mapping totals, thread fault/CPU counters and process identity leave the host.
Mapping addresses and pathnames are not printed or saved.
"""
import json
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

EXPECTED_INVOCATION = '25e853ed5afe41bbabb2a7a1d6f380a6'
EXPECTED_WORKER = 2383603


def now():
    return datetime.now(timezone.utc).isoformat()


def identity():
    props = dict(line.split('=', 1) for line in subprocess.check_output(
        ['systemctl', 'show', 'arbitrage-private', '-p', 'MainPID', '-p', 'InvocationID'],
        text=True).splitlines())
    parent = int(props['MainPID'])
    children = list(map(int, Path(f'/proc/{parent}/task/{parent}/children').read_text().split()))
    assert props['InvocationID'] == EXPECTED_INVOCATION and children == [EXPECTED_WORKER]
    stat = Path(f'/proc/{EXPECTED_WORKER}/stat').read_text().rsplit(')', 1)[1].split()
    return {'master': parent, 'worker': EXPECTED_WORKER, 'invocation': props['InvocationID'],
            'start_ticks': int(stat[19])}


def mapping_totals():
    maps = []
    for line in Path(f'/proc/{EXPECTED_WORKER}/smaps').read_text().splitlines():
        if re.match(r'^[0-9a-f]+-[0-9a-f]+ ', line):
            parts = line.split(None, 5)
            start, end = (int(x, 16) for x in parts[0].split('-'))
            maps.append({'start': start, 'end': end, 'perms': parts[1],
                         'name': parts[5] if len(parts) > 5 else '', 'rss_kib': 0,
                         'private_dirty_kib': 0, 'anonymous_kib': 0})
        elif maps:
            key = line.split(':', 1)[0]
            out = {'Rss': 'rss_kib', 'Private_Dirty': 'private_dirty_kib',
                   'Anonymous': 'anonymous_kib'}.get(key)
            if out:
                maps[-1][out] = int(line.split()[1])
    totals = {}
    for i, row in enumerate(maps):
        next_row = maps[i + 1] if i + 1 < len(maps) else {}
        # This is a mapping-shape heuristic, not an allocator heap-content read.
        arena_shape = (not row['name'] and row['perms'] == 'rw-p'
                       and row['start'] % (64 * 1024**2) == 0
                       and next_row.get('start') == row['end']
                       and not next_row.get('name') and next_row.get('perms') == '---p'
                       and next_row['end'] - row['start'] == 64 * 1024**2)
        category = ('main_heap' if row['name'] == '[heap]' else
                    'allocator_64m_shape' if arena_shape else
                    'anonymous_writable_other' if not row['name'] and row['perms'] == 'rw-p' else
                    'anonymous_reserved' if not row['name'] else 'file_or_special')
        total = totals.setdefault(category, Counter())
        total.update({key: row[key] for key in ('rss_kib', 'private_dirty_kib', 'anonymous_kib')})
        total.update({'count': 1, 'virtual_kib': (row['end'] - row['start']) // 1024})
    return {k: dict(v) for k, v in totals.items()}


def thread_counters():
    result = {}
    for folder in Path(f'/proc/{EXPECTED_WORKER}/task').iterdir():
        try:
            fields = (folder / 'stat').read_text().rsplit(')', 1)[1].split()
            result[int(folder.name)] = {
                'start_ticks': int(fields[19]), 'minor_faults': int(fields[7]),
                'major_faults': int(fields[9]), 'user_ticks': int(fields[11]),
                'system_ticks': int(fields[12]),
            }
        except FileNotFoundError:
            continue
    return result


r = {'started_at_utc': now(), 'identity_before': identity(),
     'limitations': ['Mapping categories are kernel metadata heuristics, not heap contents.',
                    'Minor faults show the thread touching pages, not object ownership or net retention.',
                    'Threads that start and exit between polls can be absent.']}
start = time.monotonic()
previous = thread_counters()
baseline_tids = set(previous)
thread_totals = {}
gone = set()
seen_new = set()
r['samples'] = [{'elapsed': 0, 'at_utc': now(), 'maps': mapping_totals(), 'threads': len(previous)}]
for round_id in range(1, 7):
    time.sleep(max(0, start + 10 * round_id - time.monotonic()))
    identity()
    current = thread_counters()
    gone.update(set(previous) - set(current))
    seen_new.update(set(current) - baseline_tids)
    for tid, values in current.items():
        old = previous.get(tid)
        if old is None or old['start_ticks'] != values['start_ticks']:
            continue
        row = thread_totals.setdefault((tid, values['start_ticks']), Counter())
        row.update({key: values[key] - old[key] for key in values if key != 'start_ticks'})
    previous = current
    r['samples'].append({'elapsed': round(time.monotonic() - start, 3), 'at_utc': now(),
                         'maps': mapping_totals(), 'threads': len(current)})
r['thread_deltas'] = [dict(tid=tid, start_ticks=ticks, **values)
                      for (tid, ticks), values in sorted(thread_totals.items(),
                          key=lambda item: item[1]['minor_faults'], reverse=True)]
r['new_threads_observed'] = len(seen_new)
r['exited_threads_observed'] = len(gone)
r['identity_after'] = identity()
assert r['identity_before'] == r['identity_after']
r['finished_at_utc'] = now()
r['elapsed_seconds'] = round(time.monotonic() - start, 3)
print(json.dumps(r, separators=(',', ':')))

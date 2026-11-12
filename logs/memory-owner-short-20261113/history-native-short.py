"""Fixed-count synthetic history phase control, never import app or its DB.

Three fresh child processes use the same generated SQLite. The profile arm
executes pinned production code; touching only the synthetic file simulates
the current-day cache invalidation caused by ongoing writes. No GC or trim.
"""
import argparse
import ctypes
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import resource
import sqlite3
import subprocess
import sys
import time
import gc
import _sqlite3
from datetime import datetime, timezone

EXPECTED = {
    'spread_history.py': 'fbf165aeb763e6a9a788f17c1ff755ce56d0eac15baf8939551086d8f992b3a8',
    'opportunity_history.py': '0ea42d46db6c23f43950cbc7d654330984225d5de73bfd57d7157f4e55efabe4',
    'opportunity_pool.py': '6b929eb655de812d09e9a2ddf993c2b5b46c3b9478a2fac7add38c3ecd6e9dad',
}
NOW = 1794506400+3595
FIRST = NOW//3600*3600-3*3600


def utc():
    return datetime.now(timezone.utc).isoformat()


def load_sources(source):
    sys.dont_write_bytecode = True
    for name, expected in EXPECTED.items():
        assert hashlib.sha256((source/name).read_bytes()).hexdigest() == expected
    for name in ('spread_history', 'opportunity_pool', 'opportunity_history'):
        spec = importlib.util.spec_from_file_location(name, source/(name+'.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules['spread_history'], sys.modules['opportunity_history']


class MallInfo(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in
               ('arena','ordblks','smblks','hblks','hblkhd','usmblks','fsmblks','uordblks','fordblks','keepcost')]


def readers():
    try:
        mall = ctypes.CDLL(None).mallinfo2
        mall.restype = MallInfo
        mall.argtypes = []
    except AttributeError:
        mall = None
    try:
        sql = ctypes.CDLL(_sqlite3.__file__)
        used, high = sql.sqlite3_memory_used, sql.sqlite3_memory_highwater
        used.restype = high.restype = ctypes.c_int64
        used.argtypes = []
        high.argtypes = [ctypes.c_int]
    except AttributeError:
        used = high = None
    def measure():
        out = dict(at_utc=utc(), python_blocks=sys.getallocatedblocks(), gc_counts=gc.get_count())
        if Path('/proc/self/smaps_rollup').exists():
            rows = dict(line.split(':', 1) for line in Path('/proc/self/smaps_rollup').read_text().splitlines()[1:] if ':' in line)
            out.update(rss_bytes=int(rows['Rss'].split()[0])*1024,
                       anonymous_bytes=int(rows['Anonymous'].split()[0])*1024)
        if mall:
            info = mall()
            out.update(malloc_used=info.uordblks, malloc_free=info.fordblks,
                       malloc_mapped=info.hblkhd, malloc_arena=info.arena)
        if used:
            out.update(sqlite_live=used(), sqlite_high=high(0))
        return out
    return measure


def point_value(i, hour, minute, j, metric):
    return (i % 17)/32+hour/8+minute/1024+j/4096+(.125 if metric=='close' else 0)


def canonical(data):
    return json.dumps([[list(key), value] for key, value in data.items()], sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def verify_profile(cache, count):
    data, at = cache.snapshot()
    assert at == NOW and len(data) == count
    for i in range(count):
        low, high = point_value(i,0,0,0,'open'), point_value(i,3,59,3,'open')
        buckets = [point_value(i,h,m,3,'open') for h in range(4) for m in range(4,60,5)]
        closes = [value+.125 for value in buckets]
        windows = {}
        for name, seconds in (('24h',86400),('3d',259200),('7d',604800),('15d',1296000)):
            windows[name] = dict(low=low, high=high, low_at=FIRST, high_at=NOW,
                                 first_at=FIRST, last_at=NOW, bins=240,
                                 range=high-low, max_abs=high, coverage=240/(seconds/60), peak_at=NOW)
        expected = dict(computed_at=NOW, ready=True, close_ready=True, first_at=FIRST,
                        last_at=NOW, bins=48, median=buckets[24], low=low, high=high,
                        range=high-low, max_abs=high, exit_q75=closes[36], windows=windows)
        assert data[(f'SYN{i:04d}/USDT','Gate')] == expected, 'full profile oracle mismatch'
    return hashlib.sha256(canonical(data)).hexdigest()


def child(args, spread, history):
    measure = readers()
    store = spread.HistoryStore(args.root/'history')
    cache = history.ProfileCache()
    db = next(store.directory.glob('*.sqlite3'))
    report = dict(arm=args.arm, started_at_utc=utc(), baseline=measure(), samples=[])
    expected_digest = None
    for n in range(args.rounds):
        # Only this synthetic DB's metadata; no rows or source code change.
        ns = time.time_ns()+n
        os.utime(db, ns=(ns,ns))
        begin = time.perf_counter()
        if args.arm == 'profile':
            count = cache.refresh(store,NOW)
            digest = verify_profile(cache,args.paths)
            expected_digest = expected_digest or digest
            assert digest == expected_digest
        else:
            connection = sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)
            count = 0
            try:
                connection.execute('PRAGMA query_only=ON')
                connection.execute('PRAGMA cache_size=-2048')
                for (blob,) in connection.execute("SELECT payload FROM history ORDER BY symbol,exchange,metric,hour_at"):
                    if args.arm == 'decode':
                        decoded = spread.decode_chunk(blob)
                        assert len(decoded) == 60
                        del decoded
                    count += 1
                del blob
            finally:
                connection.close()
            assert count == args.paths*8
            digest = None
        sample = measure()
        sample.update(round=n, rows_or_profiles=count, elapsed_seconds=time.perf_counter()-begin,
                      output_sha256=digest, cached_paths=sum(len(v[1]) for v in cache.days.values()))
        report['samples'].append(sample)
    cache = None
    report['after_release'] = measure()
    report['finished_at_utc'] = utc()
    print(json.dumps(report))


def main(args, spread, history):
    assert not args.root.exists(), 'Fresh private synthetic directory required'
    args.root.mkdir(mode=0o700, parents=False)
    store = spread.HistoryStore(args.root/'history')
    c = store.connect(store.day(NOW),writable=True)
    started = utc()
    try:
        with c:
            for i in range(args.paths):
                for hour in range(4):
                    t = FIRST+hour*3600
                    for metric in ('open','close'):
                        data = {str(t+m*60): [[[t+m*60+second,point_value(i,hour,m,j,metric)]
                                              for j,second in enumerate((0,5,25,55))]] for m in range(60)}
                        c.execute('INSERT INTO history VALUES(?,?,?,?,?)',
                                  (f'SYN{i:04d}/USDT','Gate',metric,t,spread.encode_chunk(data)))
        assert c.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    finally:
        c.close()
    db = next(store.directory.glob('*.sqlite3'))
    original = hashlib.sha256(db.read_bytes()).hexdigest()
    report = dict(started_at_utc=started, source_sha256=EXPECTED, python=sys.version,
                  sqlite_version=sqlite3.sqlite_version, paths=args.paths, rows=args.paths*8,
                  points_per_hour=240, rounds=args.rounds, generated_db_bytes=db.stat().st_size,
                  generated_db_sha256=original, gc_enabled=gc.isenabled(), arms=[],
                  boundary='Fresh synthetic processes, no website attach/import/restart/forced-GC/trim. Separate phases and fixed inputs; not proof of production allocator ownership.')
    for arm in ('rows','decode','profile'):
        command = [sys.executable, str(Path(__file__).resolve()), '--source',str(args.source),
                   '--root',str(args.root),'--paths',str(args.paths),'--rounds',str(args.rounds),'--arm',arm]
        proc = subprocess.run(command,capture_output=True,timeout=60)
        (args.root/(arm+'.stdout')).write_bytes(proc.stdout)
        (args.root/(arm+'.stderr')).write_bytes(proc.stderr)
        assert proc.returncode==0 and not proc.stderr, 'synthetic child failed: '+arm
        report['arms'].append(json.loads(proc.stdout))
    assert hashlib.sha256(db.read_bytes()).hexdigest() == original
    report['source_unchanged'] = True
    report['finished_at_utc'] = utc()
    raw=json.dumps(report,indent=2)+'\n'
    (args.root/'report.json').write_text(raw)
    print(raw)


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--paths',type=int,default=1171)
    p.add_argument('--rounds',type=int,default=3)
    p.add_argument('--arm',choices=('rows','decode','profile'))
    args=p.parse_args()
    assert 1 <= args.paths <= 1171 and 1 <= args.rounds <= 3
    os.umask(0o077)
    if sys.platform.startswith('linux'):
        os.nice(19)
        resource.setrlimit(resource.RLIMIT_AS,(256*1024**2,256*1024**2))
        resource.setrlimit(resource.RLIMIT_CPU,(45,45))
    spread,history=load_sources(args.source.resolve())
    (child if args.arm else main)(args,spread,history)

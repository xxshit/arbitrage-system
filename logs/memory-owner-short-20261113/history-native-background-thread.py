"""Synthetic phase attribution with all value validation in a forked child.

Parent counters are captured immediately after each actual phase. Full-value
JSON serialization and oracle construction run only in a separate validator,
after all three measurements. Production is never attached or imported.
"""
import importlib.util
import json
import os
from pathlib import Path
import resource
import sys
import time
import weakref
import hashlib

here=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('native_base',here/'history-native-short.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
assert sys.platform.startswith('linux')
os.umask(0o077);os.nice(19)
resource.setrlimit(resource.RLIMIT_AS,(256*1024**2,256*1024**2))
resource.setrlimit(resource.RLIMIT_CPU,(45,45))
arm=sys.argv[1]
assert arm=='full'
root=Path(sys.argv[2]).resolve()
assert root.name=='synthetic' and root.parent.name=='arbiscope-history-native-short-v2.4hoz_tuk'
spread,history=base.load_sources(Path('/opt/arbitrage-hub'))
store=spread.HistoryStore(root/'history')
database=next(store.directory.glob('*.sqlite3'))
def database_sha():
    digest=hashlib.sha256()
    with database.open('rb') as stream:
        for block in iter(lambda:stream.read(65536),b''):
            digest.update(block)
    return digest.hexdigest()
assert database_sha()==sys.argv[3]
count=1171
cache=history.ProfileCache()
if arm=='rolling':
    cache.window_ranges=lambda store,now:{}
measure=base.readers()
report=dict(arm=arm,started_at_utc=base.utc(),baseline=measure(),samples=[],
            source_sha256=base.EXPECTED,oracle_in_measured_process=False)
import threading
ready=threading.Event(); release=threading.Event(); errors=[]
def measured_work():
    for n in range(3):
        ns=time.time_ns()+n
        os.utime(database,ns=(ns,ns))
        begin=time.perf_counter()
        if arm=='windows':
            cache.data=cache.window_ranges(store,base.NOW)
            cache.at=base.NOW
        else:
            cache.refresh(store,base.NOW)
        sample=measure()
        sample.update(round=n,elapsed_seconds=time.perf_counter()-begin,
                      profiles=len(cache.data),cached_paths=sum(len(v[1]) for v in cache.days.values()))
        report['samples'].append(sample)

def target():
    try:
        report['measured_native_thread']=threading.get_native_id()
        measured_work()
    except BaseException as exc:
        errors.append(type(exc).__name__)
    finally:
        ready.set()
        release.wait(5)
worker=threading.Thread(target=target,name='synthetic-history-worker')
worker.start()
assert ready.wait(40),'worker deadline'
assert not errors, errors
report['worker_alive_after_last_measurement']=worker.is_alive()
report['after_rounds_worker_alive']=measure()
release.set();worker.join(5)
assert not worker.is_alive()
report['after_natural_worker_exit']=measure()


def expected(i):
    low=base.point_value(i,0,0,0,'open');high=base.point_value(i,3,59,3,'open')
    bins=[base.point_value(i,h,m,3,'open') for h in range(4) for m in range(4,60,5)]
    windows={name:dict(low=low,high=high,low_at=base.FIRST,high_at=base.NOW,
                      first_at=base.FIRST,last_at=base.NOW,bins=240,
                      range=high-low,max_abs=high,coverage=240/(seconds/60),peak_at=base.NOW)
             for name,seconds in history.WINDOWS.items()}
    if arm=='windows':
        return windows
    value=dict(computed_at=base.NOW,ready=True,close_ready=True,first_at=base.FIRST,
               last_at=base.NOW,bins=48,median=bins[24],low=low,high=high,
               range=high-low,max_abs=high,exit_q75=bins[36]+.125)
    if arm=='full':
        value['windows']=windows
    return value


validator_path=here/(arm+'-validator.json')
assert not validator_path.exists()
pid=os.fork()
if pid==0:
    try:
        assert cache.at==base.NOW and len(cache.data)==count
        for i in range(count):
            assert cache.data[(f'SYN{i:04d}/USDT','Gate')]==expected(i),'full-value mismatch'
        validator_path.write_text(json.dumps(dict(status='pass',profiles=count,
            output_sha256=hashlib.sha256(base.canonical(cache.data)).hexdigest(),
            pid=os.getpid(),parent_pid=os.getppid(),completed_at_utc=base.utc()))+'\n')
        os._exit(0)
    except BaseException:
        os._exit(1)
_,status=os.waitpid(pid,0)
assert os.waitstatus_to_exitcode(status)==0,'separate value validator failed'
report['validation']=json.loads(validator_path.read_text())
assert report['validation']['pid']!=os.getpid()
report['parent_pid']=os.getpid()
report['after_validation']=measure()
# Weak references are added only after measured rounds, never force collection.
refs=[weakref.ref(a) for _,day in cache.days.values() for a in day.values()]
report['day_arrays_before_release']=len(refs)
cache=None
report['day_arrays_after_release']=sum(ref() is not None for ref in refs)
del refs
report['after_release']=measure()
assert report['day_arrays_after_release']==0
report['source_unchanged']=database_sha()==sys.argv[3]
assert report['source_unchanged']
report['finished_at_utc']=base.utc()
print(json.dumps(report))

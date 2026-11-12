"""Dispatch one bounded isolated diagnostic; never attach or mutate production."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
mode = sys.argv[1]
assert mode in ('shape', 'replay', 'handoff', 'objects', 'release', 'final')
source = (ROOT/f'metadata-{mode}.py').read_bytes()
args = [] if mode == 'shape' else [sys.argv[2]]
bootstrap = r'''
import base64,hashlib,json,os,signal,shutil,subprocess,tempfile,time
from pathlib import Path
from datetime import datetime,timezone
source=base64.b64decode('__PAYLOAD__')
assert hashlib.sha256(source).hexdigest()=='__SHA__'
mode=__MODE__
args=__ARGS__
def guard():
 pressure=float(dict(w.split('=') for w in Path('/proc/pressure/memory').read_text().splitlines()[0].split()[1:])['avg10'])
 available=int(next(l.split()[1] for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:')))*1024
 prefix="SET SESSION time_zone='SYSTEM'; SET SESSION TRANSACTION READ ONLY; SET SESSION max_execution_time=3000; "
 query="SELECT TIMESTAMPDIFF(SECOND,MAX(captured_at),UTC_TIMESTAMP()) FROM latest_market_snapshot UNION ALL SELECT TIMESTAMPDIFF(SECOND,MAX(captured_at),UTC_TIMESTAMP()) FROM latest_dual_futures_snapshot;"
 ages=list(map(int,subprocess.check_output(['mysql','-N','-B','--database=arbitrage_hub','-e',prefix+query],text=True,timeout=5).split()))
 return dict(at_utc=datetime.now(timezone.utc).isoformat(),psi=pressure,available_bytes=available,quote_ages=ages,ok=pressure<=5 and available>=768*1024**2 and len(ages)==2 and 0<=min(ages)<=max(ages)<=30)
root=Path(tempfile.mkdtemp(prefix='arbiscope-metadata-'+mode+'.'))
os.chmod(root,0o700)
report={'root':str(root),'guards':[guard()],'script_sha256':hashlib.sha256(source).hexdigest()}
if not report['guards'][0]['ok'] or shutil.disk_usage('/').free<2*1024**3:
 report['status']='preflight_guard_abort';print(json.dumps(report));(root/'dispatch.json').write_text(json.dumps(report,indent=2));raise SystemExit(0)
script=root/('metadata-'+mode+'.py');script.write_bytes(source)
with (root/'stdout').open('xb') as out,(root/'stderr').open('xb') as err:
 proc=subprocess.Popen(['/opt/arbitrage-hub/.venv/bin/python',str(script),str(root/'fixtures') if mode=='shape' else args[0]],stdout=out,stderr=err,start_new_session=True)
 start=time.monotonic();deadline=start+(45 if mode=='shape' else 90);abort=None
 try:
  while proc.poll() is None:
   time.sleep(3)
   if time.monotonic()>deadline:abort='wall_deadline'
   check=guard();report['guards'].append(check)
   if not check['ok']:abort='resource_or_quote_guard'
   if abort and proc.poll() is None:
    os.killpg(proc.pid,signal.SIGTERM);proc.wait(timeout=3);break
 except Exception as exc:
  abort=type(exc).__name__
  if proc.poll() is None:os.killpg(proc.pid,signal.SIGTERM);proc.wait(timeout=3)
 report.update(status='aborted' if abort else 'finished',abort=abort,exit_code=proc.wait(),elapsed_seconds=time.monotonic()-start,stderr_bytes=(root/'stderr').stat().st_size)
 if proc.returncode==0 and not report['stderr_bytes']:
  report['result']=json.loads((root/'stdout').read_text())
 else: report['failure_detail']=(root/'stderr').read_text()[-1800:]
(root/'dispatch.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
'''.replace('__PAYLOAD__',base64.b64encode(source).decode()).replace('__SHA__',hashlib.sha256(source).hexdigest()).replace('__MODE__',repr(mode)).replace('__ARGS__',repr(args))
with (ROOT/f'metadata-{mode}-dispatch.stdout').open('xb') as out, (ROOT/f'metadata-{mode}-dispatch.stderr').open('xb') as err:
    proc = subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','lewan-game','python3 -'], input=bootstrap.encode(), stdout=out, stderr=err, timeout=110)
summary = {'ssh_exit':proc.returncode,'stderr_bytes':(ROOT/f'metadata-{mode}-dispatch.stderr').stat().st_size}
if proc.returncode == 0:
    result = json.loads((ROOT/f'metadata-{mode}-dispatch.stdout').read_text())
    (ROOT/f'metadata-{mode}-dispatch.json').write_text(json.dumps(result,indent=2)+'\n')
    summary.update({k:v for k,v in result.items() if k not in ('result','guards')})
    if 'result' in result: (ROOT/f'metadata-{mode}-result.json').write_text(json.dumps(result['result'],indent=2)+'\n')
print(json.dumps(summary))
raise SystemExit(proc.returncode)

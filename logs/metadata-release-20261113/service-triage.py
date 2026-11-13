from pathlib import Path
from datetime import datetime, timezone
import subprocess,json,re
services=('arbitrage-private','mysql','lewan-game')
r={'captured_at_utc':datetime.now(timezone.utc).isoformat(),'services':{},'events':[],'kernel_oom':[]}
for n in services:
 r['services'][n]=dict(x.split('=',1) for x in subprocess.check_output(['systemctl','show',n,'-p','MainPID','-p','ActiveState','-p','SubState','-p','Result','-p','InvocationID','-p','ExecMainCode','-p','ExecMainStatus','-p','ActiveEnterTimestamp','-p','InactiveEnterTimestamp','-p','StateChangeTimestamp'],text=True).splitlines())
cmd=['journalctl','--since','2026-11-12 19:00:00 UTC','--no-pager','-o','json','-u','arbitrage-private','-u','mysql','-u','lewan-game']
proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,text=True)
counts={}; health=[]
for line in proc.stdout:
 try:d=json.loads(line)
 except ValueError:continue
 m=d.get('MESSAGE','');unit=d.get('_SYSTEMD_UNIT','');pid=d.get('_PID','')
 if not isinstance(m,str):continue
 for k in ('WORKER TIMEOUT','Traceback','background_worker_retry','[ERROR]','oom-kill'):
  if k in m: counts[unit+':'+k]=counts.get(unit+':'+k,0)+1
 if pid=='1' and re.search(r'(Stopped |Stopping |Started |Starting |Main process exited|Failed with result|Deactivated successfully|A process of this unit has been killed|Consumed )',m):
  r['events'].append({'utc_us':d.get('__REALTIME_TIMESTAMP'),'message':m[:700]})
 if 'Booting worker with pid:' in m:
  r['events'].append({'utc_us':d.get('__REALTIME_TIMESTAMP'),'boot_worker':re.findall(r'pid: (\d+)',m)})
 if 'runtime_health ' in m:
  try:h=json.loads(m.split('runtime_health ',1)[1]);health.append({'utc_us':d.get('__REALTIME_TIMESTAMP'),'health':h});health=health[-2:]
  except ValueError:pass
if proc.wait()!=0:raise RuntimeError('journal_exit')
r['error_tag_counts']=counts;r['last_health']=health
proc=subprocess.Popen(['journalctl','-k','--since','2026-11-12 19:00:00 UTC','--no-pager','-o','json'],stdout=subprocess.PIPE,text=True)
for line in proc.stdout:
 try:d=json.loads(line)
 except ValueError:continue
 m=d.get('MESSAGE','')
 if isinstance(m,str) and ('Out of memory: Killed process' in m or 'Memory cgroup out of memory: Killed process' in m):
  r['kernel_oom'].append({'utc_us':d.get('__REALTIME_TIMESTAMP'),'message':m[:900]})
if proc.wait()!=0:raise RuntimeError('kernel_journal_exit')
r['uptime_seconds']=float(Path('/proc/uptime').read_text().split()[0])
r['meminfo']={l.split(':')[0]:l.split(':')[1].strip() for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith(('MemTotal:','MemAvailable:','SwapTotal:','SwapFree:'))}
print(json.dumps(r,sort_keys=True))

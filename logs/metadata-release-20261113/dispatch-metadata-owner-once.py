"""Upload the exact reviewed feature release, never continuation or credentials."""
from pathlib import Path
import base64, hashlib, json, subprocess

HERE=Path(__file__).resolve().parent
stage=HERE/'release-payload'
manifest=json.loads((stage/'manifest.json').read_text())
files={f'next/{n}':stage/'next'/n for n in manifest['next_source']}
files.update({'manifest.json':stage/'manifest.json','publish-metadata-once.py':stage/'publish-metadata-once.py'})
payload={n:{'body':base64.b64encode(p.read_bytes()).decode(),'sha':hashlib.sha256(p.read_bytes()).hexdigest()} for n,p in files.items()}
remote=r'''
from pathlib import Path
import tempfile,os,base64,hashlib,json,subprocess
os.umask(0o077)
root=Path(tempfile.mkdtemp(prefix='arbiscope-release-metadata-owner.',dir='/root'));root.chmod(0o700)
payload=PAYLOAD
assert len(payload)==3 and 'manifest.json' in payload and 'publish-metadata-once.py' in payload
for n,v in payload.items():
 assert not Path(n).is_absolute() and '..' not in Path(n).parts
 b=base64.b64decode(v['body']);assert hashlib.sha256(b).hexdigest()==v['sha'];p=root/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b)
print(json.dumps({'event':'prepared','root':str(root)}),flush=True)
with (root/'publish.stdout').open('xb') as out,(root/'publish.stderr').open('xb') as err:
 p=subprocess.run(['/opt/arbitrage-hub/.venv/bin/python','-B',str(root/'publish-metadata-once.py')],stdout=out,stderr=err,timeout=660)
r={'root':str(root),'exit':p.returncode,'files':{}}
for name in ('before.json','pre-stop.json','stopped-before.json','stopped-after.json','after.json','backup-check.json','https-check.json','old-worker-last-health.json','new-worker-health.json','result.json','failure.json','publish.stdout'):
 f=root/name
 if f.exists():
  assert f.stat().st_size<250000
  b=f.read_bytes();r['files'][name]={'sha':hashlib.sha256(b).hexdigest(),'base64':base64.b64encode(b).decode()}
r['stderr_bytes']=(root/'publish.stderr').stat().st_size
print(json.dumps(r),flush=True)
'''
(HERE/'dispatch-metadata-owner-once.lock').open('x').close()
with (HERE/'dispatch.stdout').open('x') as out,(HERE/'dispatch.stderr').open('x') as err:
 p=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','lewan-game','python3','-'],input=remote.replace('PAYLOAD',repr(payload)),text=True,stdout=out,stderr=err,timeout=700)
print({'ssh_exit':p.returncode})
if p.returncode:raise SystemExit(p.returncode)
r=json.loads((HERE/'dispatch.stdout').read_text().splitlines()[-1]);dest=HERE/'release';dest.mkdir(mode=0o700)
for n,v in r['files'].items():
 b=base64.b64decode(v.pop('base64'));assert hashlib.sha256(b).hexdigest()==v['sha'];(dest/n).write_bytes(b)
(dest/'collection.json').write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps({k:v for k,v in r.items() if k!='files'}))
if (dest/'result.json').exists():
 v=json.loads((dest/'result.json').read_text());print(json.dumps({k:v.get(k) for k in ('status','completed_at_utc','changed_files','identity','disk_free_bytes')}))
if (dest/'failure.json').exists():print((dest/'failure.json').read_text())
if r['exit']:print((dest/'publish.stdout').read_text())
raise SystemExit(r['exit'])

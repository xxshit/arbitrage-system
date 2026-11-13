"""Exercise extracted protection helpers with isolated fixtures; never main()."""
import ast,copy,hashlib,json,os,types,sys,tempfile,unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
here=Path(__file__).resolve().parent;file=here/'release-payload/publish-metadata-once.py'
s=file.read_text();tree=ast.parse(s);keep={'need','sha','exact_stopped_counts','check_protected','replace_file','restore_file','execution_disabled','narrow_change'}
module=ast.Module(body=[n for n in tree.body if isinstance(n,(ast.Import,ast.ImportFrom)) or isinstance(n,ast.FunctionDef) and n.name in keep],type_ignores=[])
ns={};exec(compile(module,str(file),'exec'),ns)
class Guards(unittest.TestCase):
 def test_exact_original_counts(self):
  a={'table_counts':{'history':1},'new_table_counts':{'intent':0}}
  for k in a:
   b=copy.deepcopy(a);b[k][next(iter(b[k]))]+=1
   with self.subTest(k=k),self.assertRaises(RuntimeError):ns['exact_stopped_counts'](a,b)
  ns['exact_stopped_counts'](a,a)
 def test_each_protected_value_and_service_identity(self):
  keys=('private_hashes','protected_hashes','new_table_counts','refresh_rules','index_digest','db_identity','site_limits','table_counts')
  a={k:{'sentinel':1} for k in keys};a['identity']={'services':{'mysql':{'MainPID':'1'},'lewan-game':{'MainPID':'2'}}}
  ns['check_protected'](a,a)
  for key in keys:
   b=copy.deepcopy(a);b[key]={'different':2}
   with self.subTest(key=key),self.assertRaises(RuntimeError):ns['check_protected'](a,b)
  for name in ('mysql','lewan-game'):
   b=copy.deepcopy(a);b['identity']['services'][name]['MainPID']='changed'
   with self.subTest(service=name),self.assertRaises(RuntimeError):ns['check_protected'](a,b)
 def test_flags_or_active_executor_never_pass(self):
  ns['APP']=Path('/not-opened')
  for env,site,executor,active in [(1,0,0,3),(0,1,0,3),(0,0,1,3),(0,0,0,0)]:
   with self.subTest(flags=(env,site,executor,active)),patch.dict(ns,{'dotenv_values':lambda *_:{'BINANCE_EXECUTION_ENABLED':str(env)}}),patch.object(ns['subprocess'],'check_output',side_effect=[f'BINANCE_EXECUTION_ENABLED={site}',f'BINANCE_EXECUTION_ENABLED={executor}']),patch.object(ns['subprocess'],'run',return_value=types.SimpleNamespace(returncode=active)),self.assertRaises(RuntimeError):ns['execution_disabled']()
 def test_existing_restore_new_removal_and_drift_rejection(self):
  digest=lambda b:hashlib.sha256(b).hexdigest()
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);app=root/'app';app.mkdir();previous=root/'previous';previous.mkdir()
   ns.update(APP=app,ROOT=root)
   owner=types.SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid())
   manifest={'next_source':{},'expected_source':{}}
   for name,old in [('old.py',b'old'),('new.py',None),('drift.py',b'old')]:
    (app/name).write_bytes(b'next' if name!='drift.py' else b'user edit')
    manifest['next_source'][name]=digest(b'next');manifest['expected_source'][name]=digest(old) if old else None
    if old:(previous/name).write_bytes(old)
   with patch.object(ns['pwd'],'getpwnam',return_value=owner):
    ns['restore_file']('old.py',manifest);ns['restore_file']('new.py',manifest)
    self.assertEqual((app/'old.py').read_bytes(),b'old');self.assertFalse((app/'new.py').exists())
    with self.assertRaises(RuntimeError):ns['restore_file']('drift.py',manifest)
    self.assertEqual((app/'drift.py').read_bytes(),b'user edit')
 def test_two_function_candidate_and_fixed_receipts(self):
  m=json.loads((here/'release-payload/manifest.json').read_text())
  base=(here.parent/'memory-code-audit-20261113/production-source/app.py').read_text()
  candidate=(here/'release-payload/next/app.py').read_text()
  ns['narrow_change'](base,candidate)
  self.assertEqual(set(m['next_source']),{'app.py'})
  self.assertEqual(hashlib.sha256(candidate.encode()).hexdigest(),m['next_source']['app.py'])
  self.assertFalse(m['live_orders_enabled']);self.assertEqual(m['short_checks']['unique_tests'],37)
  self.assertEqual(m['expected_identity']['services']['arbitrage-private']['MainPID'],'0')
  self.assertEqual(m['expected_identity']['services']['lewan-game']['ActiveState'],'failed')
  self.assertTrue(all(json.loads((here/'preflight-stopped.json').read_text())['original_protection_matches'].values()))
 def test_extra_global_or_function_change_rejected(self):
  base=(here.parent/'memory-code-audit-20261113/production-source/app.py').read_text()
  candidate=(here/'release-payload/next/app.py').read_text()
  for extra in ('\nEXTRA_MUTATION=1\n','\ndef unexpected(): pass\n'):
   with self.subTest(extra=extra),self.assertRaises(RuntimeError):ns['narrow_change'](base,candidate+extra)
  with self.assertRaises(RuntimeError):ns['narrow_change'](base,base)
 def test_bad_previous_hash_refuses_restore(self):
  digest=lambda b:hashlib.sha256(b).hexdigest()
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);app=root/'app';app.mkdir();(root/'previous').mkdir()
   (app/'app.py').write_bytes(b'next');(root/'previous/app.py').write_bytes(b'corrupt')
   ns.update(APP=app,ROOT=root)
   with self.assertRaises(RuntimeError):ns['restore_file']('app.py',{'next_source':{'app.py':digest(b'next')},'expected_source':{'app.py':digest(b'old')}})
   self.assertEqual((app/'app.py').read_bytes(),b'next')
 def test_backup_and_stopped_count_gates_precede_start(self):
  main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
  calls={}
  for n in ast.walk(main):
   if isinstance(n,ast.Call) and isinstance(n.func,ast.Name):calls.setdefault(n.func.id,[]).append(n.lineno)
  self.assertLess(min(calls['backup']),min(calls['replace_file']))
  self.assertLess(min(calls['exact_stopped_counts']),min(calls['replace_file']))
  self.assertTrue(any(min(calls['replace_file'])<line<min(calls['start_and_wait']) for line in calls['exact_stopped_counts']))
  self.assertNotIn('systemctl stop mysql',s);self.assertNotIn('systemctl start lewan-game',s)
result=unittest.TextTestRunner(stream=sys.stdout,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Guards))
(here/'release-guard-receipt.json').write_text(json.dumps(dict(at_utc=datetime.now(timezone.utc).isoformat(),tests=result.testsRun,errors=len(result.errors),failures=len(result.failures),publisher_sha256=hashlib.sha256(file.read_bytes()).hexdigest(),scope='synthetic release protection only'),indent=2)+'\n')
raise SystemExit(0 if result.wasSuccessful() else 1)

"""New one-file metadata ownership release. No application import or DB rollback."""
import ast
from pathlib import Path
from datetime import datetime, timezone
import base64, fcntl, gzip, hashlib, json, os, pwd, re, shlex, shutil, subprocess, sys, time
from urllib.request import urlopen
from urllib.error import HTTPError
from dotenv import dotenv_values
from sqlalchemy import create_engine, event, text

APP=Path("/opt/arbitrage-hub")
ROOT=Path(__file__).resolve().parent
BASE="https://arbi-k7m4p2.47-238-145-158.sslip.io"
NEW_TABLES={"binance_account_link","binance_hedge_intent","binance_executor_health"}

def need(ok, label):
    if not ok: raise RuntimeError(label)

def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for part in iter(lambda:f.read(1048576),b''):h.update(part)
    return h.hexdigest()

def save(name,data):
    with (ROOT/name).open('x') as f:json.dump(data,f,ensure_ascii=False,sort_keys=True,indent=2,default=str);f.write('\n')

def state(name):
    return dict(line.split('=',1) for line in subprocess.check_output(['systemctl','show',name,'-p','MainPID','-p','ActiveState','-p','InvocationID','-p','NRestarts'],text=True).splitlines())

def identity(stopped=False):
    states={n:state(n) for n in ('arbitrage-private','mysql','lewan-game')}
    need(states['mysql']['ActiveState']=='active' and states['lewan-game']['ActiveState']=='failed' and states['lewan-game']['MainPID']=='0','protected_service_state_drift')
    master=int(states['arbitrage-private']['MainPID'])
    if stopped:
        need(master==0 and states['arbitrage-private']['ActiveState']=='inactive','website_not_stopped')
        group=Path('/sys/fs/cgroup/system.slice/arbitrage-private.service/cgroup.procs')
        need(not group.exists() or not group.read_text().strip(),'website_processes_remaining')
        return {'services':states,'worker':None,'worker_start_ticks':None}
    need(states['arbitrage-private']['ActiveState']=='active' and master>0,'website_not_active')
    workers=list(map(int,Path(f'/proc/{master}/task/{master}/children').read_text().split()))
    need(len(workers)==1,'worker_count')
    fields=Path(f'/proc/{workers[0]}/stat').read_text().rsplit(')',1)[1].split()
    return {'services':states,'worker':workers[0],'worker_start_ticks':int(fields[19])}

def limits():
    return dict(line.split('=',1) for line in subprocess.check_output(['systemctl','show','arbitrage-private','-p','MemoryHigh','-p','MemoryMax'],text=True).splitlines())

def snapshot(manifest, stopped=False):
    r={'at_utc':datetime.now(timezone.utc).isoformat(),'identity':identity(stopped=stopped),'site_limits':limits(),
       'source':{n:sha(APP/n) if (APP/n).is_file() else None for n in manifest['expected_source']},
       'private_hashes':{n:sha(APP/n) for n in ('.env','.chat_encryption.key','.binance_api_encryption.key')},
       'static_sha256':sha(APP/'static/app.js')}
    values=dotenv_values(APP/'.env')
    engine=create_engine(values['DATABASE_URL'],hide_parameters=True,connect_args={'connect_timeout':5,'read_timeout':20,'write_timeout':20})
    need(engine.url.database=='arbitrage_hub' and engine.url.host in ('127.0.0.1','localhost'),'unexpected_db')
    @event.listens_for(engine,'connect')
    def ro(connection,record):
        with connection.cursor() as c:
            c.execute("SET SESSION time_zone='SYSTEM'");c.execute('SET SESSION TRANSACTION READ ONLY');c.execute('SET SESSION max_execution_time=10000')
    try:
        with engine.connect() as c:
            need(c.execute(text('SELECT @@transaction_read_only')).scalar()==1,'read_only_required')
            r['db_identity']=dict(c.execute(text('SELECT @@server_uuid AS server_uuid,@@socket AS socket,@@port AS port')).mappings().one())
            need(Path(r['db_identity']['socket']).resolve()==Path('/run/mysqld/mysqld.sock').resolve(),'database_socket')
            tables=c.execute(text('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME')).scalars().all()
            need(len(set(tables)-NEW_TABLES)==51 and all(re.fullmatch('[a-zA-Z0-9_]+',n) for n in tables),'schema_shape')
            r['table_counts']={n:c.execute(text('SELECT COUNT(*) FROM `'+n+'`')).scalar() for n in tables if n not in NEW_TABLES}
            r['new_table_counts']={n:c.execute(text('SELECT COUNT(*) FROM `'+n+'`')).scalar() for n in tables if n in NEW_TABLES}
            queries={
                'account_stable':'SELECT id,username,password_hash,role,account_level,active,password_changed_at,created_at FROM user_account ORDER BY id',
                'positions':'SELECT * FROM hedge_position ORDER BY id',
                'watch_flags':'SELECT id,symbol,active,started_at,stopped_at FROM thought_watch ORDER BY id',
                'personal_watches':'SELECT * FROM opportunity_watch ORDER BY id',
                'runtime_rules':'SELECT rule_key,enabled,value FROM runtime_rule ORDER BY rule_key'}
            r['protected_hashes']={}
            for name,q in queries.items():
                # Backup/deployment protection only: never export row contents.
                h=hashlib.sha256();h.update(b'[')
                for index,row in enumerate(c.execute(text(q))):
                    if index:h.update(b',')
                    h.update(json.dumps(list(row),default=str,separators=(',',':')).encode())
                h.update(b']');r['protected_hashes'][name]=h.hexdigest()
            r['refresh_rules']=[list(row) for row in c.execute(text("SELECT rule_key,enabled,value FROM runtime_rule WHERE rule_key IN ('spot_market_refresh','dual_market_refresh') ORDER BY rule_key"))]
            r['index_digest']=hashlib.sha256(json.dumps([list(row) for row in c.execute(text("SELECT TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX,COLUMN_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME NOT IN ('binance_account_link','binance_hedge_intent','binance_executor_health') ORDER BY TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX"))],default=str,separators=(',',':')).encode()).hexdigest()
    finally:engine.dispose()
    return r

def https(expected):
    rows=[]
    paths=[('/login',200),('/api/runtime-health',401),('/api/binance/account',401),('/api/dual-futures',401),('/api/alerts?view=active',401)]+[('/'+n+'?v=metadata-owner-20261113',200) for n in expected if n.startswith('static/')]
    for path,wanted in paths:
        try:
            with urlopen(BASE+path,timeout=12) as response:
                code,body,cache=response.status,response.read(1048577),response.headers.get('Cache-Control')
        except HTTPError as error:
            try:code,body,cache=error.code,error.read(65536),error.headers.get('Cache-Control')
            finally:error.close()
        need(code==wanted,'https_status_'+str(code));row={'path':path,'status':code,'cache_control':cache}
        if path.startswith('/static/'):
            need(len(body)<=1048576,'static_size');row['sha256']=hashlib.sha256(body).hexdigest()
            need(row['sha256']==expected[path.split('?',1)[0].lstrip('/')],'https_static_sha')
            need(cache and 'no-store' in cache,'static_cache_policy')
        rows.append(row)
    return rows

def backup(before):
    # Reserve at most 1 GiB for this fresh backup, retaining 3 GiB headroom.
    need(shutil.disk_usage(ROOT).free>4*1024**3,'disk_headroom')
    key=ROOT/'chat-encryption.key';shutil.copyfile(APP/'.chat_encryption.key',key);key.chmod(0o600)
    need(sha(key)==before['private_hashes']['.chat_encryption.key'],'restore_key_match')
    binance_key=ROOT/'binance-encryption.key';shutil.copyfile(APP/'.binance_api_encryption.key',binance_key);binance_key.chmod(0o600)
    need(sha(binance_key)==before['private_hashes']['.binance_api_encryption.key'],'binance_restore_key_match')
    part=ROOT/'mysql.sql.gz.partial'
    dump_error=ROOT/'backup-dump.stderr';zip_error=ROOT/'backup-gzip.stderr'
    with part.open('xb') as output,dump_error.open('xb') as de,zip_error.open('xb') as ze:
        dump=subprocess.Popen(['nice','-n','19','mysqldump','--protocol=SOCKET','--socket='+before['db_identity']['socket'],'--user=root','--single-transaction','--quick','--routines','--events','--triggers','--hex-blob','--default-character-set=utf8mb4','--databases','arbitrage_hub'],stdout=subprocess.PIPE,stderr=de)
        zipper=subprocess.Popen(['nice','-n','19','gzip','-3'],stdin=dump.stdout,stdout=output,stderr=ze);dump.stdout.close()
        try:
            deadline=time.monotonic()+360
            while zipper.poll() is None:
                if time.monotonic()>deadline:raise RuntimeError('backup_timeout')
                if part.stat().st_size>1024**3 or shutil.disk_usage(ROOT).free<3*1024**3:raise RuntimeError('backup_disk_guard')
                time.sleep(.25)
            zcode=zipper.returncode;dcode=dump.wait(timeout=20)
        except (subprocess.TimeoutExpired,RuntimeError):
            for child in (zipper,dump):
                if child.poll() is None:child.terminate()
            for child in (zipper,dump):
                try:child.wait(timeout=5)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
            raise RuntimeError('backup_aborted_before_publication') from None
    need(zcode==0 and dcode==0,'backup_child_failed')
    need(dump_error.stat().st_size==0 and zip_error.stat().st_size==0,'backup_stderr_not_empty')
    need(part.stat().st_size<=1024**3 and shutil.disk_usage(ROOT).free>=3*1024**3,'backup_final_disk_guard')
    tables=[];completed=False
    verify_deadline=time.monotonic()+90;uncompressed=0
    with gzip.open(part,'rb') as content:
        for line in iter(lambda:content.readline(16*1024**2+1),b''):
            uncompressed+=len(line)
            need(len(line)<=16*1024**2 and uncompressed<=12*1024**3 and time.monotonic()<=verify_deadline,'backup_verify_bound')
            if line.startswith(b'CREATE TABLE `'):
                tables.append(line.split(b'`',2)[1].decode('ascii'))
            if line.startswith(b'-- Dump completed on '):completed=True
    need(completed and sorted(tables)==sorted(set(before['table_counts'])|set(before['new_table_counts'])),'backup_completeness')
    need(part.stat().st_size>1000000,'backup_too_small')
    final=ROOT/'mysql.sql.gz';os.replace(part,final)
    result={'verified_at_utc':datetime.now(timezone.utc).isoformat(),'bytes':final.stat().st_size,'sha256':sha(final),'tables':len(tables),'completed_footer':completed,'gzip_valid':True,'restore_key_matches':sha(key)==sha(APP/'.chat_encryption.key'),'binance_restore_key_matches':sha(binance_key)==sha(APP/'.binance_api_encryption.key'),'dump_exit':dcode,'gzip_exit':zcode,'dump_stderr_bytes':dump_error.stat().st_size,'gzip_stderr_bytes':zip_error.stat().st_size}
    save('backup-check.json',result);return result

def replace_file(source,relative):
    target=APP/relative
    if not target.parent.exists():
        target.parent.mkdir(mode=0o755,parents=True)
        target.parent.chmod(0o755)
    stat=target.stat() if target.exists() else None;tmp=target.with_name(target.name+'.metadata-owner-20261113-next')
    with tmp.open('xb') as f:f.write(source.read_bytes());f.flush();os.fsync(f.fileno())
    owner=pwd.getpwnam('arbhub')
    os.chmod(tmp,stat.st_mode&0o777 if stat else 0o644);os.chown(tmp,stat.st_uid if stat else owner.pw_uid,stat.st_gid if stat else owner.pw_gid);os.replace(tmp,target)

def restore_file(relative, manifest):
    need(sha(APP/relative)==manifest['next_source'][relative],'rollback_source_drift')
    if manifest['expected_source'][relative] is None:
        (APP/relative).unlink()
    else:
        need(sha(ROOT/'previous'/relative)==manifest['expected_source'][relative],'rollback_backup_sha')
        replace_file(ROOT/'previous'/relative,relative)

def journal_summary(since):
    raw=subprocess.check_output(['journalctl','-u','arbitrage-private','--since',since,'--no-pager','-o','json'],text=True,timeout=12)
    health=[];boots=[];errors={}
    for line in raw.splitlines():
        e=json.loads(line);m=e.get('MESSAGE','')
        if not isinstance(m,str):continue
        if 'runtime_health ' in m:
            try:
                v=json.loads(m.split('runtime_health ',1)[1]);v['at_utc_us']=e['__REALTIME_TIMESTAMP'];v['invocation']=e.get('_SYSTEMD_INVOCATION_ID');health.append(v)
            except (ValueError,KeyError):pass
        match=re.search(r'Booting worker with pid: (\d+)',m)
        if match:boots.append(int(match.group(1)))
        for pattern in ('WORKER TIMEOUT','Traceback (most recent call last)','background_worker_retry','[ERROR]'):
            if pattern in m:errors[pattern]=errors.get(pattern,0)+1
    return {'last_health':health[-1] if health else None,'boot_pids':boots,'error_counts':errors,'health_sample_count':len(health)}

def check_protected(before,after):
    for n in ('mysql','lewan-game'):
        need(after['identity']['services'][n]==before['identity']['services'][n],'protected_service_identity_changed')
    for k in ('private_hashes','protected_hashes','new_table_counts','refresh_rules','index_digest','db_identity','site_limits'):
        need(after[k]==before[k],k+'_changed')
    need(set(after['table_counts'])==set(before['table_counts']),'tables_changed')

def start_and_wait():
    subprocess.run(['systemctl','start','arbitrage-private'],check=True,timeout=50)
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        try:
            live=identity()
            with urlopen(BASE+'/login',timeout=5) as response:
                if response.status==200:return live
        except HTTPError as error:error.close()
        except (RuntimeError,OSError,ValueError):pass
        time.sleep(1)
    raise RuntimeError('website_start_timeout')

def execution_disabled():
    values=dotenv_values(APP/'.env')
    service_env=shlex.split(subprocess.check_output(['systemctl','show','arbitrage-private','-p','Environment','--value'],text=True))
    need(values.get('BINANCE_EXECUTION_ENABLED')!='1' and 'BINANCE_EXECUTION_ENABLED=1' not in service_env,'execution_flag_must_be_off')
    executor_env=shlex.split(subprocess.check_output(['systemctl','show','arbitrage-binance','-p','Environment','--value'],text=True))
    need('BINANCE_EXECUTION_ENABLED=1' not in executor_env,'executor_flag_must_be_off')
    active=subprocess.run(['systemctl','is-active','--quiet','arbitrage-binance'],check=False)
    need(active.returncode in (3,4),'executor_must_not_be_running')

def exact_stopped_counts(paused,installed):
    need(paused['table_counts']==installed['table_counts'],'original_51_counts_changed')
    need(paused['new_table_counts']==installed['new_table_counts'],'module_3_counts_changed')

def narrow_change(before, after):
    allowed={'fetch_equity_metadata','collect_equity_metadata'}
    trees=[ast.parse(s) for s in (before,after)]
    maps=[{n.name:n for n in t.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))} for t in trees]
    changed={n for n in maps[0].keys()|maps[1].keys() if n not in maps[0] or n not in maps[1] or ast.dump(maps[0][n])!=ast.dump(maps[1][n])}
    need(changed==allowed,'two_function_scope')
    for t in trees:t.body=[n for n in t.body if not (isinstance(n,ast.FunctionDef) and n.name in allowed)]
    need(ast.dump(trees[0])==ast.dump(trees[1]),'non_function_scope')
    for n in allowed:
        need(ast.dump(maps[0][n].args)==ast.dump(maps[1][n].args),'signature_scope')
    compile(after,'candidate/app.py','exec')

def main():
    os.umask(0o077)
    need(os.geteuid()==0 and ROOT.parent==Path('/root') and ROOT.name.startswith('arbiscope-release-metadata-owner.'),'private_release_path')
    manifest=json.loads((ROOT/'manifest.json').read_text())
    need(manifest['scope']=='Only metadata wire ownership and owner-thread decoding; app.py two functions; live OFF','scope')
    need(set(manifest['next_source'])=={'app.py'} and manifest['live_orders_enabled'] is False,'file_scope')
    need(manifest['next_source']['app.py']=='3e3c818f8f3d38410eef87e325535017d5456e959a04bc09421da5b4908b9696','reviewed_candidate')
    need(sha(ROOT/'next/app.py')==manifest['next_source']['app.py'],'staged_source_sha')
    checks=manifest['short_checks']
    need(checks['unique_tests']==37 and checks['candidate_rechecks']==[10,19] and checks['errors']==checks['failures']==checks['success_stderr_bytes']==0,'short_checks')
    need(checks['review_sha256']=='bc5d32de8fcb6f2e56eabb0f33954832c8794a1704894a74a402d87627bfb02d','short_review_binding')
    need(sys.version_info[:2]==(3,12),'runtime_compatibility')
    narrow_change((APP/'app.py').read_text(),(ROOT/'next/app.py').read_text())
    execution_disabled()
    (ROOT/'publish-once.lock').mkdir(mode=0o700)
    before=snapshot(manifest,stopped=True);save('before.json',before)
    need(before['source']==manifest['expected_source'],'source_changed')
    need(before['identity']==manifest['expected_identity'],'identity_changed_since_preflight')
    for k,v in manifest['original_protection'].items():need(before[k]==v,'original_'+k+'_changed')
    need(len(before['refresh_rules'])==2 and all(bool(r[1]) and float(r[2])==5 for r in before['refresh_rules']),'five_second_refresh_required')
    previous=ROOT/'previous/app.py';previous.parent.mkdir(mode=0o700);shutil.copyfile(APP/'app.py',previous)
    need(sha(previous)==manifest['expected_source']['app.py'],'previous_source_sha')
    with Path('/var/backups/arbitrage-hub/mysql/.backup.lock').open('r') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        backup_result=backup(before)
    paused=snapshot(manifest,stopped=True);save('stopped-before.json',paused)
    need(paused['source']==before['source'] and paused['identity']==before['identity'],'drift_during_backup')
    check_protected(before,paused);exact_stopped_counts(before,paused);execution_disabled()
    need(shutil.disk_usage(ROOT).free>=3*1024**3,'publication_disk_headroom')
    expected={**manifest['expected_source'],**manifest['next_source']}
    changed=False;started=False;install_at=datetime.now(timezone.utc).isoformat()
    try:
        # The website was already stopped before this release. No stop/restart of MySQL or game.
        replace_file(ROOT/'next/app.py','app.py');changed=True
        installed=snapshot(manifest,stopped=True);save('stopped-after.json',installed)
        need(installed['source']==expected,'installed_source_sha')
        check_protected(paused,installed);exact_stopped_counts(paused,installed);execution_disabled()
        start_at=datetime.now(timezone.utc).isoformat();started=True;start_and_wait()
        after=snapshot(manifest);save('after.json',after);check_protected(paused,after)
        need(after['source']==expected,'after_source_sha')
        need(after['identity']['services']['arbitrage-private']['InvocationID']!=before['identity']['services']['arbitrage-private']['InvocationID'],'new_invocation_required')
        checks=https({});save('https-check.json',checks)
        deadline=time.monotonic()+75;health=None;report={}
        while time.monotonic()<deadline:
            report=journal_summary(start_at);health=report['last_health']
            if health and health.get('pid')==after['identity']['worker'] and health.get('workers')==30 and not health.get('dead') and not health.get('restarts'):break
            time.sleep(1)
        save('new-worker-health.json',report)
        need(health is not None and health.get('pid')==after['identity']['worker'] and health.get('workers')==30 and not health.get('dead') and not health.get('restarts'),'new_runtime_health_missing_or_incomplete')
        need(health.get('invocation')==after['identity']['services']['arbitrage-private']['InvocationID'],'health_invocation')
        need(report['boot_pids']==[after['identity']['worker']] and not report['error_counts'],'startup_lifecycle_or_errors')
        need(identity()==after['identity'],'worker_changed_during_checks');execution_disabled()
        listeners=[row.split()[3] for row in subprocess.check_output(['ss','-lntH'],text=True).splitlines() if row.split()[3].endswith((':3306',':15831'))]
        need(sorted(listeners)==['127.0.0.1:15831','127.0.0.1:3306'],'listener_scope')
        result={'completed_at_utc':datetime.now(timezone.utc).isoformat(),'status':'METADATA_OWNER_PUBLISHED_PRODUCTION_OBSERVATION_PENDING','changed_files':['app.py'],'changed_definitions':['fetch_equity_metadata','collect_equity_metadata'],'backup':backup_result,'identity':after['identity'],'source':expected,'protected_summaries_unchanged':True,'original_protection_verified':True,'stopped_all_54_table_counts_equal':True,'indexes_and_refresh_rules_unchanged':True,'listener_addresses':listeners,'http':checks,'install_at_utc':install_at,'start_requested_at_utc':start_at,'old_identity':before['identity'],'preexisting_outage':manifest['preexisting_outage'],'mysql_or_game_restarted_by_this_release':False,'live_execution_enabled':False,'real_orders_sent':0,'executor_started':False,'disk_free_bytes':shutil.disk_usage(ROOT).free,'memory_acceptance':'PENDING; cold-start RSS is not success; historical failures preserved; no synthetic soak'}
        save('result.json',result);print(json.dumps(result,sort_keys=True))
    except Exception as error:
        restored=False
        try:
            if changed:
                if started:subprocess.run(['systemctl','stop','arbitrage-private'],check=True,timeout=90)
                identity(stopped=True)
                restore_file('app.py',manifest);start_and_wait();restored=True
        finally:
            failure={'at_utc':datetime.now(timezone.utc).isoformat(),'status':'FAILED_METADATA_OWNER_RELEASE','error_type':type(error).__name__,'reason':str(error) if isinstance(error,RuntimeError) else 'unexpected_error','website_file_restored':restored,'database_rollback':False,'changed_app':changed}
            save('failure.json',failure);print(json.dumps(failure))
        raise SystemExit(1)

if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'at_utc':datetime.now(timezone.utc).isoformat(),'status':'FAILED_BEFORE_PUBLICATION_OR_REVIEW_REQUIRED','error_type':type(error).__name__,'reason':str(error) if isinstance(error,RuntimeError) else 'details_suppressed'}));sys.exit(1)

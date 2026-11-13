"""Independent final read-only check; no publisher main or app import."""
import ast

from pathlib import Path

from datetime import datetime, timezone

import base64, fcntl, gzip, hashlib, json, os, pwd, re, shlex, shutil, subprocess, sys, time

from urllib.request import urlopen

from urllib.error import HTTPError

from dotenv import dotenv_values

from sqlalchemy import create_engine, event, text

def need(ok, label):
    if not ok: raise RuntimeError(label)

def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for part in iter(lambda:f.read(1048576),b''):h.update(part)
    return h.hexdigest()

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

def check_protected(before,after):
    for n in ('mysql','lewan-game'):
        need(after['identity']['services'][n]==before['identity']['services'][n],'protected_service_identity_changed')
    for k in ('private_hashes','protected_hashes','new_table_counts','refresh_rules','index_digest','db_identity','site_limits'):
        need(after[k]==before[k],k+'_changed')
    need(set(after['table_counts'])==set(before['table_counts']),'tables_changed')

def execution_disabled():
    values=dotenv_values(APP/'.env')
    service_env=shlex.split(subprocess.check_output(['systemctl','show','arbitrage-private','-p','Environment','--value'],text=True))
    need(values.get('BINANCE_EXECUTION_ENABLED')!='1' and 'BINANCE_EXECUTION_ENABLED=1' not in service_env,'execution_flag_must_be_off')
    executor_env=shlex.split(subprocess.check_output(['systemctl','show','arbitrage-binance','-p','Environment','--value'],text=True))
    need('BINANCE_EXECUTION_ENABLED=1' not in executor_env,'executor_flag_must_be_off')
    active=subprocess.run(['systemctl','is-active','--quiet','arbitrage-binance'],check=False)
    need(active.returncode in (3,4),'executor_must_not_be_running')
APP=Path('/opt/arbitrage-hub');ROOT=Path('/root/arbiscope-release-metadata-owner.22didsd7')
BASE='https://arbi-k7m4p2.47-238-145-158.sslip.io'
NEW_TABLES={'binance_account_link','binance_hedge_intent','binance_executor_health'}
manifest=json.loads((ROOT/'manifest.json').read_text());expected=json.loads((ROOT/'result.json').read_text());before=json.loads((ROOT/'before.json').read_text())
execution_disabled();r=snapshot(manifest);check_protected(before,r)
r['identity_matches_release']=r['identity']==expected['identity'];r['source_matches_release']=r['source']==expected['source']
r['protection_matches_original']=all(r[k]==v for k,v in manifest['original_protection'].items());r['https']=https({})
r['listeners']=[row.split()[3] for row in subprocess.check_output(['ss','-lntH'],text=True).splitlines() if row.split()[3].endswith((':3306',':15831'))]
r['listener_scope_ok']=sorted(r['listeners'])==['127.0.0.1:15831','127.0.0.1:3306']
r['backup_retained']={'bytes':(ROOT/'mysql.sql.gz').stat().st_size,'size_matches':(ROOT/'mysql.sql.gz').stat().st_size==expected['backup']['bytes'],'mode':oct((ROOT/'mysql.sql.gz').stat().st_mode & 0o777),'matching_chat_key':sha(ROOT/'chat-encryption.key')==r['private_hashes']['.chat_encryption.key'],'matching_binance_key':sha(ROOT/'binance-encryption.key')==r['private_hashes']['.binance_api_encryption.key']}
r['private_release_mode']=oct(ROOT.stat().st_mode & 0o777);r['once_lock_exists']=(ROOT/'publish-once.lock').is_dir();r['publisher_stderr_bytes']=(ROOT/'publish.stderr').stat().st_size;r['live_execution_enabled']=False;r['disk_free_bytes']=shutil.disk_usage(ROOT).free;r['finished_at_utc']=datetime.now(timezone.utc).isoformat()
need(r['identity_matches_release'] and r['source_matches_release'] and r['protection_matches_original'] and r['listener_scope_ok'],'final_gate_drift')
print(json.dumps(r,sort_keys=True))

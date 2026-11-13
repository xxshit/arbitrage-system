"""Fresh read-only preflight for the approved two-function metadata release. No app import."""
from pathlib import Path
from datetime import datetime, timezone
import base64, fcntl, gzip, hashlib, json, os, pwd, re, shlex, shutil, subprocess, sys, time
from urllib.request import urlopen
from urllib.error import HTTPError
from dotenv import dotenv_values
from sqlalchemy import create_engine, event, text

def need(ok, label):
    if not ok:
        raise RuntimeError(label)

def sha(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for part in iter(lambda : f.read(1048576), b''):
            h.update(part)
    return h.hexdigest()

def state(name):
    return dict((line.split('=', 1) for line in subprocess.check_output(['systemctl', 'show', name, '-p', 'MainPID', '-p', 'ActiveState', '-p', 'InvocationID', '-p', 'NRestarts'], text=True).splitlines()))

def identity(stopped=False):
    states = {n: state(n) for n in ('arbitrage-private', 'mysql', 'lewan-game')}
    need(all((states[n]['ActiveState'] == 'active' for n in ('mysql', 'lewan-game'))), 'protected_service_not_active')
    master = int(states['arbitrage-private']['MainPID'])
    if stopped:
        need(master == 0 and states['arbitrage-private']['ActiveState'] == 'inactive', 'website_not_stopped')
        group = Path('/sys/fs/cgroup/system.slice/arbitrage-private.service/cgroup.procs')
        need(not group.exists() or not group.read_text().strip(), 'website_processes_remaining')
        return {'services': states, 'worker': None, 'worker_start_ticks': None}
    need(states['arbitrage-private']['ActiveState'] == 'active' and master > 0, 'website_not_active')
    workers = list(map(int, Path(f'/proc/{master}/task/{master}/children').read_text().split()))
    need(len(workers) == 1, 'worker_count')
    fields = Path(f'/proc/{workers[0]}/stat').read_text().rsplit(')', 1)[1].split()
    return {'services': states, 'worker': workers[0], 'worker_start_ticks': int(fields[19])}

def limits():
    return dict((line.split('=', 1) for line in subprocess.check_output(['systemctl', 'show', 'arbitrage-private', '-p', 'MemoryHigh', '-p', 'MemoryMax'], text=True).splitlines()))

def snapshot(manifest, stopped=False):
    r = {'at_utc': datetime.now(timezone.utc).isoformat(), 'identity': identity(stopped=stopped), 'site_limits': limits(), 'source': {n: sha(APP / n) if (APP / n).is_file() else None for n in manifest['expected_source']}, 'private_hashes': {n: sha(APP / n) for n in ('.env', '.chat_encryption.key', '.binance_api_encryption.key')}, 'static_sha256': sha(APP / 'static/app.js')}
    values = dotenv_values(APP / '.env')
    engine = create_engine(values['DATABASE_URL'], hide_parameters=True, connect_args={'connect_timeout': 5, 'read_timeout': 20, 'write_timeout': 20})
    need(engine.url.database == 'arbitrage_hub' and engine.url.host in ('127.0.0.1', 'localhost'), 'unexpected_db')

    @event.listens_for(engine, 'connect')
    def ro(connection, record):
        with connection.cursor() as c:
            c.execute("SET SESSION time_zone='SYSTEM'")
            c.execute('SET SESSION TRANSACTION READ ONLY')
            c.execute('SET SESSION max_execution_time=10000')
    try:
        with engine.connect() as c:
            need(c.execute(text('SELECT @@transaction_read_only')).scalar() == 1, 'read_only_required')
            r['db_identity'] = dict(c.execute(text('SELECT @@server_uuid AS server_uuid,@@socket AS socket,@@port AS port')).mappings().one())
            need(Path(r['db_identity']['socket']).resolve() == Path('/run/mysqld/mysqld.sock').resolve(), 'database_socket')
            tables = c.execute(text('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME')).scalars().all()
            need(len(set(tables) - NEW_TABLES) == 51 and all((re.fullmatch('[a-zA-Z0-9_]+', n) for n in tables)), 'schema_shape')
            r['table_counts'] = {n: c.execute(text('SELECT COUNT(*) FROM `' + n + '`')).scalar() for n in tables if n not in NEW_TABLES}
            r['new_table_counts'] = {n: c.execute(text('SELECT COUNT(*) FROM `' + n + '`')).scalar() for n in tables if n in NEW_TABLES}
            queries = {'account_stable': 'SELECT id,username,password_hash,role,account_level,active,password_changed_at,created_at FROM user_account ORDER BY id', 'positions': 'SELECT * FROM hedge_position ORDER BY id', 'watch_flags': 'SELECT id,symbol,active,started_at,stopped_at FROM thought_watch ORDER BY id', 'personal_watches': 'SELECT * FROM opportunity_watch ORDER BY id', 'runtime_rules': 'SELECT rule_key,enabled,value FROM runtime_rule ORDER BY rule_key'}
            r['protected_hashes'] = {}
            for (name, q) in queries.items():
                h = hashlib.sha256()
                h.update(b'[')
                for (index, row) in enumerate(c.execute(text(q))):
                    if index:
                        h.update(b',')
                    h.update(json.dumps(list(row), default=str, separators=(',', ':')).encode())
                h.update(b']')
                r['protected_hashes'][name] = h.hexdigest()
            r['refresh_rules'] = [list(row) for row in c.execute(text("SELECT rule_key,enabled,value FROM runtime_rule WHERE rule_key IN ('spot_market_refresh','dual_market_refresh') ORDER BY rule_key"))]
            r['index_digest'] = hashlib.sha256(json.dumps([list(row) for row in c.execute(text("SELECT TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX,COLUMN_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME NOT IN ('binance_account_link','binance_hedge_intent','binance_executor_health') ORDER BY TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX"))], default=str, separators=(',', ':')).encode()).hexdigest()
    finally:
        engine.dispose()
    return r

def execution_disabled():
    values = dotenv_values(APP / '.env')
    service_env = shlex.split(subprocess.check_output(['systemctl', 'show', 'arbitrage-private', '-p', 'Environment', '--value'], text=True))
    need(values.get('BINANCE_EXECUTION_ENABLED') != '1' and 'BINANCE_EXECUTION_ENABLED=1' not in service_env, 'execution_flag_must_be_off')
    executor_env = shlex.split(subprocess.check_output(['systemctl', 'show', 'arbitrage-binance', '-p', 'Environment', '--value'], text=True))
    need('BINANCE_EXECUTION_ENABLED=1' not in executor_env, 'executor_flag_must_be_off')
    active = subprocess.run(['systemctl', 'is-active', '--quiet', 'arbitrage-binance'], check=False)
    need(active.returncode in (3, 4), 'executor_must_not_be_running')

APP=Path('/opt/arbitrage-hub')
ROOT=Path('/root')
NEW_TABLES={'binance_account_link','binance_hedge_intent','binance_executor_health'}
manifest={'expected_source': {'alert_history_index.py': '8161fb56a801c0cd7e89df06c9cdf074da8858501235b24c73429ed6be6f8ea7', 'alert_read_payload.py': '0c9345baa6dcba00f24c6d80ad3239a4130e0a59d81f55b270f24c00d54733b5', 'alert_reminders.py': 'fc5b1a6f213544b7cafe5d6a3af174f7f4dd758964e66c7a7b09041f65ab4e67', 'alert_screening.py': 'a38be0da586f2be8ec8c54ed9e6e9a2c2a9cecccbd1b65a11e2c3f6334d2b899', 'anomaly_engine.py': '6dd52fed9f69c13539fa1bc4ece090702c3e6a5259db855996af44b8fd472737', 'anomaly_streams.py': 'ad1603aee41a71a3aee1fb3f272ccb2f750f57ddf4264cf5de8abd118353fd0e', 'app.py': '1c4fc4f972dcd5748dac9596b7fc981a793f69169030845825e0a23526595eae', 'asset_catalog.py': 'd6ab273298ce9743a1bdfc321bb90fc110b34ad7a5840564b3774cb673f1fc47', 'background_runtime.py': 'c4cfe235c4d467d9fe65580e7017faa2f08d34a877e133fb53de318d0d61a5c5', 'binance_hedge/__init__.py': '4ffaecdd788efe93f0c3fde20defb38a837047504aa402c3c59c562613d37221', 'binance_hedge/budget.py': '16fd84f9e2e97841c22a1ac9c9003bfd5826ae1387ed98bc5ab036849910fcd4', 'binance_hedge/client.py': 'a1d1a766e62dcd03c73934c7e9a433c136abe63c96cbf5a4589691f5af41980f', 'binance_hedge/executor.py': 'fd23a54635426cfdfeeb7f0331aada70b6923c6967f82288e3c23fe6a3e617e0', 'binance_hedge/planner.py': 'c69d8ba64ddcc8c264120485620a8893f8ff26fa41ba708f033b864e62440cbc', 'binance_hedge/store.py': '1659f06d3857f3a133760be2c540c4492ea80293c8a89880575a5d504e011077', 'binance_hedge/streams.py': 'dbac27e26797bea533409ea4a61e7c7dd2b5bb8e0bec1fc52259fe6f29e34665', 'binance_hedge/wakeup.py': '4eb1bb54e870d709e1abe34c5394440c7a758dd8682f84f701a5b351b9909c2e', 'binance_hedge/web.py': 'd960b8313b1324eef452285e8e4267ba03f9af23920d44e98496a7f450392169', 'binance_hedge/worker.py': '6f1c47f11a3a06dce2c098b2d258739a1b90fc378c4d649574b6d374fa57b872', 'deploy/arbitrage-binance.service': '7e948faf141daedc9fb0939fbdc23efa573c0d2f51a749e90f41b5fa11efece5', 'equity_spot.py': 'd5f82b4918a65b2f751caac4ecb2354fb6dbfcce450055b910fcaa9086e69edc', 'funding_alerts.py': '8b6c4a16dd638896fd2a464ff67708bbe5f12a8eb3862258fc81540d71687345', 'funding_history.py': '24c266b9d798ba224a7b6b15cd4d1e3303b6c559d2a4336c4030eab630dd9125', 'market_status.py': '00c7eb9ac20f16cb754cdbd2bf1cf8d6064485e479e14799247e6021ff16ef94', 'market_transport.py': '968f2b2c2daba7704a35ced5d631d9cb0496c2a4f6341418622ee52e74c30d21', 'opportunity_activity.py': '11fbd036b19a266dcc5044b14c4ad35aa8e490177710640f36c210a9419bb47e', 'opportunity_funding.py': '4c2e7795883d3c7e55faa1f7dc2709f8731140f9beb0d000f067b0beda7bc2b2', 'opportunity_history.py': '0ea42d46db6c23f43950cbc7d654330984225d5de73bfd57d7157f4e55efabe4', 'opportunity_pool.py': '6b929eb655de812d09e9a2ddf993c2b5b46c3b9478a2fac7add38c3ecd6e9dad', 'opportunity_validation.py': '23af34d1e27e7a4350450a75a0de00adb45d4f8099f4a5424da34f15e42f7d34', 'persistence_policy.py': 'e1488fb9eb8d887d90fca4de4a79f81eb6b1eefac751a8fff5b9deb489e01463', 'quote_refresh.py': '7b82007454d4e87621d8b56a4504709fc8f98bbe369cd0ce8aa5365819ca9d59', 'read_performance.py': 'b8d26671f5d990f53ead2c02bd9ed4b3fc81b0ab69c9c731c10a4c79899c33c2', 'scripts/backup_mysql.sh': 'c06f939ff3ba4b181bcfe5c728fbbc49444e4b05de94bcff5315c65b46153e0d', 'scripts/run_mysql_backup_loop.sh': '4f1b8d5d8a54fcb4d584bb8c606eab400960aab648fe2b35e499c5394edbaad1', 'snapshot_persistence.py': 'fea4ad427fcdfa3b190e66dd1171e2775f3b78ef572a505f230a6173ab8fa2d2', 'spread_history.py': 'fbf165aeb763e6a9a788f17c1ff755ce56d0eac15baf8939551086d8f992b3a8', 'static/app.js': 'ca32b225e723092032046f194c2f2d9403c7261de2e6cfdc890b6478bddaee14', 'static/binance-hedge.js': '3652b438162c97cf4715fe6e4335e23464c59cb780c53cd3a47acb891e1807f6', 'static/style.css': '2376022afb3f42e441c576492bff258e4cd27594731c45dc82f8640f87e7de4a', 'templates/index.html': 'db268a37aae129262f766648c98b257a1ce19da789f3ce39f04e415f7b0f7273', 'trend_reference.py': 'c5372d28bfc18d4ef03d06257454d3cbe3dd2074c9dd326a19178c678d451bef'}, 'expected_identity': {'services': {'arbitrage-private': {'ActiveState': 'active', 'InvocationID': '25e853ed5afe41bbabb2a7a1d6f380a6', 'MainPID': '2383599', 'NRestarts': '0'}, 'lewan-game': {'ActiveState': 'active', 'InvocationID': '9713cdbc61094381bc0d3c8bc27eca11', 'MainPID': '398878', 'NRestarts': '0'}, 'mysql': {'ActiveState': 'active', 'InvocationID': '43b595043d5e45ce824f9332a34556c3', 'MainPID': '398829', 'NRestarts': '0'}}, 'worker': 2383603, 'worker_start_ticks': 727482204}}

execution_disabled()
r=snapshot(manifest)
r['source_matches']=r['source']==manifest['expected_source']
r['identity_matches']=r['identity']==manifest['expected_identity']
r['disk_free_bytes']=shutil.disk_usage('/root').free
r['memory_available_kib']=next(int(x.split()[1]) for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:'))
r['memory_pressure']=Path('/proc/pressure/memory').read_text().strip()
r['backup_lock_available']=False
with Path('/var/backups/arbitrage-hub/mysql/.backup.lock').open('r') as lock:
 try:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);r['backup_lock_available']=True
 except BlockingIOError:pass
pid=r['identity']['worker']
r['worker_memory']={line.split(':')[0]:int(line.split()[1])*1024 for line in Path(f'/proc/{pid}/smaps_rollup').read_text().splitlines()[1:] if line.startswith(('Rss:','Anonymous:','Private_Dirty:','Swap:'))}
r['captured_at_utc']=datetime.now(timezone.utc).isoformat()
print(json.dumps(r,default=str,sort_keys=True))

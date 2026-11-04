"""Separate, authenticated Unix HTTP verifier for an isolated synthetic workload.

The driver supplies only the synthetic funding writer's expectations. The peer
alone receives, decompresses and decodes HTTP bodies; the website gets scalar
receipts. Neither side keeps completed response objects between cycles.
"""
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
import gzip
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time

VIEWS = dict(summary='/api/alerts?view=summary', detail='/api/alerts?view=detail&symbol=SEED1/USDT',
             active='/api/alerts?view=active', spot='/api/spot-futures', dual='/api/dual-futures')
MAX_WIRE = 32 * 1024**2


def check_market_page(value, epoch):
    """In this generated feed each group has one path freshly stamped by its writer."""
    assert value['captured_at_epoch'] == epoch
    groups = value['symbols']
    assert len(groups) == min(value['total_symbols'], value['page_size']) > 0
    for group in groups:
        rows = group['rows']
        assert len(rows) == 1 and rows[0]['captured_at_epoch'] == epoch, 'quote page mixed generations'
    return len(groups)


def process_memory():
    result = dict(pid=os.getpid())
    if sys.platform == 'linux':
        values = {}
        for line in Path('/proc/self/smaps_rollup').read_text().splitlines():
            if ':' in line:
                key, value = line.split(':', 1)
                if key in ('Rss', 'Pss', 'Anonymous'):
                    values[key] = int(value.split()[0]) * 1024
        assert values.keys() == {'Rss', 'Pss', 'Anonymous'}
        result.update(rss_bytes=values['Rss'], pss_bytes=values['Pss'], anonymous_bytes=values['Anonymous'])
    return result


def peer_main(path, source):
    # No app import, application database connection, environment proxy or TCP path.
    sys.path.insert(0, str(source))
    from scripts.funding_alert_workload import FundingAlertWorkload, FundingEventJournal
    class UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(str(path))
    def fetch(url, cookie=None, login=False):
        conn = UnixConnection('localhost', timeout=30)
        headers = {'Accept-Encoding': 'gzip'}
        if cookie:
            headers['Cookie'] = cookie
        body = None
        if login:
            body = json.dumps(dict(username='synthetic-coupled', password='generated-unix-reader-only')).encode()
            headers['Content-Type'] = 'application/json'
        try:
            conn.request('POST' if login else 'GET', url, body, headers)
            with conn.getresponse() as reply:
                length = int(reply.getheader('Content-Length'))
                assert 0 <= length <= MAX_WIRE
                raw = reply.read()
                assert len(raw) == length
                return reply.status, reply.getheader('Content-Encoding'), raw, reply.getheader('Set-Cookie')
        finally:
            conn.close()
    assert fetch(VIEWS['summary'])[0] == 401
    status, _, raw, header = fetch('/api/auth/login', login=True)
    assert status == 200 and json.loads(raw)['ok']
    cookies = SimpleCookie(header)
    cookie = '; '.join(m.OutputString(attrs=[]) for m in cookies.values())
    del header, cookies, raw
    assert cookie
    oracle = SimpleNamespace(expected_events=FundingEventJournal(Path(path).parent / "expected-funding.sqlite3"), contracts=[], latest={}, http_checks=0, active_http_checks=0, http_lock=threading.Lock())
    decoder = threading.Lock()
    def verify(view, expected_epoch):
        start = time.monotonic()
        status, enc, body, _ = fetch(VIEWS[view], cookie)
        wire_seconds = time.monotonic()-start
        # All HTTP reads stay concurrent. Decode after the socket closes; each
        # completed body is fully verified before this batch is acknowledged.
        with decoder:
            decoded = gzip.decompress(body) if enc == 'gzip' else body
            assert len(decoded) < MAX_WIRE
            value = json.loads(decoded)
            out = dict(status=status, gzip=enc=='gzip', value_sha256=hashlib.sha256(decoded).hexdigest())
            if status == 503:
                assert view == 'detail' and '首次整理' in value['error']
            else:
                assert status == 200
                if view in ('summary', 'active'):
                    FundingAlertWorkload.verify_http(oracle, value['funding'])
                if view in ('spot', 'dual'):
                    out['verified_quote_rows'] = check_market_page(value, expected_epoch)
                if view == 'summary':
                    out['observations'] = value['total_observations']
                if view == 'detail':
                    out['items'] = len(value['items'])
            out['wire_seconds'] = wire_seconds
            out['seconds'] = time.monotonic()-start
            return out
    print(json.dumps(dict(ready=True,pid=os.getpid(),parent=os.getppid(),anonymous=401,login=200)),flush=True)
    with ThreadPoolExecutor(max_workers=5,thread_name_prefix='verifier') as readers:
        for line in sys.stdin.buffer:
            req=json.loads(line)
            assert set(req['views'])<=VIEWS.keys() and len(set(req['views']))==len(req['views'])
            oracle.contracts=req['base_contracts'];oracle.latest=req['latest']
            assert len(oracle.expected_events)==req['count']
            jobs={v:readers.submit(verify,v,req['epoch']) for v in req['views']}
            receipts={v:j.result(timeout=40) for v,j in jobs.items()}
            print(json.dumps(dict(views=receipts,memory=process_memory(),http_checks=oracle.http_checks,
                                  active_http_checks=oracle.active_http_checks)),flush=True)


class Peer:
    def __init__(self, app, root, source, program):
        from werkzeug.serving import WSGIRequestHandler, make_server
        self.path=root/'verify.sock'
        assert not self.path.exists() and root.stat().st_mode & 0o077 == 0 and root.resolve()==root
        class QuietRequest(WSGIRequestHandler):
            def log_request(self, *args): pass  # No URLs or cookies in test output.
        self.server=make_server('unix://'+str(self.path),0,app,threaded=True,request_handler=QuietRequest)
        assert self.server.address_family==socket.AF_UNIX
        self.pool=ThreadPoolExecutor(max_workers=5,thread_name_prefix='website-reader')
        self.server.process_request=lambda req,addr: self.pool.submit(self.server.process_request_thread,req,addr)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.p=None
        try:
            self.p=subprocess.Popen([sys.executable,'-B',str(program),'--http-peer',str(self.path),str(source)],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,env=dict(PATH='/usr/bin:/bin',LANG='C.UTF-8',TZ='UTC',PYTHON_DOTENV_DISABLED='1',MALLOC_ARENA_MAX='4'))
            self.ready=json.loads(self.p.stdout.readline())
            assert self.ready==dict(ready=True,pid=self.p.pid,parent=os.getpid(),anonymous=401,login=200)
        except BaseException:
            self.close()
            raise
    def batch(self, views, funding, epoch):
        journal = funding.expected_events
        assert journal.path == self.path.parent / "expected-funding.sqlite3"
        keys = set(funding.contracts[:16]) | journal.contracts()
        request=dict(views=views,epoch=epoch,count=len(journal),
                base_contracts=funding.contracts[:16],latest={k:funding.latest[k] for k in sorted(keys)})
        self.p.stdin.write(json.dumps(request,separators=(',',':')).encode()+b'\n');self.p.stdin.flush()
        out=json.loads(self.p.stdout.readline())
        assert out['memory']['pid']==self.p.pid and set(out['views'])==set(views)
        funding.http_checks=out['http_checks'];funding.active_http_checks=out['active_http_checks']
        self.last_memory=out['memory']
        return out['views']
    def close(self):
        try:
            if self.p:
                self.p.stdin.close()
                assert self.p.wait(timeout=45)==0
                self.p.stdout.close()
        finally:
            self.server.shutdown();self.thread.join(5)
            self.server.server_close();self.pool.shutdown(wait=True)
            self.path.unlink(missing_ok=True)

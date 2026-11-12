"""Short offline native controls of pinned metadata JSON/cache ownership.

Fresh processes exercise the actual narrow candidate functions and a
separate weak-reference lifetime witness. Full application AST preparation occurs outside measured children. No networking, DB, app import, trim or forced GC.
The TTL clock advances synthetically; this is NOT an elapsed-time soak.
Complete value validation happens in a separate child after memory sampling.
"""
import ast
import ctypes
import gc
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor, wait, Future
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.request import Request

SOURCE = Path('/opt/arbitrage-hub')
CANDIDATE_FUNCTIONS = 'def get_json(url, timeout=4):\n    request = Request(url, headers={\'User-Agent\': \'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ArbiScope/1.0\', \'Accept\': \'application/json\', \'Accept-Encoding\': \'gzip\'})\n    with urlopen(request, timeout=timeout, context=MARKET_HTTP.context) as response:\n        return decode_json(response.read(16 * 1024 * 1024 + 1), response.headers.get(\'Content-Encoding\', \'\'))[0]\n\ndef fetch_equity_metadata(url):\n    request = Request(url, headers={\'User-Agent\': \'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ArbiScope/1.0\', \'Accept\': \'application/json\', \'Accept-Encoding\': \'gzip\'})\n    try:\n        with urlopen(request, timeout=3, context=MARKET_HTTP.context) as response:\n            body = response.read(16 * 1024 * 1024 + 1)\n            if len(body) > 16 * 1024 * 1024:\n                raise ValueError(\'public_payload_too_large\')\n            encoding = response.headers.get(\'Content-Encoding\', \'\')\n    except HTTPError as exc:\n        exc.close()\n        raise\n    return ((body, encoding), time.monotonic())\n\ndef collect_equity_metadata():\n    """Non-blocking, bounded public-directory cache, called only by market worker."""\n    now = time.monotonic()\n    for (key, future) in list(EQUITY_METADATA_PENDING.items()):\n        if not future.done():\n            continue\n        del EQUITY_METADATA_PENDING[key]\n        try:\n            (document, observed) = future.result()\n            payload = decode_json(document[0], document[1])[0]\n            MARKET_PAYLOAD_CACHE[EQUITY_SPOT_URLS[key]] = dict(payload=payload, fetched_at=observed)\n            EQUITY_METADATA_ERRORS.pop(key, None)\n        except Exception as exc:\n            try:\n                EQUITY_METADATA_ERRORS[key] = type(exc).__name__\n                EQUITY_METADATA_RETRY_AT[key] = now + 5\n            finally:\n                exc.__traceback__ = None\n    (results, errors) = ({}, {})\n    keys = [\'equity_bybit_instruments\', *[key for key in EQUITY_SPOT_TTLS if key != \'equity_bybit_instruments\']]\n    for key in keys:\n        cached = MARKET_PAYLOAD_CACHE.get(EQUITY_SPOT_URLS[key])\n        if (not cached or now - cached[\'fetched_at\'] >= EQUITY_SPOT_TTLS[key]) and key not in EQUITY_METADATA_PENDING and (now >= EQUITY_METADATA_RETRY_AT.get(key, 0)):\n            EQUITY_METADATA_PENDING[key] = EQUITY_METADATA_POOL.submit(fetch_equity_metadata, EQUITY_SPOT_URLS[key])\n        if cached:\n            results[key] = cached[\'payload\']\n        if key in EQUITY_METADATA_ERRORS or not cached:\n            errors[key] = EQUITY_METADATA_ERRORS.get(key, \'公开目录首轮核验中\')\n    return (results, errors)'
EXPECTED = {'app.py':'1c4fc4f972dcd5748dac9596b7fc981a793f69169030845825e0a23526595eae',
            'market_transport.py':'968f2b2c2daba7704a35ced5d631d9cb0496c2a4f6341418622ee52e74c30d21',
            'equity_spot.py':'d5f82b4918a65b2f751caac4ecb2354fb6dbfcce450055b910fcaa9086e69edc'}


class MallInfo(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in ('arena','ordblks','smblks','hblks','hblkhd','usmblks','fsmblks','uordblks','fordblks','keepcost')]


def reader():
    mall = ctypes.CDLL(None).mallinfo2; mall.restype = MallInfo; mall.argtypes = []
    def measure():
        fields = dict(line.split(':',1) for line in Path('/proc/self/smaps_rollup').read_text().splitlines()[1:] if ':' in line)
        info = mall()
        return dict(rss_bytes=int(fields['Rss'].split()[0])*1024,
                    anonymous_bytes=int(fields['Anonymous'].split()[0])*1024,
                    malloc_used=info.uordblks,malloc_free=info.fordblks,malloc_arena=info.arena,malloc_mapped=info.hblkhd,
                    python_blocks=sys.getallocatedblocks(),gc_counts=gc.get_count())
    return measure


def load(fixtures):
    for name, sha in EXPECTED.items(): assert hashlib.sha256((SOURCE/name).read_bytes()).hexdigest()==sha
    spec=importlib.util.spec_from_file_location('pinned_transport',SOURCE/'market_transport.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    constants=json.loads((Path(__file__).parent/'prepared-constants.json').read_text())
    manifest=json.loads((fixtures/'manifest.json').read_text())
    wire={}; chunks={}
    for row in manifest['directories']:
        assert row['ok'] and row['encoding']=='gzip' and row['transfer_encoding']=='chunked'
        body=(fixtures/(row['key']+'.wire')).read_bytes()
        assert hashlib.sha256(body).hexdigest()==row['wire_sha256']
        wire[row['key']]=body
        frames=[b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Encoding: gzip\r\n\r\n']
        for offset in range(0,len(body),16384):
            piece=body[offset:offset+16384];frames.extend([hex(len(piece))[2:].encode()+b'\r\n',piece,b'\r\n'])
        frames.append(b'0\r\n\r\n');chunks[constants['EXTRA_URLS'][row['key']]]=b''.join(frames)
    return module,constants,manifest,wire,chunks


class Socket:
    def __init__(self,body): self.body=body
    def makefile(self,*args): return io.BytesIO(self.body)


class ImmediatePool:
    def submit(self,fn,*args):
        future=Future()
        try: future.set_result(fn(*args))
        except BaseException as exc: future.set_exception(exc)
        return future
    def shutdown(self,**kwargs): pass


class PayloadDict(dict): pass
class PayloadList(list): pass


def run_arm(fixtures,arm):
    started=time.monotonic()
    os.nice(19)
    resource.setrlimit(resource.RLIMIT_CPU,(20,20))
    resource.setrlimit(resource.RLIMIT_AS,(512*1024**2,512*1024**2))
    module,constants,manifest,wire,chunks=load(fixtures)
    clock=SimpleNamespace(value=10000.)
    calls={key:0 for key in constants['METADATA_TTLS']};refs=[]; tids=set()
    urlkeys={constants['EXTRA_URLS'][key]:key for key in calls}
    def open_fixture(request,**kwargs):
        tids.add(threading.get_native_id());calls[urlkeys[request.full_url]]+=1
        response=http.client.HTTPResponse(Socket(chunks[request.full_url]));response.begin();return response
    def decode_tracked(body,encoding):
        payload,size=module.decode_json(body,encoding)
        payload=PayloadDict(payload) if isinstance(payload,dict) else PayloadList(payload)
        refs.append(weakref.ref(payload));return payload,size
    pool=ThreadPoolExecutor(max_workers=3,thread_name_prefix='metadata-offline')
    env=dict(Request=Request,HTTPError=__import__('urllib.error',fromlist=['HTTPError']).HTTPError,urlopen=open_fixture,MARKET_HTTP=SimpleNamespace(context=None),
             decode_json=decode_tracked if arm=='lifetime' else module.decode_json,
             time=SimpleNamespace(monotonic=lambda:clock.value),EQUITY_SPOT_URLS=constants['EXTRA_URLS'],
             EQUITY_SPOT_TTLS=constants['METADATA_TTLS'],EQUITY_METADATA_POOL=pool,
             EQUITY_METADATA_PENDING={},EQUITY_METADATA_ERRORS={},EQUITY_METADATA_RETRY_AT={},MARKET_PAYLOAD_CACHE={})
    functions=(Path(__file__).parent/'prepared-functions.py').read_text()
    exec(compile(functions,'pinned-metadata-functions','exec'),env)
    latencies=[]
    measure=reader(); baseline=measure(); samples=[]; previous=None; max_live_after_release=0
    ticks=24 if arm=='lifetime' else 240
    for tick in range(ticks):
        clock.value=10000.+tick*15
        current,errors=env['collect_equity_metadata']()
        wait(tuple(env['EQUITY_METADATA_PENDING'].values()),timeout=5)
        assert all(f.done() for f in env['EQUITY_METADATA_PENDING'].values())
        publish_started=time.perf_counter()
        current,errors=env['collect_equity_metadata']()
        latencies.append(time.perf_counter()-publish_started)
        assert not errors and len(current)==5 and not env['EQUITY_METADATA_PENDING']
        # Match one reader generation overlapping publication, then let it go.
        previous=current
        del current,errors
        previous=None
        if arm=='lifetime':
            alive=sum(ref() is not None for ref in refs)
            max_live_after_release=max(max_live_after_release,alive)
            assert alive==5
        if tick in (0,1,3,7,15,29,59,89,119,149,179,209,239): samples.append(dict(tick=tick,**measure()))
    after_work=measure()
    pool.shutdown(wait=True)
    after_shutdown=measure()
    # The expensive exact-value oracle runs in another PID, after measurements.
    readfd,writefd=os.pipe(); validator=os.fork()
    if validator==0:
        os.close(readfd)
        try:
            for key in calls:
                expected=(wire[key],'gzip') if arm=='wire-only' else module.decode_json(wire[key],'gzip')[0]
                assert env['MARKET_PAYLOAD_CACHE'][constants['EXTRA_URLS'][key]]['payload']==expected
            os.write(writefd,json.dumps(dict(ok=True,pid=os.getpid(),directories=5)).encode())
            os._exit(0)
        except BaseException:
            os.write(writefd,b'{"ok":false}');os._exit(1)
    os.close(writefd);validation=json.loads(os.read(readfd,4096));os.close(readfd)
    _,status=os.waitpid(validator,0);assert status==0 and validation['ok']
    env['MARKET_PAYLOAD_CACHE'].clear()
    after_release=measure()
    alive=sum(ref() is not None for ref in refs)
    assert alive==0
    return dict(arm=arm,pid=os.getpid(),thread_ids=sorted(tids),elapsed_seconds=time.monotonic()-started,
                ticks=ticks,ttl_clock='synthetic 15-second increments, NOT elapsed time',calls=calls,baseline=baseline,samples=samples,
                after_work=after_work,after_shutdown=after_shutdown,after_release=after_release,validation=validation,
                publish_max_ms=max(latencies)*1000,publish_p95_ms=sorted(latencies)[int(.95*len(latencies))]*1000,
                tracked_payloads=len(refs),max_live_payloads=max_live_after_release,live_after_cache_clear=alive,
                source_unchanged=all(hashlib.sha256((SOURCE/k).read_bytes()).hexdigest()==v for k,v in EXPECTED.items()))


def main():
    sys.dont_write_bytecode=True
    fixtures=Path(sys.argv[1])
    if len(sys.argv)>2:
        print(json.dumps(run_arm(fixtures,sys.argv[2])));return
    (Path(__file__).parent/'prepared-functions.py').write_text(CANDIDATE_FUNCTIONS)
    constants={}
    for node in ast.parse((SOURCE/'equity_spot.py').read_text()).body:
        if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name) and node.targets[0].id in ('EXTRA_URLS','METADATA_TTLS'):
            constants[node.targets[0].id]=ast.literal_eval(node.value)
    (Path(__file__).parent/'prepared-constants.json').write_text(json.dumps(constants))
    result={'started_at_utc':datetime.now(timezone.utc).isoformat(),'source_sha256':EXPECTED,'candidate_functions_sha256':hashlib.sha256(CANDIDATE_FUNCTIONS.encode()).hexdigest(),'arms':[]}
    for arm in ('final-owner','lifetime'):
        child=subprocess.run([sys.executable,__file__,str(fixtures),arm],capture_output=True,timeout=27)
        assert child.returncode==0 and not child.stderr, (arm,child.returncode,child.stderr.decode()[-1200:])
        result['arms'].append(json.loads(child.stdout))
    result['ended_at_utc']=datetime.now(timezone.utc).isoformat()
    print(json.dumps(result))


if __name__=='__main__': main()

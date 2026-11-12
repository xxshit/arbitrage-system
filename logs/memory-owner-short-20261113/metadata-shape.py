"""One GET per public equity directory; no app import, account data or DB.

Save bounded public wire fixtures privately for offline short lifetime controls.
Only sizes, header categories, counts, hashes and error class names are exported.
"""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import ssl
import sys
import time
from types import SimpleNamespace
from urllib.request import Request, urlopen
from datetime import datetime, timezone

SOURCE = Path('/opt/arbitrage-hub')
EXPECTED = {
    'app.py': '1c4fc4f972dcd5748dac9596b7fc981a793f69169030845825e0a23526595eae',
    'market_transport.py': '968f2b2c2daba7704a35ced5d631d9cb0496c2a4f6341418622ee52e74c30d21',
}


def main():
    root = Path(sys.argv[1]); root.mkdir(mode=0o700)
    os.nice(19)
    resource.setrlimit(resource.RLIMIT_AS, (256*1024**2, 256*1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
    sys.dont_write_bytecode = True
    for name, sha in EXPECTED.items():
        assert hashlib.sha256((SOURCE/name).read_bytes()).hexdigest() == sha
    constants = {}
    for node in ast.parse((SOURCE/'equity_spot.py').read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in ('EXTRA_URLS', 'METADATA_TTLS'):
            constants[node.targets[0].id] = ast.literal_eval(node.value)
    assert set(constants['METADATA_TTLS']) == {'equity_gate_pairs', 'equity_gate_coins', 'equity_bitget_instruments', 'equity_bitget_stocks', 'equity_bybit_instruments'}
    assert all(constants['EXTRA_URLS'][k].startswith(('https://api.gateio.ws/api/v4/spot/', 'https://api.bitget.com/api/v3/', 'https://api.bybit.com/v5/market/')) for k in constants['METADATA_TTLS'])
    spec = importlib.util.spec_from_file_location('pinned_transport', SOURCE/'market_transport.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    context = ssl.create_default_context()
    current = {}

    class Response:
        def __init__(self, response):
            self.response = response; self.headers = response.headers
        def __enter__(self): return self
        def __exit__(self, *args): self.response.close()
        def read(self, size):
            assert size == 16*1024*1024+1
            body = self.response.read(size)
            assert len(body) <= 16*1024*1024
            current.update(read_request_bytes=size, wire_bytes=len(body), wire_sha256=hashlib.sha256(body).hexdigest(),
                           content_length=self.headers.get('Content-Length'), encoding=self.headers.get('Content-Encoding', ''),
                           transfer_encoding=self.headers.get('Transfer-Encoding', ''), status=self.response.status)
            (root/(current['key']+'.wire')).write_bytes(body)
            return body

    def open_public(request, **kwargs):
        assert request.full_url == constants['EXTRA_URLS'][current['key']]
        return Response(urlopen(request, **kwargs))

    node = next(n for n in ast.parse((SOURCE/'app.py').read_text()).body if isinstance(n, ast.FunctionDef) and n.name == 'get_json')
    env = dict(Request=Request, urlopen=open_public, MARKET_HTTP=SimpleNamespace(context=context), decode_json=module.decode_json)
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'pinned-app-get-json', 'exec'), env)
    report = {'started_at_utc': datetime.now(timezone.utc).isoformat(), 'source_sha256': dict(EXPECTED), 'directories': []}
    report['source_sha256']['equity_spot.py'] = hashlib.sha256((SOURCE/'equity_spot.py').read_bytes()).hexdigest()
    for key, ttl in constants['METADATA_TTLS'].items():
        current.clear(); current.update(key=key, ttl_seconds=ttl)
        started = time.monotonic()
        try:
            payload = env['get_json'](constants['EXTRA_URLS'][key], timeout=3)
            raw = (root/(key+'.wire')).read_bytes()
            decoded, decoded_length = module.decode_json(raw, current['encoding'])
            assert decoded == payload
            if isinstance(payload, list): rows = payload
            else:
                rows = payload.get('data', payload.get('result', {}).get('list', []))
            current.update(ok=True, decoded_bytes=decoded_length, rows=len(rows) if isinstance(rows, list) else None,
                           payload_kind=type(payload).__name__)
            del payload, decoded, rows, raw
        except Exception as exc:
            current.update(ok=False, error_class=type(exc).__name__)
            if hasattr(exc, 'close'): exc.close()
        current['elapsed_seconds'] = time.monotonic()-started
        report['directories'].append(dict(current))
    report['ended_at_utc'] = datetime.now(timezone.utc).isoformat()
    report['source_unchanged'] = all(hashlib.sha256((SOURCE/k).read_bytes()).hexdigest() == v for k, v in report['source_sha256'].items())
    (root/'manifest.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report))


if __name__ == '__main__': main()

"""Local short control for a specific possible allocation amplifier.

Production-matched WS decoder + AST-only public parse_frame. Synthetic messages,
no network, database, app import, forced GC, trim or long-duration loop.
"""
import ast
import gc
import hashlib
import importlib.metadata
import inspect
import json
from pathlib import Path
import platform
import random
import sys
import time
import tracemalloc
from types import SimpleNamespace
import weakref
import zlib

from websockets.extensions.permessage_deflate import PerMessageDeflate
from websockets.exceptions import PayloadTooBig
from websockets.frames import Frame, Opcode

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / 'memory-code-audit-20261113/production-source/anomaly_streams.py'
remote = json.loads((ROOT / 'production-ws-decoder.json').read_text())
assert hashlib.sha256(inspect.getsource(PerMessageDeflate.decode).encode()).hexdigest() == remote['decoder_source_sha256']
assert importlib.metadata.version('websockets') == remote['websockets']
tree = ast.parse(SOURCE.read_text())
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'parse_frame')
namespace = {'json': json}
exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), namespace)
parse_frame = namespace['parse_frame']


def extension(no_context):
    return PerMessageDeflate(no_context, no_context, 15, 15)


def fixtures(kind):
    rows = []
    randomizer = random.Random(8675309)
    padding = ''.join(randomizer.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(32768))
    count = 600 if kind != 'large_ignored_fields' else 32
    for i in range(count):
        symbol = f'SYN{i % 32:02d}USDT'
        stamp = 1794500000000 + i * 100
        seq = i + 1
        if kind == 'bybit_deltas':
            snapshot = i < 32
            bid = str(10000 + i)
            data = {'symbol': symbol, 'bid1Price': bid}
            if snapshot:
                data.update(ask1Price=str(20000 + i), bid1Size='12', ask1Size='15')
            payload = {'topic': 'tickers.' + symbol, 'type': 'snapshot' if snapshot else 'delta',
                       'ts': stamp, 'cs': seq, 'data': data}
            feed = 'bybit'
            expected = [dict(symbol=symbol, bid=float(bid), ask=float(20000 + i % 32),
                             bid_qty=12., ask_qty=15., at=stamp / 1000, sequence=seq,
                             reset=False, timestamp_source='exchange', sequence_kind='bybit_ticker')]
        else:
            payload = {'s': symbol, 'b': '1.0001', 'a': '1.0002', 'B': '12', 'A': '15', 'E': stamp, 'u': seq}
            if kind == 'large_ignored_fields':
                payload['unused'] = {'blob': padding, 'nested': [{'value': i}]}
            feed = 'bn_future'
            expected = [dict(symbol=symbol, bid=1.0001, ask=1.0002, bid_qty=12., ask_qty=15.,
                             at=stamp / 1000, sequence=seq, reset=False, timestamp_source='exchange')]
        rows.append((feed, json.dumps(payload, separators=(',', ':')).encode(), expected))
    return rows


def encoded_rows(rows, no_context, fragmented):
    encoder = extension(no_context)
    output = []
    for feed, raw, expected in rows:
        if fragmented:
            half = len(raw) // 2
            frames = [Frame(Opcode.TEXT, raw[:half], fin=False), Frame(Opcode.CONT, raw[half:], fin=True)]
        else:
            frames = [Frame(Opcode.TEXT, raw)]
        output.append((feed, raw, expected, tuple(encoder.encode(f) for f in frames)))
    return output


def exercise(encoded, no_context, cap, lifetime=False):
    decoder = extension(no_context)
    state = {}
    live = weakref.WeakValueDictionary()
    peak_live = 0
    class TrackedDict(dict):
        pass
    def hook(values):
        nonlocal peak_live
        obj = TrackedDict(values)
        live[id(obj)] = obj
        peak_live = max(peak_live, len(live))
        return obj
    namespace['json'] = SimpleNamespace(loads=lambda raw: json.loads(raw, object_hook=hook)) if lifetime else json
    if not lifetime:
        tracemalloc.start()
    max_surviving = 0
    try:
        for feed, raw, expected, frames in encoded:
            parts = []
            remaining = cap
            for frame in frames:
                decoded = decoder.decode(frame, max_size=remaining)
                remaining -= len(decoded.data)
                parts.append(decoded.data)
            data = b''.join(parts)
            assert data == raw
            result = parse_frame(feed, data, 1794500000, state)
            assert result == expected
            if lifetime:
                # Keep the returned public quotes and state alive while checking.
                max_surviving = max(max_surviving, len(live))
                assert not live
        if not lifetime:
            current, peak = tracemalloc.get_traced_memory()
        else:
            current = peak = None
        assert len(state) <= 32
        return {'messages': len(encoded), 'cap_bytes': cap, 'peak_traced_bytes': peak,
                'current_traced_bytes': current, 'max_decoded_dicts_alive_inside_parse': peak_live if lifetime else None,
                'max_decoded_dicts_alive_after_parse': max_surviving if lifetime else None,
                'final_state_symbols': len(state), 'all_bytes_and_public_values_equal': True}
    finally:
        namespace['json'] = json
        if not lifetime:
            tracemalloc.stop()


began = time.monotonic()
results = []
for kind, fragmented, no_context in (
    ('small', False, True), ('small', False, False),
    ('small', True, False), ('bybit_deltas', False, False),
    ('large_ignored_fields', False, True), ('large_ignored_fields', True, False),
):
    rows = fixtures(kind)
    encoded = encoded_rows(rows, no_context, fragmented)
    lower_cap = 4096 if kind != 'large_ignored_fields' else 64 * 1024
    measured = [exercise(encoded, no_context, cap) for cap in (lower_cap, 1024**2)]
    lifetime = exercise(encoded, no_context, 1024**2, lifetime=True)
    results.append({'kind': kind, 'fragmented': fragmented, 'no_context_takeover': no_context,
                    'max_body_bytes': max(len(r[1]) for r in rows),
                    'max_wire_frame_bytes': max(len(f.data) for row in encoded for f in row[-1]),
                    'fixture_sha256': hashlib.sha256(b'\n'.join(r[1] for r in rows)).hexdigest(),
                    'expected_full_values_sha256': hashlib.sha256(json.dumps([r[2] for r in rows],sort_keys=True).encode()).hexdigest(),
                    'comparisons': measured, 'lifetime': lifetime})
oversize = encoded_rows([('bn_future', b'x' * (1024**2 + 1), [])], True, False)[0][-1][0]
try:
    extension(True).decode(oversize, max_size=1024**2)
except PayloadTooBig:
    oversize_rejected = True
else:
    raise AssertionError('Oversize accepted')
assert gc.isenabled()
report = {'elapsed_seconds': time.monotonic() - began, 'python': sys.version.split()[0],
          'platform': platform.system(), 'websockets': importlib.metadata.version('websockets'),
          'local_zlib': zlib.ZLIB_RUNTIME_VERSION, 'production_python': remote['python'],
          'production_zlib': remote['zlib'], 'decoder_source_sha256': remote['decoder_source_sha256'],
          'parser_source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
          'cases': results, 'oversize_rejected': oversize_rejected,
          'forced_gc_calls': 0, 'network_or_database_calls': 0,
          'boundary': 'Local Python allocation/lifetime control only. Exact WS decoder source/version, but different OS/Python patch/zlib; not a Linux RSS or production root-cause verdict.'}
print(json.dumps(report, ensure_ascii=False, indent=2))

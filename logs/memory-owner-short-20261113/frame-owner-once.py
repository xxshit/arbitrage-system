"""Prepared only. Requires a new explicit approval; old sampling grant is spent.

One <=30s nonblocking 5Hz frame-metadata sample, with idle threads included.
py-spy visits all threads; only approved target TIDs' allowlisted function/line
counts leave the anonymous pipe. No locals, arguments, raw output files or order.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone


def utc():
    return datetime.now(timezone.utc).isoformat()


def identity(manifest):
    props = dict(line.split('=', 1) for line in subprocess.check_output(
        ['systemctl', 'show', 'arbitrage-private', '-p', 'MainPID', '-p', 'InvocationID'], text=True).splitlines())
    assert props['InvocationID'] == manifest['identity']['invocation']
    parent = props['MainPID']
    assert Path(f'/proc/{parent}/task/{parent}/children').read_text().split() == [str(manifest['identity']['worker'])]
    pid = manifest['identity']['worker']
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    assert int(fields[19]) == manifest['identity']['start_ticks']
    for tid, start in manifest['target_threads'].items():
        fields = Path(f'/proc/{pid}/task/{tid}/stat').read_text().rsplit(')', 1)[1].split()
        assert int(fields[19]) == start


def pressure_ok():
    values = dict(item.split('=', 1) for item in Path('/proc/pressure/memory').read_text().splitlines()[0].split()[1:])
    assert float(values['avg10']) <= 5
    info = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    assert int(info['MemAvailable'].split()[0]) >= 512 * 1024


def counters(manifest):
    pid = manifest['identity']['worker']
    status = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
    threads = {}
    for tid in manifest['target_threads']:
        fields = Path(f'/proc/{pid}/task/{tid}/stat').read_text().rsplit(')', 1)[1].split()
        threads[tid] = {'minor_faults': int(fields[7]), 'major_faults': int(fields[9]),
                        'cpu_ticks': int(fields[11]) + int(fields[12])}
    return {'at_utc': utc(), 'rss_bytes': int(status['VmRSS'].split()[0]) * 1024,
            'anonymous_bytes': int(status['RssAnon'].split()[0]) * 1024, 'threads': threads}


def quote_age():
    prefix = "SET SESSION time_zone='SYSTEM'; SET SESSION TRANSACTION READ ONLY; SET SESSION max_execution_time=1000; "
    query = "SELECT MAX(TIMESTAMPDIFF(SECOND, latest_at, UTC_TIMESTAMP())) FROM (SELECT MAX(captured_at) latest_at FROM latest_market_snapshot UNION ALL SELECT MAX(captured_at) FROM latest_dual_futures_snapshot) q;"
    result = subprocess.check_output(['mysql', '--batch', '--skip-column-names', '--database=arbitrage_hub', '-e', prefix + query], text=True, timeout=3).strip()
    age = int(result)
    assert 0 <= age <= 30
    return age


def limits():
    os.nice(19)
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))


def main():
    assert sys.argv[1:] == ['--new-explicit-approval'], 'Approval required; preparation never invokes sampler.'
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / 'frame-manifest.json').read_text())
    spec = importlib.util.spec_from_file_location('frame_aggregate', root / 'frame-aggregate.py')
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    parser.privacy_check()
    identity(manifest)
    pressure_ok()
    before_age = quote_age()
    binary = Path('/tmp/arbiscope-function-sample.c4slh8rc/py-spy')
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == 'a41205d4925beafd108e1ace85e66c0a1a7adbfcbebcd4441f07848b608886c0'
    for name, record in manifest['sources'].items():
        assert hashlib.sha256((Path('/opt/arbitrage-hub') / name).read_bytes()).hexdigest() == record['sha256']
    (root / 'new-sample-once.lock').touch(exist_ok=False)
    agg = parser.Aggregator(manifest)
    pipe = {'stdout_bytes': 0, 'stderr_bytes': 0, 'errors': 0}
    first = counters(manifest)
    began, started = time.monotonic(), utc()
    command = [str(binary), 'record', '--pid', str(manifest['identity']['worker']),
               '--duration', '30', '--rate', '5', '--threads', '--idle', '--nonblocking',
               '--format', 'raw', '--output', '/dev/stdout']
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding='utf8', errors='replace', preexec_fn=limits,
        env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    def deadline():
        if child.poll() is None:
            child.terminate()
    deadline_timer = threading.Timer(30, deadline)
    deadline_timer.start()
    def read_output():
        try:
            while True:
                line = child.stdout.readline(parser.MAX_LINE + 1)
                if not line:
                    break
                pipe['stdout_bytes'] += len(line.encode())
                agg.add(line)
                if len(line) > parser.MAX_LINE:
                    while line and not line.endswith('\n'):
                        line = child.stdout.readline(parser.MAX_LINE + 1)
                        pipe['stdout_bytes'] += len(line.encode())
        except Exception:
            pipe['errors'] += 1
    def discard_error():
        try:
            while chunk := child.stderr.read(4096):
                pipe['stderr_bytes'] += len(chunk.encode())
        except Exception:
            pipe['errors'] += 1
    readers = [threading.Thread(target=read_output, daemon=True), threading.Thread(target=discard_error, daemon=True)]
    for reader in readers:
        reader.start()
    abort = None
    last_gate = began
    try:
        while child.poll() is None:
            if time.monotonic() - began >= 30:
                abort = 'bounded_wall_clock_end'
                break
            identity(manifest)
            if time.monotonic() - last_gate >= 5:
                pressure_ok()
                quote_age()
                last_gate = time.monotonic()
            time.sleep(.5)
    except Exception:
        abort = 'identity_resource_or_quote_gate'
    finally:
        if child.poll() is None:
            child.terminate()  # The sampler child only, never the website.
        try:
            code = child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            code = child.wait(timeout=2)
        for reader in readers:
            reader.join(timeout=2)
        deadline_timer.cancel()
    report = {'started_at_utc': started, 'ended_at_utc': utc(),
              'elapsed_seconds': time.monotonic() - began, 'sampler_exit': code,
              'abort': abort, 'pipe_counts': pipe, 'aggregate': agg.result(),
              'identity': manifest['identity'], 'quote_age_before': before_age,
              'counters_before': first, 'counters_after': counters(manifest),
              'boundary': 'Function/line frequency, not retained-allocation attribution. No repeat.'}
    identity(manifest)
    report['quote_age_after'] = quote_age()
    print(json.dumps(report))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'safe_failure': type(exc).__name__, 'at_utc': utc()}))
        raise SystemExit(1)

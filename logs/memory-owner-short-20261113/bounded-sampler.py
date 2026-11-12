"""Bounded child lifetime with end-of-run output draining, never raw output.

The caller must reserve time between the sampler's collection duration and this
outer hard deadline. A deadline kill is a failed sample, even if exit is handled.
"""
import subprocess
import threading
import time


def collect(command, aggregator, max_line, guard, *, deadline_seconds=30, constrain=None):
    pipe = {'stdout_bytes': 0, 'stderr_bytes': 0, 'errors': 0}
    began = time.monotonic()
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding='utf8', errors='replace', preexec_fn=constrain,
        env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    expired = threading.Event()
    def deadline():
        if child.poll() is None:
            expired.set()
            child.terminate()
    timer = threading.Timer(max(0, deadline_seconds - (time.monotonic() - began)), deadline)
    timer.start()
    def read_output():
        try:
            while True:
                line = child.stdout.readline(max_line + 1)
                if not line:
                    break
                pipe['stdout_bytes'] += len(line.encode())
                aggregator.add(line)
                if len(line) > max_line:
                    while line and not line.endswith('\n'):
                        line = child.stdout.readline(max_line + 1)
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
    try:
        while child.poll() is None:
            guard()
            time.sleep(.05)
    except Exception:
        abort = 'identity_resource_or_quote_gate'
    finally:
        if child.poll() is None:
            child.terminate()
        try:
            code = child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            code = child.wait(timeout=2)
        for reader in readers:
            reader.join(timeout=2)
        timer.cancel()
        timer.join(timeout=1)
        child.stdout.close()
        child.stderr.close()
    if expired.is_set():
        abort = 'bounded_wall_clock_end'
    if any(reader.is_alive() for reader in readers):
        abort = 'output_drain_failure'
    return {'elapsed_seconds': time.monotonic() - began, 'sampler_exit': code,
            'abort': abort, 'pipe_counts': pipe, 'aggregate': aggregator.result()}

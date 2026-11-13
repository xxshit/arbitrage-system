"""CPython allocator accounting for the existing minute health log.

Only allocator pool counts and byte totals, never Python objects, pointers,
frames, values, or allocation traces. No process attachment or allocator changes.
The C entry point writes to our own anonymous FILE, never process stderr.
"""
import ctypes
import re
import sys
import threading
import time

_LIMIT = 8192
_LOCK = threading.Lock()
_LAST = None
_LAST_AT = None
_READER = None
_FAILURE = None


def _parse(raw):
    if not isinstance(raw, bytes) or len(raw) > _LIMIT:
        raise ValueError('allocator_statistics_size')
    text = raw.decode('ascii')
    values = {}
    for label, name in (
        ('arenas allocated current', 'arenas'),
        ('arenas reclaimed', 'arenas_reclaimed'),
        ('bytes in allocated blocks', 'allocated_bytes'),
        ('bytes in available blocks', 'available_bytes'),
        ('bytes lost to pool headers', 'pool_headers_bytes'),
        ('bytes lost to quantization', 'quantization_bytes'),
        ('bytes lost to arena alignment', 'alignment_bytes'),
    ):
        match = re.search(r'^# ' + re.escape(label) + r'\s*=\s*([\d,]+)$', text, re.M)
        if not match:
            raise ValueError('allocator_statistics_format')
        values[name] = int(match[1].replace(',', ''))
    for label, name in ((r'(\d+) arenas \* (\d+) bytes/arena', 'arena_bytes'),
                        (r'(\d+) unused pools \* (\d+) bytes', 'unused_pools_bytes')):
        match = re.search(r'^' + label + r'\s*=\s*([\d,]+)$', text, re.M)
        if not match:
            raise ValueError('allocator_statistics_format')
        values[name] = int(match[3].replace(',', ''))
        if int(match[1]) * int(match[2]) != values[name]:
            raise ValueError('allocator_statistics_sum')
        if name == 'arena_bytes' and int(match[1]) != values['arenas']:
            raise ValueError('allocator_statistics_arenas')
    if values['arena_bytes'] != sum(values[k] for k in (
        'allocated_bytes', 'available_bytes', 'unused_pools_bytes',
        'pool_headers_bytes', 'quantization_bytes', 'alignment_bytes')):
        raise ValueError('allocator_statistics_balance')
    # Fixed 32 size classes; aggregate counts only. No addresses or type names.
    classes = []
    for match in re.finditer(r'^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$', text, re.M):
        index, size, pools, used, free = map(int, match.groups())
        if index >= 32 or size != (index + 1) * 16:
            raise ValueError('allocator_statistics_class')
        classes.append((size, pools, used, free))
    if len(classes) > 32 or len({r[0] for r in classes}) != len(classes):
        raise ValueError('allocator_statistics_classes')
    if sum(size * used for size, _, used, _ in classes) != values['allocated_bytes']:
        raise ValueError('allocator_statistics_used')
    if sum(size * free for size, _, _, free in classes) != values['available_bytes']:
        raise ValueError('allocator_statistics_available')
    values['classes'] = tuple(classes)
    return values


class _Reader:
    def __init__(self):
        # This is an internal CPython interface: use only the validated ABI.
        if sys.platform != 'linux' or sys.implementation.name != 'cpython' or sys.version_info[:2] != (3, 12):
            raise RuntimeError('unsupported_interpreter')
        self.libc = ctypes.CDLL(None)
        self.tmpfile = self.libc.tmpfile
        self.tmpfile.argtypes, self.tmpfile.restype = [], ctypes.c_void_p
        self.close = self.libc.fclose
        self.close.argtypes, self.close.restype = [ctypes.c_void_p], ctypes.c_int
        self.flush = self.libc.fflush
        self.flush.argtypes, self.flush.restype = [ctypes.c_void_p], ctypes.c_int
        self.seek = self.libc.fseek
        self.seek.argtypes, self.seek.restype = [ctypes.c_void_p, ctypes.c_long, ctypes.c_int], ctypes.c_int
        self.read = self.libc.fread
        self.read.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_void_p]
        self.read.restype = ctypes.c_size_t
        # pythonapi is PyDLL: keep the GIL while CPython counts its own pools.
        self.stats = ctypes.pythonapi._PyObject_DebugMallocStats
        self.stats.argtypes, self.stats.restype = [ctypes.c_void_p], ctypes.c_int

    def __call__(self):
        sink = self.tmpfile()
        if not sink:
            raise OSError('allocator_statistics_file')
        try:
            if self.stats(sink) != 1:
                raise RuntimeError('allocator_statistics_unavailable')
            if self.flush(sink) or self.seek(sink, 0, 0):
                raise OSError('allocator_statistics_io')
            buffer = ctypes.create_string_buffer(_LIMIT + 1)
            count = self.read(buffer, 1, _LIMIT + 1, sink)
            return _parse(buffer.raw[:count])
        finally:
            self.close(sink)


def python_allocator_status():
    """At most once per minute, only from the existing health thread.

    API request threads get the last immutable numeric snapshot, without C
    collection. Unsupported ABI/format or any collection error disables this
    optional diagnostic for this interpreter; normal service work continues.
    """
    global _LAST, _LAST_AT, _READER, _FAILURE
    if threading.current_thread().name != 'runtime-health':
        return dict(_LAST) if _LAST is not None else {'available': False}
    if not _LOCK.acquire(blocking=False):
        return dict(_LAST) if _LAST is not None else {'available': False}
    try:
        now = time.monotonic()
        if _LAST_AT is not None and now - _LAST_AT < 60:
            return dict(_LAST)
        if _FAILURE:
            return {'available': False, 'error_type': _FAILURE}
        started = time.perf_counter()
        try:
            if _READER is None:
                _READER = _Reader()
            result = _READER()
            result.update(available=True, sampled_at_epoch=time.time(),
                          elapsed_seconds=round(time.perf_counter() - started, 6))
            _LAST, _LAST_AT = result, now
            return dict(result)
        except Exception as error:
            # Only a fixed exception category, not messages/contents/tracebacks.
            _FAILURE = type(error).__name__
            _LAST = {'available': False, 'error_type': _FAILURE}
            _LAST_AT = now
            return dict(_LAST)
    finally:
        _LOCK.release()

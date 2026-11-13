import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import runtime_allocator as allocator


STATS = b'''Small block threshold = 512, in 32 size classes.
class size num pools blocks in use avail blocks
    0     16     1     2     1019
# arenas allocated current = 1
# arenas reclaimed = 0
1 arenas * 1048576 bytes/arena = 1,048,576
# bytes in allocated blocks = 32
# bytes in available blocks = 16,304
63 unused pools * 16384 bytes = 1,032,192
# bytes lost to pool headers = 48
# bytes lost to quantization = 0
# bytes lost to arena alignment = 0
'''


class RuntimeAllocatorTests(unittest.TestCase):
    def setUp(self):
        self.state = patch.multiple(allocator, _LAST=None, _LAST_AT=None,
                                    _READER=None, _FAILURE=None, _LOCK=threading.Lock())
        self.state.start()
        self.addCleanup(self.state.stop)

    def test_balances_size_class_bytes_against_arena_total(self):
        result = allocator._parse(STATS)
        self.assertEqual(result['allocated_bytes'], 32)
        self.assertEqual(result['unused_pools_bytes'], 1032192)
        self.assertEqual(result['classes'], ((16, 1, 2, 1019),))

    def test_rejects_truncated_inconsistent_and_oversized_totals(self):
        for raw in (STATS.replace(b'1,048,576', b'1,048,575'),
                    STATS.replace(b'= 32', b'= 48'),
                    STATS.replace(b'2     1019', b'3     1019'),
                    STATS + b'x' * allocator._LIMIT,
                    STATS.split(b'# arenas reclaimed')[0]):
            with self.subTest(raw_length=len(raw)), self.assertRaises(ValueError):
                allocator._parse(raw)

    def test_request_thread_never_collects(self):
        with patch.object(allocator, '_Reader') as factory:
            self.assertEqual(allocator.python_allocator_status(), {'available': False})
            factory.assert_not_called()

    def test_health_collection_is_rate_limited_and_snapshot_is_owned(self):
        read = Mock(side_effect=lambda: allocator._parse(STATS))
        with patch.object(allocator, '_Reader', return_value=read), \
                patch.object(allocator.threading, 'current_thread', return_value=SimpleNamespace(name='runtime-health')), \
                patch.object(allocator.time, 'monotonic', side_effect=[10., 69., 70.]):
            first = allocator.python_allocator_status()
            first['arena_bytes'] = 0
            second = allocator.python_allocator_status()
            self.assertEqual(second['arena_bytes'], 1048576)
            allocator.python_allocator_status()
        self.assertEqual(read.call_count, 2)

    def test_busy_collection_never_waits(self):
        with patch.object(allocator.threading, 'current_thread', return_value=SimpleNamespace(name='runtime-health')), \
                patch.object(allocator, '_Reader') as factory:
            allocator._LOCK.acquire()
            try:
                self.assertEqual(allocator.python_allocator_status(), {'available': False})
            finally:
                allocator._LOCK.release()
            factory.assert_not_called()

    def test_failure_disables_collection_and_never_keeps_error_contents(self):
        read = Mock(side_effect=ValueError('PRIVATE_DATA_MUST_NOT_ESCAPE'))
        with patch.object(allocator, '_Reader', return_value=read), \
                patch.object(allocator.threading, 'current_thread', return_value=SimpleNamespace(name='runtime-health')), \
                patch.object(allocator.time, 'monotonic', side_effect=[1., 62.]):
            self.assertEqual(allocator.python_allocator_status(), {'available': False, 'error_type': 'ValueError'})
            self.assertEqual(allocator.python_allocator_status(), {'available': False, 'error_type': 'ValueError'})
        self.assertEqual(read.call_count, 1)
        self.assertEqual(allocator._FAILURE, 'ValueError')

    def test_unsupported_abi_does_not_open_native_handles(self):
        with patch.object(allocator.sys, 'version_info', (3, 13)), patch.object(allocator.ctypes, 'CDLL') as native:
            with self.assertRaises(RuntimeError):
                allocator._Reader()
            native.assert_not_called()

    def test_anonymous_stream_closed_on_unavailable_and_bad_output(self):
        for enabled in (0, 1):
            reader = allocator._Reader.__new__(allocator._Reader)
            reader.tmpfile = Mock(return_value=123)
            reader.close = Mock(return_value=0)
            reader.stats = Mock(return_value=enabled)
            reader.flush = Mock(return_value=0)
            reader.seek = Mock(return_value=0)
            reader.read = Mock(return_value=0)
            with self.assertRaises((RuntimeError, ValueError)):
                reader()
            reader.close.assert_called_once_with(123)


if __name__ == '__main__':
    unittest.main()

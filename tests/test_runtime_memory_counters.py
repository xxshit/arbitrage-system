"""Kernel metadata telemetry only; never import application bootstrap."""
import gc
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref

from background_runtime import WorkerRuntime, mysql_engine_options, thread_memory_counters


def task(tid, name):
    return SimpleNamespace(native_id=tid, name=name)


def stat(start=200, faults=123):
    fields = ['S'] + ['0'] * 30
    for key, value in {7: faults, 11: 21, 12: 3, 19: start}.items():
        fields[key] = str(value)
    return '777 (private name) with parens) ' + ' '.join(fields)


class RuntimeMemoryCounterTests(unittest.TestCase):
    def sample(self, threads, values=None):
        with patch('background_runtime.threading.enumerate', new=lambda: threads), \
             patch('background_runtime.Path.read_text', side_effect=values) as read:
            if values is None:
                read.side_effect = None
                read.return_value = stat()
            return thread_memory_counters()

    def test_fixed_roles_map_to_kernel_tid_and_start(self):
        result = self.sample([task(777, 'alert-stream-io'), task(778, 'market-http_7')])
        self.assertEqual(result['rows'], [
            dict(tid=777, start_ticks=200, role='alert-stream-io', minor_faults=123, cpu_ticks=24),
            dict(tid=778, start_ticks=200, role='market-http', minor_faults=123, cpu_ticks=24)])
        self.assertEqual((result['skipped'], result['truncated']), (0, 0))

    def test_unknown_names_and_malicious_suffix_not_exported(self):
        data = self.sample([task(1, 'user-private-account'), task(2, 'market-http_secret')])
        encoded = json.dumps(data)
        self.assertNotIn('private', encoded)
        self.assertNotIn('secret', encoded)
        self.assertEqual([r['role'] for r in data['rows']], ['other', 'other'])

    def test_exiting_or_unstarted_threads_do_not_fail_health(self):
        result = self.sample([task(None, 'MainThread'), task(1, 'market-http_0'),
                              task(2, 'spot-market-refresh')], [FileNotFoundError(), stat()])
        self.assertEqual(result['skipped'], 2)
        self.assertEqual(result['rows'][0]['tid'], 2)

    def test_unreadable_and_malformed_stat_is_bounded(self):
        result = self.sample([task(1, 'MainThread'), task(2, 'MainThread')],
                             [PermissionError(), 'bad'])
        self.assertEqual(result, dict(rows=[], enumerated=2, skipped=2, truncated=0))

    def test_oversized_thread_list_reports_truncation(self):
        result = self.sample([task(i+1, 'market-http_0') for i in range(300)])
        self.assertEqual(len(result['rows']), 256)
        self.assertEqual(result['truncated'], 44)

    def test_reused_tid_is_distinguished_by_start_ticks(self):
        a = self.sample([task(7, 'market-http_0')], [stat(100)])
        b = self.sample([task(7, 'market-http_0')], [stat(300)])
        self.assertNotEqual(a['rows'][0]['start_ticks'], b['rows'][0]['start_ticks'])

    def test_no_thread_object_or_sample_history_is_retained(self):
        class Thread:
            native_id, name = 9, 'alert-stream-io'
        item = Thread()
        reference = weakref.ref(item)
        before = gc.isenabled(), gc.get_threshold()
        result = self.sample([item])
        del item
        self.assertIsNone(reference())
        self.assertEqual((gc.isenabled(), gc.get_threshold()), before)
        self.assertEqual(set(result['rows'][0]),
                         {'tid', 'start_ticks', 'role', 'minor_faults', 'cpu_ticks'})

    def test_worker_shutdown_and_connection_limits_are_preserved(self):
        runtime = WorkerRuntime()
        class Stop(BaseException):
            pass
        logger = Mock()
        with self.assertRaises(Stop):
            runtime.guard(lambda: (_ for _ in ()).throw(Stop()), logger)()
        logger.warning.assert_not_called()
        options = mysql_engine_options('mysql+pymysql://synthetic')
        self.assertEqual(options['pool_size']+options['max_overflow'], 15)
        record = object()
        runtime.checkout(None, record, None)
        self.assertEqual(runtime.snapshot()['db_checked_out'], 1)
        runtime.checkin(None, record)
        self.assertEqual(runtime.snapshot()['db_checked_out'], 0)


if __name__ == '__main__':
    unittest.main()

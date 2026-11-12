"""Synthetic SQLite and object ownership checks; no application startup."""
from array import array
import hashlib
import json
import tempfile
import unittest
import weakref
from unittest.mock import patch

import opportunity_history as history
from spread_history import HistoryStore, encode_chunk

NOW = 1792886400 + 12*3600 + 30


class Decoded(dict):
    pass


class ProfileStreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = HistoryStore(self.temp.name)

    def write(self, symbol, metric, at, points):
        connection = self.store.connect(self.store.day(at), writable=True)
        try:
            with connection:
                connection.execute('INSERT OR REPLACE INTO history VALUES(?,?,?,?,?)',
                    (symbol, 'Gate', metric, int(at)//3600*3600, encode_chunk(points)))
        finally:
            connection.close()

    def fill(self, paths=5):
        for i in range(paths):
            for age in range(24):
                at = int(NOW)//3600*3600-age*3600
                for metric, value in (('open', .5+i), ('close', .625+i)):
                    self.write(f'SYN{i:04}/USDT', metric, at,
                        {str(at+m*300): [[[at+m*300, value]]] for m in range(12)})

    def test_cross_day_bins_extrema_and_close_values(self):
        self.fill()
        cache = history.ProfileCache()
        self.assertEqual(cache.refresh(self.store, NOW), 5)
        for i, value in enumerate(cache.snapshot()[0].values()):
            self.assertTrue(value['ready'])
            self.assertTrue(value['close_ready'])
            self.assertEqual(value['median'], .5+i)
            self.assertEqual(value['exit_q75'], .625+i)
            self.assertEqual(value['bins'], 23*12+1)
            self.assertEqual(value['windows']['24h']['range'], 0)

    def test_two_work_arrays_and_one_decoded_graph_at_a_time(self):
        self.fill(40)
        arrays, decoded = [], []
        live_counts = []
        class Array(array):
            def __mul__(self, count):
                result = Array(self.typecode, super().__mul__(count))
                if len(result) == 289:
                    arrays.append(weakref.ref(result))
                    live_counts.append(sum(ref() is not None for ref in arrays))
                return result
        original = history.decode_chunk
        def decode(payload):
            self.assertTrue(all(ref() is None for ref in decoded), 'old decoded hour overlaps next decode')
            result = Decoded(original(payload))
            decoded.append(weakref.ref(result))
            return result
        with patch.object(history, 'array', Array), patch.object(history, 'decode_chunk', decode):
            cache = history.ProfileCache()
            cache.refresh(self.store, NOW)
        self.assertEqual(max(live_counts), 2)
        self.assertEqual(len(arrays), 80)
        self.assertTrue(all(ref() is None for ref in arrays+decoded))

    def test_late_revision_deletion_and_moving_boundary_are_visible(self):
        at = NOW-7*86400
        self.write('BOUNDARY/USDT', 'open', at,
                   {str(int(at)//60*60): [[[at-1, 99], [at+1, .7], [at+20, .8]]]})
        cache = history.ProfileCache()
        cache.refresh(self.store, NOW)
        self.assertEqual(cache.snapshot()[0][('BOUNDARY/USDT','Gate')]['windows']['7d']['high'], .8)
        cache.refresh(self.store, NOW+2)
        self.assertEqual(cache.snapshot()[0][('BOUNDARY/USDT','Gate')]['windows']['7d']['low'], .8)
        old_arrays = [weakref.ref(v) for _, ranges in cache.days.values() for v in ranges.values()]
        self.write('BOUNDARY/USDT', 'open', at, {str(at): [[[at+20, -1.5]]]})
        cache.refresh(self.store, NOW+3)
        self.assertEqual(cache.snapshot()[0][('BOUNDARY/USDT','Gate')]['windows']['7d']['low'], -1.5)
        self.assertTrue(all(ref() is None for ref in old_arrays))
        connection = self.store.connect(self.store.day(at), writable=True)
        try:
            with connection:
                connection.execute('DELETE FROM history')
        finally:
            connection.close()
        cache.refresh(self.store, NOW+4)
        self.assertEqual(cache.snapshot()[0], {})

    def test_previous_reader_survives_publish_then_releases_without_gc(self):
        self.fill()
        cache = history.ProfileCache()
        cache.refresh(self.store, NOW)
        old, _ = cache.snapshot()
        class Marker:
            pass
        marker = Marker()
        ref = weakref.ref(marker)
        old['synthetic-reader-marker'] = marker
        del marker
        cache.refresh(self.store, NOW+5)
        self.assertIsNotNone(ref())
        self.assertNotIn('synthetic-reader-marker', cache.snapshot()[0])
        del old
        self.assertIsNone(ref())

    def test_decode_failure_closes_both_readers_and_does_not_publish(self):
        self.fill()
        cache = history.ProfileCache()
        cache.refresh(self.store, NOW)
        before = cache.snapshot()
        connections = []
        connect = self.store.connect
        def tracked(*args, **kwargs):
            conn = connect(*args, **kwargs)
            connections.append(conn)
            return conn
        # Focus on the two-day streaming branch, after the range summary stage.
        with patch.object(cache, 'window_ranges', return_value={}), patch.object(self.store, 'connect', tracked), \
             patch.object(history, '_consume_profile_hour', side_effect=ValueError('generated malformed chunk')):
            with self.assertRaises(ValueError):
                cache.refresh(self.store, NOW+5)
        self.assertIs(cache.snapshot()[0], before[0])
        self.assertEqual(cache.snapshot()[1], before[1])
        self.assertEqual(len(connections), 2)
        for conn in connections:
            with self.assertRaisesRegex(Exception, 'closed'):
                conn.execute('SELECT 1')

    def test_day_first_path_limit_preserved_with_interleaved_new_symbols(self):
        prior = NOW-23*3600
        for i in range(4000):
            self.write(f'Z{i:04}/USDT', 'open', prior, {str(prior): [[[prior, .5]]]})
        self.write('A_NEW/USDT', 'open', NOW, {str(NOW): [[[NOW, 9]]]})
        self.write('Z0000/USDT', 'close', NOW, {str(NOW): [[[NOW, .75]]]})
        cache = history.ProfileCache()
        with patch.object(cache, 'window_ranges', return_value={}):
            cache.refresh(self.store, NOW)
        data, _ = cache.snapshot()
        self.assertEqual(len(data), 4000)
        self.assertNotIn(('A_NEW/USDT','Gate'), data)
        self.assertEqual(data[('Z0000/USDT','Gate')]['exit_q75'], .75)
        self.assertEqual(next(iter(data)), ('Z0000/USDT','Gate'))


if __name__ == '__main__':
    unittest.main()

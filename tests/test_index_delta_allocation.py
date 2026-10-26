"""Allocation and immutable-reader contracts for incremental public history."""
import json
import threading
import tracemalloc
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from alert_history_index import AlertHistoryIndex


class HistoryDeltaAllocationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 26, 12)
        self.stamp = self.now - timedelta(hours=1)
        self.index = AlertHistoryIndex()
        self.count = 48000
        self.versions = {}
        self.new_ids = set()
        self.removed = set()

    def row(self, identity):
        version = self.versions.get(identity, 0)
        return (identity, 'SYNTH/USDT', 'spot_futures', 'rapid_spread', 'synthetic',
                version, -.5, .5, None, self.stamp, 'Gate', 'Binance', None, version)

    def load(self, identities, since):
        identities = range(self.count) if identities is None else set(identities) | self.new_ids
        return (self.row(i) for i in identities if i not in self.removed)

    def refresh(self):
        return self.index.snapshot(self.now-timedelta(hours=24), self.now,
            self.load, lambda: ((i, self.versions.get(i, 0))
                               for i in range(self.count) if i not in self.removed),
            lambda value: value, materialize=False)

    def peak_of_refresh(self):
        self.assertFalse(tracemalloc.is_tracing())
        tracemalloc.start()
        try:
            baseline, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            self.refresh()
            return tracemalloc.get_traced_memory()[1] - baseline
        finally:
            tracemalloc.stop()

    def test_no_change_does_not_allocate_another_full_history(self):
        self.refresh()
        previous = self.index.rows
        self.now += timedelta(seconds=5)
        # A delta with no rows needs only small staging metadata. A dictionary
        # copy at this history size costs >5MiB; leave ample allocator headroom.
        self.assertLess(self.peak_of_refresh(), 128 * 1024)
        self.assertEqual(len(self.index.rows), self.count)
        self.assertEqual(list(previous.items()), list(self.index.rows.items()))

    def test_one_revision_is_small_and_old_reader_keeps_every_value(self):
        self.refresh()
        previous = self.index.rows
        self.versions[12345] = 7
        self.index.invalidate([12345])
        self.now += timedelta(seconds=5)
        self.assertLess(self.peak_of_refresh(), 128 * 1024)
        self.assertEqual(len(self.index.rows), self.count)
        for identity, (event, _) in self.index.rows.items():
            self.assertEqual(event.open_spread, self.versions.get(identity, 0))
            self.assertEqual(previous[identity][0].open_spread, 0)

    def test_missing_dirty_id_does_not_copy_unrelated_history(self):
        self.refresh()
        self.removed.add(self.count + 99)
        self.index.invalidate([self.count + 99])
        self.assertLess(self.peak_of_refresh(), 128 * 1024)
        self.assertEqual(len(self.index.rows), self.count)

    def test_reader_can_finish_old_iteration_across_publication(self):
        self.count = 2048
        self.refresh()
        captured, release = threading.Event(), threading.Event()
        result, errors = [], []

        def reader():
            try:
                with self.index.cache_lock:
                    rows = self.index.rows
                iterator = iter(rows.values())
                first = next(iterator)
                captured.set()
                if not release.wait(2):
                    raise AssertionError('writer did not finish')
                result.extend([first, *iterator])
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=reader)
        worker.start()
        try:
            self.assertTrue(captured.wait(2))
            self.versions[9] = 2
            self.removed.add(17)
            self.new_ids.add(self.count)
            self.index.invalidate([9, 17, self.count])
            self.refresh()
            self.assertEqual(self.index.rows[9][0].open_spread, 2)
            self.assertNotIn(17, self.index.rows)
            self.assertIn(self.count, self.index.rows)
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual({event.id for event, _ in result}, set(range(self.count)))
        self.assertTrue(all(event.open_spread == 0 for event, _ in result))

    def test_rolling_expiry_audit_and_ineligible_revision_remain_exact(self):
        self.count = 2048
        data = {i: self.row(i) for i in range(self.count)}
        # A boundary entry stays for this publication and expires next refresh.
        data[4] = (*data[4][:9], self.now-timedelta(hours=24), *data[4][10:])
        data[8] = (*data[8][:12], json.dumps({}), 1)  # indexed revision, no eligible pulse
        clock = [10000.0]

        def load(ids, since):
            selected = data if ids is None else ids
            return (data[i] for i in selected if i in data)

        def refresh():
            self.index.snapshot(self.now-timedelta(hours=24), self.now, load,
                lambda: ((i, row[-1]) for i, row in data.items()),
                lambda value: json.loads(value) or None, materialize=False)

        with patch('alert_history_index.time.monotonic', new=lambda: clock[0]):
            refresh()
            before = self.index.rows
            self.assertIn(4, before)
            self.assertNotIn(8, before)
            self.assertEqual(self.index.revisions[8], 1)
            self.now += timedelta(seconds=5)
            refresh()
            self.assertNotIn(4, self.index.rows)
            self.assertIn(4, before)
            del data[16]  # external writer: only the revision audit observes it
            data[8] = (*data[8][:12], '{"peak":9}', 2)
            clock[0] += 901
            refresh()
            self.assertNotIn(16, self.index.rows)
            self.assertNotIn(4, self.index.rows)
            self.assertEqual(self.index.rows[8][1]['peak'], 9)
            self.assertEqual(self.index.revisions, {i: row[-1] for i, row in data.items()})
            self.assertEqual(set(self.index.rows), set(data) - {4})

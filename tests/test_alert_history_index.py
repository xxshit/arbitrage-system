import json
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock

from alert_history_index import AlertHistoryIndex


class HistoryIndexTests(unittest.TestCase):
    def setUp(self):
        self.index = AlertHistoryIndex()
        self.now = datetime(2026, 10, 19, 12)
        self.rows = {}
        self.loads = []
        self.project = Mock(side_effect=lambda raw: json.loads(raw) or None)

    def row(self, identity, age=1, evidence=None, revision=1):
        return (identity, f'COIN{identity}/USDT', 'spot_futures', 'rapid_spread', 'public history',
                .5, .6, .1, None, self.now-timedelta(seconds=age), 'Gate', 'Binance', evidence, revision)

    def load(self, identities, since):
        self.loads.append((identities, since))
        return [row for identity, row in self.rows.items()
                if self.now-timedelta(hours=24) <= row[9] <= self.now
                and (identities is None or identity in identities or (since is not None and row[9] > since))]

    def snapshot(self, load=None):
        return self.index.snapshot(self.now-timedelta(hours=24), self.now, load or self.load,
                                   lambda: ((row[0], row[-1]) for row in self.rows.values()), self.project)

    def test_unchanged_history_is_not_decoded_and_changed_id_replaces_snapshot(self):
        self.rows = {i:self.row(i, evidence=json.dumps({'peak':i})) for i in range(100)}
        before = self.snapshot()
        self.assertEqual(self.project.call_count, 100)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.project.call_count, 100)
        self.rows[7] = self.row(7, evidence='{"peak":700}', revision=2)
        self.index.invalidate([7])
        after = self.snapshot()
        self.assertEqual(self.project.call_count, 101)
        self.assertEqual(before[1][7]['peak'], 7)  # no mutation of in-flight reads
        self.assertEqual(after[1][7]['peak'], 700)

    def test_exact_window_future_entry_and_no_noise_as_legacy(self):
        self.rows = {1:self.row(1, age=86400), 2:self.row(2, age=86401),
                     3:self.row(3, age=-1), 4:self.row(4, evidence='{}')}
        self.assertEqual([row.id for row in self.snapshot()[0]], [1])
        self.now += timedelta(seconds=1)
        self.assertEqual([row.id for row in self.snapshot()[0]], [3])
        self.assertEqual(len(self.rows), 4)  # no DB retention/deletion side effect

    def test_failed_refresh_is_atomic_and_retries_same_ids(self):
        self.rows = {1:self.row(1, evidence='{"peak":1}'), 2:self.row(2)}
        self.snapshot()
        self.rows[1] = self.row(1, evidence='{"peak":2}', revision=2)
        self.index.invalidate([1, 2])
        def broken(identities, since):
            yield self.rows[1]
            raise RuntimeError('database unavailable')
        with self.assertRaises(RuntimeError):
            self.snapshot(broken)
        self.assertEqual(self.index.rows[1][1]['peak'], 1)
        self.assertEqual(self.snapshot()[1][1]['peak'], 2)
        self.assertEqual(self.loads[-1][0], {1, 2})

    def test_commit_during_read_is_not_acknowledged_by_older_snapshot(self):
        self.rows = {1:self.row(1, evidence='{"peak":1}')}
        def committing(identities, since):
            rows = self.load(identities, since)
            self.rows[1] = self.row(1, evidence='{"peak":2}', revision=2)
            self.index.invalidate([1])
            return rows
        self.assertEqual(self.snapshot(committing)[1][1]['peak'], 1)
        self.assertEqual(self.snapshot()[1][1]['peak'], 2)

    def test_schema_reset_race_and_clock_rewind_force_full_rebuild(self):
        self.rows = {1:self.row(1)}
        def resetting(identities, since):
            self.index.reset()
            return self.load(identities, since)
        with self.assertRaisesRegex(RuntimeError, 'generation_changed'):
            self.snapshot(resetting)
        self.snapshot()
        self.assertIsNone(self.loads[-1][0])
        self.now -= timedelta(seconds=2)
        self.snapshot()
        self.assertIsNone(self.loads[-1][0])

    def test_revision_audit_recovers_other_writer_update_and_delete(self):
        self.rows = {1:self.row(1, evidence='{"peak":1}'), 2:self.row(2)}
        self.snapshot()
        self.rows[1] = self.row(1, evidence='{"peak":2}', revision=2)
        self.rows.pop(2)
        self.index.last_audit = 0
        events, pulses = self.snapshot()
        self.assertEqual([row.id for row in events], [1])
        self.assertEqual(pulses[1]['peak'], 2)

    def test_closed_page_dirty_queue_is_bounded_without_dropping_history(self):
        self.snapshot()
        self.index.invalidate(range(10001))
        self.assertTrue(self.index.full)
        self.assertFalse(self.index.dirty)
        self.rows = {1:self.row(1)}
        self.assertEqual(len(self.snapshot()[0]), 1)

    def test_commit_notification_does_not_block_behind_history_read(self):
        with self.index.read_lock:
            completed = threading.Event()
            worker = threading.Thread(target=lambda: (self.index.invalidate([1]), completed.set()))
            worker.start()
            self.assertTrue(completed.wait(1))
            worker.join(1)

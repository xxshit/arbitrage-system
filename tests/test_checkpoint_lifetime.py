"""A retired pulse has no unsaved version and no future heartbeat to throttle."""
import copy
import unittest
import weakref
from unittest.mock import patch

from anomaly_streams import AlertStreamHub
import persistence_policy


PATH = ('spot_futures', 'SYNTHETIC/USDT', 'Gate', 'Binance')


class Watermark(dict):
    pass


class CheckpointLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.hub = AlertStreamHub()

    def observe(self, value, at):
        self.hub._updates(self.hub.detector.observe(PATH, 'basis', value, at, {'basis': value}))

    def new_pulse(self):
        self.observe(0, 1000)
        self.observe(.6, 1001)
        rows = self.hub.batch(now=1001)
        self.assertEqual(len(rows), 1)
        return rows

    def test_reset_releases_committed_watermark_without_gc(self):
        create = persistence_policy.pulse_checkpoint
        refs = []
        def checkpoint(pulse, at):
            result = Watermark(create(pulse, at));refs.append(weakref.ref(result));return result
        with patch.object(persistence_policy, 'pulse_checkpoint', checkpoint):
            first = self.new_pulse()
            self.hub.acknowledge(first, now=1001)
            self.assertIsNotNone(refs[0]())
            self.hub._updates(self.hub.detector.reset(PATH, 1002))
            final = self.hub.batch(now=1002)
            self.assertEqual(final[0]['state'], 'data_gap')
            self.assertEqual(final[0]['peak'], .6)
            self.hub.acknowledge(final, now=1002)
        self.assertIsNone(refs[0]())
        self.assertFalse(self.hub.persisted_checkpoints)
        self.assertFalse(self.hub.pending)

    def test_failed_final_commit_preserves_exact_retry(self):
        first = self.new_pulse();self.hub.acknowledge(first, now=1001)
        previous = copy.deepcopy(self.hub.persisted_checkpoints)
        self.hub._updates(self.hub.detector.reset(PATH, 1002, 'isolated'))
        final = self.hub.batch(now=1002)
        # A failing writer does not call acknowledge.
        self.assertEqual(self.hub.batch(now=1002.1), final)
        self.assertEqual(self.hub.persisted_checkpoints, previous)
        self.hub.acknowledge(final, now=1002.2)
        self.assertFalse(self.hub.persisted_checkpoints)

    def test_old_ack_cannot_remove_unsaved_final_version(self):
        first = self.new_pulse()
        self.observe(1.2, 1002)
        self.hub._updates(self.hub.detector.reset(PATH, 1003))
        final = self.hub.batch(now=1003)
        self.hub.acknowledge(first, now=1003)
        self.assertEqual(self.hub.batch(now=1003.1), final)
        self.assertEqual(final[0]['peak'], 1.2)
        self.hub.acknowledge(final, now=1003.2)
        self.assertFalse(self.hub.persisted_checkpoints)

    def test_replacement_pulse_keeps_its_own_heartbeat(self):
        first = self.new_pulse();self.hub.acknowledge(first, now=1001)
        self.hub._updates(self.hub.detector.reset(PATH, 1002))
        self.observe(0, 1003);self.observe(.7, 1004)
        final_and_new = self.hub.batch(now=1004)
        self.assertEqual(len(final_and_new), 2)
        self.hub.acknowledge(final_and_new, now=1004)
        active = self.hub.detector.tracks[(PATH, 'basis')].pulse['id']
        self.assertEqual(set(self.hub.persisted_checkpoints), {active})
        self.observe(.71, 1005)
        self.assertEqual(self.hub.batch(now=1005), [])
        self.assertEqual(self.hub.batch(now=1019)[0]['id'], active)

    def test_natural_settlement_removes_final_watermark(self):
        first = self.new_pulse();self.hub.acknowledge(first, now=1001)
        for at in range(1002, 1302):
            self.observe(.6, at)
            rows = self.hub.batch(now=at)
            self.hub.acknowledge(rows, now=at)
        self.assertIsNone(self.hub.detector.tracks[(PATH, 'basis')].pulse)
        self.assertFalse(self.hub.pending)
        self.assertFalse(self.hub.persisted_checkpoints)


if __name__ == '__main__':
    unittest.main()

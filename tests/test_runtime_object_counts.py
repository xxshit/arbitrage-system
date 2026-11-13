import threading
import unittest
import weakref
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

from runtime_object_counts import public_object_counts


def hub():
    return SimpleNamespace(lock=threading.Lock(), routes={}, books={}, marks={},
        workers={}, pending={}, persisted_checkpoints={}, live_quotes={}, noise={},
        detector=SimpleNamespace(tracks={}))


class PublicObjectCountsTests(unittest.TestCase):
    def test_known_public_counts_no_values_and_no_mutation(self):
        h = hub()
        path = ('secret-like-symbol',)
        h.detector.tracks[(path, 'basis')] = SimpleNamespace(history=deque([0, 0]), pulse={})
        h.detector.tracks[(path, 'spread')] = SimpleNamespace(history=deque([0]*3), pulse={'id':'not-for-export'})
        h.noise[path] = SimpleNamespace(samples=deque(range(4)))
        caches = {'url-not-for-export': {'payload': {'result': {'list': [1, 2]}}}}
        snapshots = {'spot': {'symbols':[{'rows':[{'private-like-marker':123}]}]}, 'ignored': object()}
        out = public_object_counts(h, caches, threading.Lock(), snapshots)
        self.assertEqual(out['stream']['history_buckets'], 5)
        self.assertEqual(out['stream']['packed_history_payload_bytes'], 2*32+3*64)
        self.assertEqual(out['stream']['active_pulses'], 1)
        self.assertEqual(out['stream']['noise_points'], 4)
        self.assertEqual(out['market_payloads']['public_rows'], 2)
        self.assertEqual(out['spot']['rows'], 1)
        self.assertTrue(out['stream']['complete'])
        self.assertNotIn('not-for-export', str(out))
        self.assertNotIn('marker', str(out))
        self.assertEqual(len(h.detector.tracks), 2)
        self.assertEqual(len(caches), 1)

    def test_busy_lock_never_waits(self):
        h = hub(); lock = threading.Lock()
        h.lock.acquire(); lock.acquire()
        try:
            out = public_object_counts(h, {}, lock, {})
            self.assertTrue(out['stream']['skipped_busy'])
            self.assertTrue(out['market_payloads']['skipped_busy_or_budget'])
        finally:
            lock.release(); h.lock.release()

    def test_budget_marks_partial_does_not_claim_zero(self):
        h = hub()
        h.detector.tracks[(('symbol',), 'spread')] = SimpleNamespace(history=deque([1]), pulse={})
        with patch('runtime_object_counts.time.monotonic', side_effect=[0]+[1]*30):
            out = public_object_counts(h, {}, threading.Lock(), {'spot':{'symbols':[{}]}})
        self.assertEqual(out['stream']['tracks'], 1)
        self.assertEqual(out['stream']['counted_tracks'], 0)
        self.assertFalse(out['stream']['complete'])
        self.assertTrue(out['spot']['skipped_budget'])
        self.assertTrue(h.lock.acquire(blocking=False))
        h.lock.release()

    def test_roots_released_without_gc(self):
        class Cache(dict): pass
        h = hub(); cache = Cache({'url':{'payload':[{}]}}); snapshots = Cache()
        refs = [weakref.ref(cache), weakref.ref(snapshots)]
        out = public_object_counts(h, cache, threading.Lock(), snapshots)
        del cache, snapshots
        self.assertTrue(all(ref() is None for ref in refs))
        self.assertEqual(out['market_payloads']['entries'], 1)

    def test_cache_count_cap_is_explicit(self):
        h = hub(); cache = {i:{'payload':[]} for i in range(130)}
        with patch('runtime_object_counts.time.monotonic', return_value=0):
            out = public_object_counts(h, cache, threading.Lock(), {})
        self.assertEqual(out['market_payloads']['entries'], 130)
        self.assertEqual(out['market_payloads']['counted_entries'], 128)
        self.assertFalse(out['market_payloads']['complete'])

    def test_known_owner_lengths_and_exception_isolation(self):
        owner = SimpleNamespace(lock=threading.Lock(), entries={'hidden':{}}, retries={})
        out = public_object_counts(hub(), {}, threading.Lock(), {}, owners={'trend_references':owner})
        self.assertEqual(out['trend_references'], dict(entries=1,retries=0))
        del owner.entries
        out = public_object_counts(hub(), {}, threading.Lock(), {}, owners={'trend_references':owner})
        self.assertEqual(out, dict(incomplete=True,error_type='AttributeError'))
        self.assertTrue(owner.lock.acquire(blocking=False))
        owner.lock.release()


if __name__ == '__main__': unittest.main()

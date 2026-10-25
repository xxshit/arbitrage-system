import unittest
from unittest.mock import patch

from scripts.public_decode_workload import PublicDecodeWorkload
import scripts.public_decode_workload as workload


class PublicDecodeWorkloadTests(unittest.TestCase):
    def test_varying_concurrent_reads_and_cache_have_exact_values(self):
        load = PublicDecodeWorkload(lanes=3, variants=8)
        for _ in range(4):
            self.assertGreaterEqual(load.step(), 0)
        self.assertEqual(load.verify(), dict(cycles=4, responses=12, lanes=3,
                         variants=8, exact_values=True, real_network=False))

    def test_inflight_value_damage_is_rejected(self):
        load = PublicDecodeWorkload(lanes=2, variants=2)
        original = workload.decode_json
        def broken(*args):
            value, length = original(*args)
            value["rate"] = "0.25005001"
            return value, length
        with patch.object(workload, "decode_json", broken), self.assertRaises(AssertionError):
            load.step()
        self.assertEqual(load.cycles, 0)

    def test_cached_value_damage_and_missing_cycle_are_rejected(self):
        load = PublicDecodeWorkload(lanes=1, variants=1)
        with self.assertRaises(AssertionError):
            load.verify()
        load.step()
        load.cache[0]["price"] = "124"
        with self.assertRaises(AssertionError):
            load.verify()

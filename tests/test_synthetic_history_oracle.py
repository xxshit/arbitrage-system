import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import weakref

from scripts.synthetic_history_oracle import PulseOracle, TurnoverCohort, canonical


class Payload(dict):
    pass


def payload(n):
    return Payload(reminder={"policy": 1, "notices": [{"at": n/4, "kind": "first"}]},
                   version=n, peak_at=123.25+n, evidence={"nested": ["exact", n, 1.23456789]})


class PulseOracleTests(unittest.TestCase):
    def test_full_values_revisions_and_sorted_digest_match_independent_map(self):
        with tempfile.TemporaryDirectory() as directory:
            oracle = PulseOracle(Path(directory)/"oracle.sqlite3")
            expected = {}
            for start in range(0, 1234, 64):
                batch = {f"id{n:05d}": payload(n) for n in range(start, min(start+64, 1234))}
                expected.update(batch)
                oracle.commit(batch)
            changed = {"id00000": payload(2000), "id01233": payload(2001)}
            oracle.commit(changed); expected.update(changed)
            self.assertEqual(len(oracle), 1234)
            revisions = oracle.previous(["id00000", "id01233", "absent"])
            self.assertEqual(revisions, {key: {field: value[field] for field in ("reminder","version","peak_at")}
                                        for key, value in changed.items()})
            hashes = {key: hashlib.sha256(canonical(value).encode()).hexdigest() for key, value in expected.items()}
            self.assertEqual([row for batch in oracle.batches(117) for row in batch], sorted(hashes.items()))
            self.assertEqual(oracle.digest(), hashlib.sha256(canonical(sorted(hashes.items())).encode()).hexdigest())
            self.assertEqual(oracle.verify()["integrity"], "ok")
            self.assertLessEqual(oracle.status()["max_batch_rows"], 64)
            with self.assertRaises(FileExistsError):
                PulseOracle(oracle.path)

    def test_failed_batch_rolls_back_and_inputs_are_not_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            oracle = PulseOracle(Path(directory)/"oracle.sqlite3")
            item = payload(1); ref = weakref.ref(item)
            oracle.commit({"initial": item}); del item
            self.assertIsNone(ref())  # No forced collection, finalizers or retained evidence.
            before = oracle.digest()
            with sqlite3.connect(oracle.path) as conn:
                conn.execute("CREATE TRIGGER refuse BEFORE INSERT ON expected WHEN NEW.id='broken' BEGIN SELECT RAISE(ABORT,'injected'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                oracle.commit({"new": payload(2), "broken": payload(3)})
            self.assertEqual(oracle.digest(), before)
            self.assertEqual(len(oracle), 1)
            self.assertEqual(oracle.previous(["new","broken"]), {})
            self.assertEqual(oracle.verify()["integrity"], "ok")


class TurnoverTests(unittest.TestCase):
    def test_exact_real_cutoff_inclusive_and_symbol_projection(self):
        cohort = TurnoverCohort(100003, 1793630000)
        for delta in (-100, -0.001, 0, 0.001, 1, 119, 120.5, 7200, 25000, 25000.1, 30000):
            start = cohort.first_epoch+delta
            now = start+86400
            for symbol in (None, 0, 1, 499):
                expected = sum(start <= cohort.epoch(n) <= now and
                               (symbol is None or n % 500 == symbol)
                               for n in range(cohort.total))
                self.assertEqual(cohort.count(start, now, symbol), expected)
        self.assertEqual(cohort.count(cohort.first_epoch, cohort.first_epoch), 4)
        self.assertEqual(cohort.count(cohort.first_epoch+0.001, cohort.first_epoch+0.999), 0)
        self.assertEqual(cohort.count(cohort.first_epoch+25000, cohort.first_epoch+25000), 3)

    def test_old_seeds_leave_projection_without_rewriting_evidence(self):
        cohort = TurnoverCohort(100000, 1793630000)
        epochs = [cohort.epoch(n) for n in range(cohort.total)]
        at_start = cohort.count(cohort.first_epoch, cohort.first_epoch+86400)
        after_two_hours = cohort.count(cohort.first_epoch+7200, cohort.first_epoch+86400+7200)
        self.assertEqual(at_start-after_two_hours, 28800)
        self.assertEqual(epochs, [cohort.epoch(n) for n in range(cohort.total)])


if __name__ == "__main__":
    unittest.main()

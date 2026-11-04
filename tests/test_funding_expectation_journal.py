import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from threading import Lock

from scripts.funding_alert_workload import FundingAlertWorkload, FundingEventJournal


class FullFundingExpectations(unittest.TestCase):
    def test_concurrent_readers_keep_all_fields_and_reject_wrong_values_and_tie_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "expected-funding.sqlite3"
            writer = FundingEventJournal(path, create=True)
            read = FundingEventJournal(path)
            rows = {key: dict(id=key, contract="S", observed_at_ms=stamp,
                              evidence_json=json.dumps(dict(id=key, rate=0.125, notice=True)))
                    for key, stamp in (("b", 2), ("a", 2), ("c", 1))}
            writer.update(rows)
            oracle = SimpleNamespace(expected_events=read, contracts=["S"], latest={"S": {"contract": "S"}},
                                     http_lock=Lock(), http_checks=0, active_http_checks=0)
            history = [json.loads(rows[key]["evidence_json"]) for key in ("a", "b", "c")]
            def check(items, active="a"):
                FundingAlertWorkload.verify_http(oracle, {"groups": [{"contract": "S", "history": items}],
                    "active_events": [{"id": active, "contract": "S", "notice": True}]})
            with ThreadPoolExecutor(2) as pool:
                list(pool.map(check, (history, history)))
            self.assertEqual(oracle.http_checks, 2)
            for bad in (history[::-1], history[:-1], history + history[:1],
                        [dict(history[0], rate=0.126), *history[1:]]):
                with self.assertRaises(AssertionError):
                    check(bad)
            with self.assertRaises(AssertionError):
                check(history, active="missing")
            with self.assertRaisesRegex(ValueError, "read-only"):
                read.update(rows)
            with self.assertRaises(FileExistsError):
                FundingEventJournal(path, create=True)
            self.assertEqual(read.contracts(), {"S"})
            self.assertEqual(len(read), 3)
            read.verify_storage()

import copy
import json
import unittest
from unittest.mock import patch

from sqlalchemy import MetaData, create_engine, update
from sqlalchemy.orm import Session

from scripts.snapshot_write_workload import SnapshotSqlCoverage, SnapshotWriteWorkload, synthetic_tables
from snapshot_persistence import json_delta, json_equal


class NativeBranchCoverageTests(unittest.TestCase):
    def test_real_sqlite_execution_cannot_pass_native_mysql_gate(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        metadata = MetaData()
        quotes, equity = synthetic_tables(metadata)
        metadata.create_all(engine)
        coverage = SnapshotSqlCoverage(engine)
        self.addCleanup(coverage.close)
        plans = {"sets": 0, "removes": 0, "full": 0, "heartbeat": 0}
        with Session(engine) as session:
            workload = SnapshotWriteWorkload(session, quotes, equity)
            session.commit()
            self.assertGreater(len(json.dumps(workload.payload)), 160000)
            for _ in range(24):
                previous = copy.deepcopy(workload.payload)
                workload.step(session)
                session.commit()
                if json_equal(previous, workload.payload):
                    plans["heartbeat"] += 1
                else:
                    delta = json_delta(previous, workload.payload)
                    if delta is None:
                        plans["full"] += 1
                    else:
                        plans["sets"] += bool(delta[0])
                        plans["removes"] += bool(delta[1])
            self.assertTrue(workload.verify(session)["exact_values"])
        self.assertTrue(all(plans.values()), plans)
        result = coverage.verify(24)
        self.assertFalse(result["native_mysql"])
        self.assertEqual(result["executed"], dict(updates=24, json_set=0, json_remove=0, full_value=22, heartbeat=2))
        with self.assertRaisesRegex(AssertionError, "native MySQL execution required"):
            coverage.verify(24, require_native=True)
        with self.assertRaisesRegex(AssertionError, "execution count differs"):
            coverage.verify(23)


class FundingWorkloadTests(unittest.TestCase):
    def setUp(self):
        # This fixture import sets an in-memory synthetic URL before app import.
        # No production settings or background application threads are loaded.
        from tests.test_alert_history import module
        from scripts.funding_alert_workload import FundingAlertWorkload
        self.module = module
        self.context = module.app.app_context()
        self.context.push()
        module.app.config.update(TESTING=True)
        self.patches = [patch.object(module, "FUNDING_ALERT_LIVE_STATES", {}),
                        patch.object(module, "MARKET_PAYLOAD_CACHE", {}),
                        patch.object(module, "is_rwa_stock_pair", return_value=False),
                        patch.object(module, "BACKGROUND_WORKERS_STARTED", True)]
        for item in self.patches:
            item.start()
        module.db.drop_all()
        module.db.create_all()
        self.load = FundingAlertWorkload(module)

    def tearDown(self):
        self.module.db.session.remove()
        self.module.db.drop_all()
        for item in reversed(self.patches):
            item.stop()
        self.context.pop()

    def test_real_writer_and_authenticated_http_keep_nonempty_full_history(self):
        module, load = self.module, self.load
        self.assertEqual(load.step(0), 0)
        self.assertEqual(load.step(12), 32)
        owner = module.UserAccount(username="synthetic-funding", password_hash="unused", role="admin", account_level="lv3", active_session_hash=module.session_digest("synthetic-funding-token"))
        module.db.session.add(owner)
        module.db.session.commit()
        user_id = owner.id
        module.db.session.remove()
        client = module.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=user_id, auth_token="synthetic-funding-token")
        for view in ("summary", "active"):
            response = client.get("/api/alerts", query_string=dict(view=view))
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()["funding"]
            load.verify_http(payload)
            self.assertEqual(len(payload["groups"]), 32)
            self.assertEqual(len(payload["active_events"]), 16)
        result = load.verify()
        self.assertEqual((result["state_rows"], result["event_rows"], result["http_checks"]), (524, 32, 2))

    def test_private_disk_expectations_detect_durable_event_corruption_after_full_http_check(self):
        import tempfile
        from pathlib import Path
        from scripts.funding_alert_workload import FundingAlertWorkload
        with tempfile.TemporaryDirectory() as directory:
            module = self.module
            load = FundingAlertWorkload(module, events_path=Path(directory).resolve() / "expected-funding.sqlite3")
            self.assertEqual(load.step(0), 0)
            with patch.object(module.db.session, "commit", side_effect=RuntimeError("synthetic failure")):
                with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                    load.step(12)
            module.db.session.rollback()
            self.assertEqual(len(load.expected_events), 0)
            self.assertEqual(load.step(12), 32)
            load.verify_http(module.funding_alert_payload())
            self.assertEqual(load.verify()["event_rows"], 32)
            table = module.FundingAlertEvent.__table__
            key = module.db.session.query(module.FundingAlertEvent.id).first()[0]
            module.db.session.execute(update(table).where(table.c.id == key).values(evidence_json="{}"))
            with self.assertRaisesRegex(AssertionError, "durable funding events differ"):
                load.verify()
            module.db.session.rollback()
            self.assertTrue(load.verify()["exact_values"])

    def test_clock_capture_scheduling_delay_preserves_exact_source_cache_minimum(self):
        import time
        module, load = self.module, self.load

        class ScheduledClock:
            delayed = False

            def __getattr__(self, name):
                return getattr(time, name)

            def monotonic(self):
                if not self.delayed:
                    self.delayed = True
                    time.sleep(.05)
                return time.monotonic()

        with patch.object(module, "time", ScheduledClock()):
            load.step(0)
        live = module.FUNDING_ALERT_LIVE_STATES[load.contracts[0]]
        source = module.MARKET_PAYLOAD_CACHE["https://fapi.binance.com/fapi/v1/premiumIndex"]["payload"][0]["time"] / 1000
        self.assertGreater(source - live["rate_at"], .01)
        self.assertEqual(live["rate_at"], min(source, live["info_at"]))
        self.assertGreaterEqual(load.max_clock_capture_seconds, .05)
        self.assertEqual(load.timestamp_checks, 524)
        load.verify_http(module.funding_alert_payload())
        self.assertTrue(load.verify()["exact_values"])

    def test_clock_contract_rejects_invented_cache_capture_time(self):
        module, load = self.module, self.load
        original = module.binance_observations

        def bad_time(instruments, rates, info, rate_at, info_at, observed_at):
            # Keep a plausible, fresh timestamp but change the recorded result.
            rows = original(instruments, rates, info, rate_at, info_at, observed_at)
            for row in rows:
                row["info_at"] -= 1
                row["rate_at"] -= 1
            return rows

        with patch.object(module, "binance_observations", bad_time):
            with self.assertRaises(AssertionError):
                load.step(0)
        self.assertEqual(load.cycles, 0)
        self.assertEqual(load.expected_states, {})

    def test_clock_contract_rejects_modified_live_info_timestamp(self):
        module, load = self.module, self.load
        original = module.refresh_funding_alerts

        def bad_time():
            result = original()
            module.FUNDING_ALERT_LIVE_STATES[load.contracts[0]]["info_at"] += 1
            return result

        with patch.object(module, "refresh_funding_alerts", bad_time):
            with self.assertRaises(AssertionError):
                load.step(0)
        self.assertEqual(load.timestamp_checks, 0)
        self.assertEqual(load.cycles, 0)

    def test_clock_contract_rejects_modified_live_observation_time(self):
        module, load = self.module, self.load
        original = module.refresh_funding_alerts

        def bad_time():
            result = original()
            module.FUNDING_ALERT_LIVE_STATES[load.contracts[0]]["observed_at"] += 1
            return result

        with patch.object(module, "refresh_funding_alerts", bad_time):
            with self.assertRaises(AssertionError):
                load.step(0)
        self.assertEqual(load.cycles, 0)

    def test_core_reread_detects_corruption_even_with_live_cache_populated(self):
        module, load = self.module, self.load
        load.step(0)
        load.verify_http(module.funding_alert_payload())
        self.assertTrue(load.verify()["exact_values"])
        table = module.FundingAlertState.__table__
        module.db.session.execute(update(table).where(table.c.contract == load.contracts[0]).values(state_json="{}"))
        with self.assertRaisesRegex(AssertionError, "durable funding states differ"):
            load.verify()
        module.db.session.rollback()
        self.assertTrue(load.verify()["exact_values"])

    def test_failed_commit_keeps_intent_journal_and_event_count_unchanged(self):
        module, load = self.module, self.load
        load.step(0)
        before = copy.deepcopy(load.expected_states)
        with patch.object(module.db.session, "commit", side_effect=RuntimeError("synthetic commit failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic commit failure"):
                load.step(12)
        module.db.session.rollback()
        self.assertEqual(load.expected_states, before)
        self.assertEqual(load.expected_events, {})
        self.assertEqual(load.cycles, 1)
        self.assertEqual(module.FundingAlertEvent.query.count(), 0)
        self.assertEqual(load.step(12), 32)
        load.verify_http(module.funding_alert_payload())
        self.assertEqual(load.verify()["event_rows"], 32)

    def test_empty_or_corrupted_http_cannot_pass(self):
        module, load = self.module, self.load
        load.step(0)
        with self.assertRaisesRegex(AssertionError, "empty or lost"):
            load.verify_http(dict(groups=[], active_events=[]))
        payload = module.funding_alert_payload()
        payload["groups"][0]["rate"] = 999
        with self.assertRaises(AssertionError):
            load.verify_http(payload)
        self.assertEqual(load.http_checks, 0)


if __name__ == "__main__":
    unittest.main()

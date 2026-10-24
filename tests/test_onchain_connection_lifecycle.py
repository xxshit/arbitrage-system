import os
import threading
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ["DATABASE_URL"] = "sqlite://"
import app as module
from tests.test_onchain_opportunities import sample_payload


class OnchainPublicationTests(unittest.TestCase):
    def setUp(self):
        self.context = module.app.app_context()
        self.context.push()
        module.db.drop_all()
        module.db.create_all()
        self.lock = threading.Lock()
        self.patch_lock = patch.object(module, "ONCHAIN_OPPORTUNITY_SCAN_LOCK", self.lock)
        self.patch_lock.start()

    def tearDown(self):
        self.patch_lock.stop()
        module.db.session.remove()
        module.db.drop_all()
        self.context.pop()

    def seed(self, network, captured_at):
        rows = module.parse_onchain_trending_pools(sample_payload(symbol="PREVIOUS"), network, captured_at)
        module.db.session.add_all(module.OnchainOpportunitySnapshot(**row) for row in rows)
        module.db.session.commit()

    def test_no_checked_out_connection_during_all_network_calls(self):
        holders = []
        phases = []

        def acquire():
            phases.append("acquire")
            connection = module.db.engine.connect()
            return connection, True

        def fetch(*args, **kwargs):
            phases.append("network")
            holders.append(module.BACKGROUND_RUNTIME.snapshot()["db_checked_out"])
            return sample_payload()

        with patch.object(module, "acquire_onchain_refresh_lock", side_effect=acquire), \
             patch.object(module, "release_onchain_refresh_lock", side_effect=lambda conn: conn.close() if conn else None), \
             patch.object(module, "get_onchain_json", side_effect=fetch):
            result = module.refresh_onchain_opportunities()
        self.assertEqual(holders, [0] * len(module.ONCHAIN_NETWORKS))
        self.assertEqual(phases, ["network"] * len(module.ONCHAIN_NETWORKS) + ["acquire"])
        self.assertEqual(result["updated_networks"], sorted(module.ONCHAIN_NETWORKS))
        self.assertEqual(module.BACKGROUND_RUNTIME.snapshot()["db_checked_out"], 0)
        self.assertFalse(self.lock.locked())

    def test_late_fetch_does_not_overwrite_newer_network_snapshot(self):
        started = datetime(2026, 10, 24, 12)
        self.seed("bsc", started + timedelta(seconds=5))
        self.seed("base", started - timedelta(seconds=5))
        with patch.object(module, "utc_now_naive", return_value=started), \
             patch.object(module, "get_onchain_json", return_value=sample_payload()):
            result = module.refresh_onchain_opportunities()
        self.assertNotIn("bsc", result["updated_networks"])
        self.assertIn("base", result["updated_networks"])
        row = module.OnchainOpportunitySnapshot.query.filter_by(network="bsc").one()
        self.assertEqual(row.token_symbol, "PREVIOUS")
        self.assertEqual(row.captured_at, started + timedelta(seconds=5))
        row = module.OnchainOpportunitySnapshot.query.filter_by(network="base").one()
        self.assertEqual(row.captured_at, started)

    def test_failed_network_retains_previous_snapshot(self):
        self.seed("bsc", datetime(2026, 10, 24))
        def fetch(url, **kwargs):
            if "/bsc/" in url:
                raise TimeoutError("synthetic timeout")
            return sample_payload()
        with patch.object(module, "get_onchain_json", side_effect=fetch):
            result = module.refresh_onchain_opportunities()
        self.assertIn("bsc", result["errors"])
        self.assertNotIn("bsc", result["updated_networks"])
        self.assertEqual(module.OnchainOpportunitySnapshot.query.filter_by(network="bsc").one().token_symbol, "PREVIOUS")

    def test_all_network_failures_never_acquire_database_lock(self):
        with patch.object(module, "get_onchain_json", side_effect=TimeoutError("synthetic timeout")), \
             patch.object(module, "acquire_onchain_refresh_lock", return_value=(None, True)) as acquire:
            with self.assertRaises(RuntimeError):
                module.refresh_onchain_opportunities()
        acquire.assert_not_called()
        self.assertFalse(self.lock.locked())

    def test_publication_failure_rolls_back_and_unlocks(self):
        self.seed("bsc", datetime(2026, 10, 24))
        with patch.object(module, "get_onchain_json", return_value=sample_payload()), \
             patch.object(module.ORMSession, "commit", side_effect=RuntimeError("synthetic write failure")):
            # SQLite StaticPool uses the scoped session, preserving existing
            # unit-test transaction semantics; production binds the named lock.
            with self.assertRaises(RuntimeError):
                module.refresh_onchain_opportunities()
        module.db.session.rollback()
        self.assertEqual(module.OnchainOpportunitySnapshot.query.filter_by(network="bsc").one().token_symbol, "PREVIOUS")
        self.assertFalse(self.lock.locked())

    def test_release_failure_still_releases_local_mutex(self):
        with patch.object(module, "get_onchain_json", return_value=sample_payload()), \
             patch.object(module, "release_onchain_refresh_lock", side_effect=RuntimeError("synthetic release failure")):
            with self.assertRaises(RuntimeError):
                module.refresh_onchain_opportunities()
        self.assertFalse(self.lock.locked())

    def test_busy_publication_lock_keeps_original_snapshot(self):
        self.seed("bsc", datetime(2026, 10, 24))
        connection = MagicMock()
        with patch.object(module, "get_onchain_json", return_value=sample_payload()), \
             patch.object(module, "acquire_onchain_refresh_lock", return_value=(connection, False)):
            result = module.refresh_onchain_opportunities()
        self.assertTrue(result["skipped"])
        self.assertEqual(module.OnchainOpportunitySnapshot.query.one().token_symbol, "PREVIOUS")
        connection.close.assert_called_once()
        self.assertFalse(self.lock.locked())

    def test_synthetic_maintenance_workload_checks_real_paths_and_rejects_corruption(self):
        from scripts.maintenance_workload import MaintenanceWorkload
        workload = MaintenanceWorkload(module)
        for _ in range(5):
            workload.step()
        checked = workload.verify()
        self.assertEqual(checked["cycles"], 5)
        self.assertEqual(checked["network_calls"], 20)
        self.assertEqual(checked["listing_events"], 2)
        row = module.OnchainOpportunitySnapshot.query.first()
        row.price_usd += .25
        module.db.session.flush()
        with self.assertRaisesRegex(AssertionError, "onchain publication values differ"):
            workload.verify()
        module.db.session.rollback()
        self.assertEqual(workload.verify(), checked)


class OnchainNamedLockTests(unittest.TestCase):
    def database(self, connection):
        return SimpleNamespace(engine=SimpleNamespace(dialect=SimpleNamespace(name="mysql"), connect=lambda: connection))

    def test_acquire_failure_discards_connection(self):
        connection = MagicMock()
        connection.execute.side_effect = RuntimeError("synthetic acquire failure")
        with patch.object(module, "db", self.database(connection)):
            with self.assertRaises(RuntimeError):
                module.acquire_onchain_refresh_lock()
        connection.invalidate.assert_called_once()
        connection.close.assert_called_once()

    def test_acquire_ends_read_transaction_before_publication(self):
        connection = MagicMock()
        connection.execute.return_value.scalar.return_value = 1
        with patch.object(module, "db", self.database(connection)):
            self.assertEqual(module.acquire_onchain_refresh_lock(), (connection, True))
        connection.commit.assert_called_once()

    def test_release_failure_discards_physical_named_lock_connection(self):
        connection = MagicMock()
        connection.execute.side_effect = RuntimeError("synthetic release failure")
        with self.assertRaises(RuntimeError):
            module.release_onchain_refresh_lock(connection)
        connection.invalidate.assert_called_once()
        connection.close.assert_called_once()

    def test_public_https_uses_verified_shared_context_and_closes_response(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data": []}'
        with patch.object(module, "urlopen", return_value=response) as request:
            self.assertEqual(module.get_onchain_json("https://example.test/public", timeout=3), {"data": []})
        self.assertIs(request.call_args.kwargs["context"], module.MARKET_HTTP.context)
        self.assertTrue(module.MARKET_HTTP.context.check_hostname)
        response.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()

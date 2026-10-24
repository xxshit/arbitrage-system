import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
import app as module
from alert_read_payload import PackedPulse, PulseSpool
from read_performance import install_read_indexes


class PublicReadTests(unittest.TestCase):
    def setUp(self):
        self.context = module.app.app_context()
        self.context.push()
        module.db.drop_all()
        module.db.create_all()

    def tearDown(self):
        module.db.session.remove()
        module.db.drop_all()
        self.context.pop()

    def test_latest_episode_and_counts_do_not_load_old_history(self):
        now = datetime.now()
        for i in range(100):
            module.db.session.add(module.BasisTracking(symbol="DEMO/USDT", strategy="spot_futures", direction="positive", started_at=now-timedelta(hours=i), opening_basis=1+i, max_basis=2+i, max_abs_basis=2+i, max_at=now, resolved_at=now))
        module.db.session.commit()
        row = module.BasisTracking.query.order_by(module.BasisTracking.started_at.desc()).first()
        module.db.session.add_all([module.BasisExpansionLog(tracking_id=row.id, level=1, observed_basis=1, created_at=now) for _ in range(70)])
        module.db.session.commit()
        groups = [{"symbol": "DEMO/USDT"}, {"symbol": "MISSING/USDT"}]
        module.enrich_basis_openings(groups, "spot_futures")
        self.assertEqual(groups[0]["basis_opening"]["opened_basis"], 1)
        self.assertEqual(groups[0]["basis_opening"]["open_count"], 70)
        self.assertIsNone(groups[1]["basis_opening"])

    def test_unchanged_wallet_rows_use_bounded_heartbeat_statements(self):
        data = {f"COIN{i}/USDT": [{"name": "ETH", "deposit_open": True, "withdraw_open": True}] for i in range(1001)}
        module.store_transfer_networks("Gate", data)
        statements = []
        def record(conn, cursor, statement, parameters, context, many):
            statements.append(statement)
        module.sa_event.listen(module.db.engine, "before_cursor_execute", record)
        try:
            module.store_transfer_networks("Gate", data)
        finally:
            module.sa_event.remove(module.db.engine, "before_cursor_execute", record)
        updates = [s for s in statements if s.startswith("UPDATE")]
        self.assertEqual(len(updates), 5)
        self.assertTrue(all("chains_json" not in s for s in updates))
        self.assertEqual(module.TransferNetworkSnapshot.query.count(), 1001)

    def test_indexes_are_additive_idempotent_and_public_only(self):
        before = {table.name for table in module.db.metadata.tables.values()}
        with module.db.engine.begin() as conn:
            self.assertEqual(install_read_indexes(conn, module.db.metadata), [])
        self.assertEqual(before, set(module.db.metadata.tables))
        self.assertTrue(all(index.table.name in {"basis_tracking", "basis_expansion_log", "alert_pulse", "transfer_network_snapshot"} for index in module.READ_PERFORMANCE_INDEXES))

    def test_packed_projection_is_lossless_and_does_not_hold_full_payload(self):
        payload = {"id": "x", "metric": "spread", "quality": {"evidence": "public-quote" * 10000}, "peak": 1.23456789}
        packed = PackedPulse(payload, PulseSpool())
        self.assertEqual(packed.unpack(), payload)
        self.assertEqual(packed["peak"], payload["peak"])
        self.assertFalse(hasattr(packed, "__dict__"))
        self.assertNotIn("quality", packed.__slots__)


if __name__ == "__main__":
    unittest.main()

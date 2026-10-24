import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
import app as module


def catalog(*symbols, status="TRADING"):
    return [dict(symbol=symbol, status=status, contractType="PERPETUAL") for symbol in symbols]


class ListingSyncTransactionTests(unittest.TestCase):
    def setUp(self):
        self.context = module.app.app_context()
        self.context.push()
        module.db.drop_all()
        module.db.create_all()

    def tearDown(self):
        module.db.session.remove()
        module.db.drop_all()
        self.context.pop()

    def test_unchanged_catalog_uses_bounded_heartbeat_and_no_new_events(self):
        items = catalog(*(f"COIN{i}USDT" for i in range(1001)))
        first = datetime(2026, 10, 24, 12)
        second = first + timedelta(minutes=30)
        with patch.object(module, "datetime") as clock:
            clock.now.return_value = first
            module.sync_binance_listing_events(items)
        statements = []
        def record(conn, cursor, statement, parameters, context, many):
            statements.append(statement)
        module.sa_event.listen(module.db.engine, "before_cursor_execute", record)
        try:
            with patch.object(module, "datetime") as clock:
                clock.now.return_value = second
                module.sync_binance_listing_events(items)
        finally:
            module.sa_event.remove(module.db.engine, "before_cursor_execute", record)
        updates = [sql for sql in statements if sql.startswith("UPDATE")]
        self.assertEqual(len(updates), 5)
        self.assertTrue(all("SET last_seen_at=" in sql for sql in updates))
        self.assertEqual(module.ListingEvent.query.count(), 0)
        self.assertEqual(module.ListingState.query.count(), 1001)
        self.assertTrue(all(row.first_seen_at == first and row.last_seen_at == second and row.active
                            for row in module.ListingState.query.all()))

    def test_add_remove_status_reactivation_and_other_exchange_semantics(self):
        module.sync_binance_listing_events(catalog("KEEPUSDT", "REMOVEUSDT", "STATUSUSDT"))
        module.db.session.add(module.ListingState(exchange="Gate", symbol="GATEUSDT", status="TRADING", active=True))
        module.db.session.commit()
        module.sync_binance_listing_events(catalog("KEEPUSDT", "NEWUSDT") + catalog("STATUSUSDT", status="BREAK"))
        events = sorted((row.symbol, row.event_type) for row in module.ListingEvent.query.all())
        self.assertEqual(events, [("NEWUSDT", "上架"), ("REMOVEUSDT", "下架")])
        self.assertFalse(module.ListingState.query.filter_by(symbol="REMOVEUSDT").one().active)
        self.assertEqual(module.ListingState.query.filter_by(symbol="STATUSUSDT").one().status, "BREAK")
        self.assertTrue(module.ListingState.query.filter_by(symbol="GATEUSDT").one().active)
        module.sync_binance_listing_events(catalog("KEEPUSDT", "NEWUSDT", "REMOVEUSDT", "STATUSUSDT"))
        self.assertTrue(module.ListingState.query.filter_by(symbol="REMOVEUSDT").one().active)
        self.assertEqual(module.ListingEvent.query.count(), 2)

    def test_empty_or_irrelevant_payload_does_not_delist_anything(self):
        module.sync_binance_listing_events(catalog("KEEPUSDT"))
        module.sync_binance_listing_events([])
        module.sync_binance_listing_events([dict(symbol="EQUITYUSDT", contractType="TRADIFI_PERPETUAL")])
        self.assertTrue(module.ListingState.query.one().active)
        self.assertEqual(module.ListingEvent.query.count(), 0)

    def test_commit_failure_does_not_publish_partial_state_or_events(self):
        module.sync_binance_listing_events(catalog("OLDUSDT"))
        with patch.object(module.ORMSession, "commit", side_effect=RuntimeError("synthetic commit failure")):
            with self.assertRaises(RuntimeError):
                module.sync_binance_listing_events(catalog("NEWUSDT"))
        module.db.session.rollback()
        self.assertEqual([(row.symbol, row.active) for row in module.ListingState.query.all()], [("OLDUSDT", True)])
        self.assertEqual(module.ListingEvent.query.count(), 0)


if __name__ == "__main__":
    unittest.main()

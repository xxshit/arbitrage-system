import copy
from datetime import timedelta
import unittest
from unittest.mock import patch

from tests import test_alert_history as fixture

module = fixture.module


class SnapshotIntegrationTests(unittest.TestCase):
    setUp = fixture.AlertHistoryTests.setUp
    tearDown = fixture.AlertHistoryTests.tearDown

    def groups(self, **values):
        row = dict(long_exchange="Gate", short_exchange="Binance 合约", long_ask=100., long_bid=99., short_bid=101., short_ask=102.,
                   basis=.1, funding_rate=.005, funding_interval_hours=4, next_funding_time="10-21 08:00", open_spread=1., close_spread=3.03)
        return [dict(symbol="DEMO/USDT", rows=[{**row, **values}])]

    def test_both_strategies_keep_real_values_ids_heartbeat_and_cold_start(self):
        for model, save, load, cache in (
                (module.LatestMarketSnapshot, module.save_latest_market_snapshot, module.load_latest_market_snapshot, "SPOT_FUTURES_CACHE"),
                (module.LatestDualFuturesSnapshot, module.save_latest_dual_futures_snapshot, module.load_latest_dual_futures_snapshot, "DUAL_FUTURES_CACHE")):
            with module.app.app_context(), patch.object(module, cache, {"snapshot": None}):
                save(self.groups())
                identity = model.query.one().id
                updated = self.groups(open_spread=-.8, long_ask=103, futures_open_interest=0, long_funding_rate=None)
                save(updated)
                item = model.query.one()
                self.assertEqual(item.id, identity)
                self.assertEqual((item.open_spread, item.long_ask, item.captured_at), (-.8, 103, self.now))
                self.assertEqual(load()["symbols"][0]["rows"][0]["open_spread"], -.8)
                self.assertEqual(module.FuturesPriceHistory.query.count(), 0)
                self.assertEqual(module.AlertEvent.query.count(), 0)
                self.assertEqual(module.HedgePosition.query.count(), 0)

    def test_unchanged_quote_heartbeat_advances_without_refreshing_missing_spot(self):
        groups = self.groups()
        groups.append(dict(symbol="MISSING/USDT", rows=copy.deepcopy(groups[0]["rows"])))
        with module.app.app_context():
            module.save_latest_market_snapshot(groups)
            at = self.now + timedelta(seconds=15)
            with patch.object(module, "datetime") as clock:
                clock.now.return_value = at
                module.save_latest_market_snapshot(self.groups())
            self.assertEqual(module.LatestMarketSnapshot.query.filter_by(symbol="DEMO/USDT").one().captured_at, at)
            self.assertEqual(module.LatestMarketSnapshot.query.filter_by(symbol="MISSING/USDT").one().captured_at, self.now)

    def test_duplicate_paths_coalesce_to_last_observation_before_inserting(self):
        for save, model in ((module.save_latest_market_snapshot, module.LatestMarketSnapshot),
                            (module.save_latest_dual_futures_snapshot, module.LatestDualFuturesSnapshot)):
            groups = self.groups()
            groups[0]["rows"].append({**groups[0]["rows"][0], "open_spread": .9})
            with module.app.app_context():
                save(groups)
                self.assertEqual(model.query.count(), 1)
                self.assertEqual(model.query.one().open_spread, .9)

    def test_transaction_failure_rolls_back_batch_and_retry_saves_new_value(self):
        with module.app.app_context():
            module.save_latest_market_snapshot(self.groups())
            with patch.object(module.db.session, "commit", side_effect=RuntimeError("synthetic failure")):
                with self.assertRaises(RuntimeError):
                    module.save_latest_market_snapshot(self.groups(open_spread=2))
            module.db.session.rollback()
            self.assertEqual(module.LatestMarketSnapshot.query.one().open_spread, 1)
            module.save_latest_market_snapshot(self.groups(open_spread=2))
            self.assertEqual(module.LatestMarketSnapshot.query.one().open_spread, 2)

    def test_equity_failed_commit_keeps_persistence_retry_due_and_no_schema_change(self):
        with module.app.app_context(), patch.object(module, "EQUITY_SPOT_CACHE", dict(snapshot=None, saved_at=0)), \
                patch.object(module, "build_equity_spot_snapshot", return_value={"symbols": [], "value": 1}):
            module.refresh_equity_spot_snapshot({}, {}, {})
            first = module.db.session.get(module.EquitySpotSnapshot, 1).payload
            module.EQUITY_SPOT_CACHE["saved_at"] = 0
            with patch.object(module, "build_equity_spot_snapshot", return_value={"symbols": [], "value": 2}), \
                    patch.object(module.db.session, "commit", side_effect=RuntimeError("synthetic failure")):
                with self.assertRaises(RuntimeError):
                    module.refresh_equity_spot_snapshot({}, {}, {})
            module.db.session.rollback()
            self.assertEqual(module.db.session.get(module.EquitySpotSnapshot, 1).payload, first)
            self.assertEqual(module.EQUITY_SPOT_CACHE["saved_at"], 0)

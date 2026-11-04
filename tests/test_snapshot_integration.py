import copy
from datetime import timedelta
import unittest
from unittest.mock import patch

from sqlalchemy import event

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

    def test_stale_dual_id_is_not_reused_by_a_new_path_in_the_same_save(self):
        model = module.LatestDualFuturesSnapshot
        with module.app.app_context():
            def save(symbol):
                groups = self.groups()
                groups[0]["symbol"] = symbol
                module.save_latest_dual_futures_snapshot(groups)
                return model.query.one().id

            original = save("OLD/USDT")
            replacement = save("NEW/USDT")
            self.assertGreater(replacement, original)
            self.assertGreater(save("OLD/USDT"), replacement)
            module.save_latest_dual_futures_snapshot([])
            self.assertEqual(model.query.count(), 0)

    def test_latest_quote_heartbeats_read_scalars_without_hydrating_snapshot_models(self):
        with module.app.app_context():
            for save, model in ((module.save_latest_market_snapshot, module.LatestMarketSnapshot),
                                (module.save_latest_dual_futures_snapshot, module.LatestDualFuturesSnapshot)):
                groups = [dict(symbol=f"S{i}/USDT", rows=self.groups()[0]["rows"]) for i in range(260)]
                save(groups)
                module.db.session.remove()
                def reject_load(target, context):
                    self.fail("a quote heartbeat loaded a full model")
                event.listen(model, "load", reject_load)
                try:
                    save(groups)
                finally:
                    event.remove(model, "load", reject_load)
                self.assertEqual(model.query.count(), 260)

    def test_backfill_keeps_first_prices_and_reads_each_missing_market_once(self):
        model = module.FuturesPriceHistory
        calls = []

        def fetch(symbol):
            calls.append(symbol)
            return symbol, [(100, 111.), (400, 112.), (400, 999.)]

        groups = [{"symbol": symbol} for symbol in ("ONE/USDT", "ONE/USDT", "TWO/USDT")]
        with module.app.app_context(), patch.object(module, "fetch_price_history_from_binance", fetch):
            prior = model(symbol="ONE/USDT", bucket_at=100, price=88.)
            module.db.session.add(prior)
            module.db.session.commit()
            prior_id = prior.id
            module.backfill_price_history(groups)
            module.backfill_price_history(groups)
            self.assertCountEqual(calls, ["ONE/USDT", "TWO/USDT"] * 2)
            self.assertEqual([(row.symbol, row.bucket_at, row.price) for row in
                              model.query.order_by(model.symbol, model.bucket_at)], [
                ("ONE/USDT", 100, 88.), ("ONE/USDT", 400, 112.),
                ("TWO/USDT", 100, 111.), ("TWO/USDT", 400, 112.),
            ])
            self.assertEqual(model.query.filter_by(symbol="ONE/USDT", bucket_at=100).one().id, prior_id)

    def test_backfill_keeps_a_bucket_inserted_after_its_read(self):
        model = module.FuturesPriceHistory
        # Stage the competing row after the actual SELECT, just before the INSERT.
        def stage_winner(conn, cursor, statement, parameters, context, executemany):
            if context.isinsert and context.compiled.statement.table.name == model.__tablename__ and not staged:
                staged.append(True)
                conn.execute(model.__table__.insert(), dict(symbol="RACE", bucket_at=100, price=42.))
        staged = []
        with module.app.app_context(), patch.object(module, "fetch_price_history_from_binance",
                                                     return_value=("RACE", [(100, 43.), (400, 44.)])):
            engine = module.db.engine
            event.listen(engine, "before_cursor_execute", stage_winner)
            try:
                module.backfill_price_history([{"symbol": "RACE"}])
            finally:
                event.remove(engine, "before_cursor_execute", stage_winner)
            self.assertEqual(staged, [True])
            self.assertEqual([(r.bucket_at, r.price) for r in model.query.order_by(model.bucket_at)],
                             [(100, 42.), (400, 44.)])

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

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from funding_history import DAY_MS, FundingHistoryClient, FundingHistoryError, FundingSyncSchedule, RequestGate, expected_settlement

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "funding-collection-test")
import app as module


class FundingCollectionUnitTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "checkpoint.json"
        self.now = 1_800_000_030.
        self.target = {"interval": 1, "candidates": ["DEMOUSDT"]}

    def client(self, get):
        client = FundingHistoryClient(get)
        for gate in client.gates.values():
            gate.spacing = 0
        return client

    def test_schedule_uses_each_interval_and_real_next_boundary_anchor(self):
        for interval in (1, 2, 4, 6, 8):
            target = {"interval": interval}
            boundary = (self.now // (interval*3600)) * interval*3600
            self.assertEqual(expected_settlement(target, self.now), boundary*1000)
            self.assertEqual(expected_settlement(target, boundary+1), (boundary-interval*3600)*1000)
        target = {"interval": 4, "next_at": self.now+1200}
        self.assertEqual(expected_settlement(target, self.now), (self.now+1200-14400)*1000)
        self.assertIsNone(expected_settlement({"interval": float('nan')}, self.now))

    def test_late_publication_is_retried_not_marked_as_complete(self):
        schedule = FundingSyncSchedule(self.path)
        targets = {"DEMO": self.target}
        old = expected_settlement(self.target, self.now) - 3600000
        self.assertTrue(schedule.plan(targets, {"DEMO": old}, self.now, 8))
        schedule.done("DEMO", self.target, self.now, old)
        self.assertEqual(schedule.plan(targets, {"DEMO": old}, self.now+10, 8), [])
        self.assertTrue(schedule.plan(targets, {"DEMO": old}, self.now+31, 8))
        new = expected_settlement(self.target, self.now)
        schedule.done("DEMO", self.target, self.now+31, new, full=True)
        self.assertEqual(schedule.plan(targets, {"DEMO": new}, self.now+40, 8), [])

    def test_new_settlement_and_interval_change_bypass_old_target_retry(self):
        schedule = FundingSyncSchedule(self.path)
        schedule.done("DEMO", self.target, self.now, 0, error="network")
        schedule.states["DEMO"]["retry"] = self.now + 10000
        self.assertTrue(schedule.plan({"DEMO": self.target}, {"DEMO": 0}, self.now+3600, 8))
        later = self.now + 3600
        old_interval = {"interval": 8}
        schedule.done("DEMO", old_interval, later, 0, error="network")
        self.assertNotEqual(expected_settlement(old_interval, later), expected_settlement(self.target, later))
        self.assertTrue(schedule.plan({"DEMO": self.target}, {"DEMO": 0}, later+1, 8))

    def test_retry_is_bounded_and_failure_never_marks_success_or_full_audit(self):
        schedule = FundingSyncSchedule(self.path)
        for _ in range(12):
            schedule.done("DEMO", self.target, self.now, 0, full=True, error="timeout")
        self.assertEqual(schedule.states["DEMO"]["retry"], self.now+300)
        self.assertNotIn("success", schedule.states["DEMO"])
        self.assertNotIn("full", schedule.states["DEMO"])

    def test_restart_fairness_and_reserved_full_history_repair(self):
        targets = {f"C{i:03}": self.target for i in range(140)}
        schedule = FundingSyncSchedule(self.path)
        seen = set()
        for index in range(3):
            now = self.now + index*10
            batch = schedule.plan(targets, {}, now, 64)
            self.assertLessEqual(len(batch), 65)
            self.assertTrue(any(full for _, full in batch))
            seen.update(key for key, _ in batch)
            for key, full in batch:
                schedule.done(key, self.target, now, expected_settlement(self.target, now), full=full)
            schedule.save({"at": now})
            schedule = FundingSyncSchedule(self.path)
        self.assertEqual(seen, set(targets))
        # Newest timestamps don't prove old interior gaps absent: audit every day.
        for state in schedule.states.values():
            state.update(success=self.now, full=self.now-86401, retry=0)
        batch = schedule.plan(targets, {key: expected_settlement(self.target, self.now) for key in targets}, self.now, 64)
        self.assertEqual(len(batch), 1)
        self.assertTrue(batch[0][1])

    def test_broken_checkpoint_or_removed_symbol_cannot_block_collection(self):
        for raw in ('{', '[]', '{"version":1,"states":{"bad":{"retry":"tomorrow"}}}'):
            self.path.write_text(raw)
            schedule = FundingSyncSchedule(self.path)
            self.assertTrue(schedule.plan({"DEMO": self.target}, {}, self.now, 8))
            self.assertNotIn("bad", schedule.states)

    def test_unexpected_interval_change_found_by_periodic_safety_poll(self):
        schedule = FundingSyncSchedule(self.path)
        current = expected_settlement(self.target, self.now)
        schedule.done("DEMO", self.target, self.now-901, current, full=True)
        self.assertTrue(schedule.plan({"DEMO": self.target}, {"DEMO": current}, self.now, 8))

    def test_global_tail_tied_timestamps_does_not_drop_symbols_or_future_filter(self):
        end = int(self.now*1000)
        payload = [{"symbol": f"C{i}USDT", "fundingTime": end, "fundingRate": ".00005"} for i in range(999)]
        payload.append({"symbol": "FUTUREUSDT", "fundingTime": end+1, "fundingRate": "9"})
        get = Mock(return_value=payload)
        rows = self.client(get).binance_recent(end)
        self.assertEqual(len(rows), 999)
        self.assertTrue(all(rate == .005 for _, _, rate in rows))
        self.assertNotIn('symbol=', get.call_args.args[0])
        self.assertIn('limit=1000', get.call_args.args[0])

    def test_hourly_full_backfill_paginates_and_preserves_rates_and_overlap(self):
        end = int(self.now*1000)
        history = [{"fundingTime": end-hour*3600000, "fundingRate": str((-.005 if hour % 2 else .005)/100)} for hour in range(750)]
        def get(url, **_):
            params = parse_qs(urlparse(url).query)
            until = int(params.get('endTime', [end])[0])
            return sorted([row for row in history if row['fundingTime'] <= until][:200], key=lambda row: row['fundingTime'])
        client = self.client(Mock(side_effect=get))
        raw, rows = client.fetch("Binance", ["DEMOUSDT"], end-31*DAY_MS, end)
        self.assertEqual(raw, "DEMOUSDT")
        self.assertEqual(len(rows), 745)
        self.assertEqual(client.get_json.call_count, 4)
        self.assertEqual(rows[-1], (end, .005))

    def test_stalled_full_pagination_is_failure_not_successful_coverage(self):
        end = int(self.now*1000)
        payload = [{"fundingTime": end-i*3600000, "fundingRate": "0.00005"} for i in range(200)]
        client = self.client(Mock(return_value=payload))
        with self.assertRaisesRegex(FundingHistoryError, 'pagination_stalled'):
            client.fetch("Binance", ["DEMOUSDT"], end-31*DAY_MS, end)

    def test_okx_does_not_store_predicted_rate_as_realized(self):
        end = int(self.now*1000)
        payload = {"code": "0", "data": [{"fundingTime": str(end), "fundingRate": "99", "realizedRate": "0.00005"}]}
        client = self.client(Mock(return_value=payload))
        self.assertEqual(client.fetch("OKX", ["DEMOUSDT"], end-DAY_MS, end)[1], [(end, .005)])
        payload['data'][0]['realizedRate'] = ''
        with self.assertRaisesRegex(FundingHistoryError, 'incomplete_settlement'):
            client.fetch("OKX", ["DEMOUSDT"], end-DAY_MS, end)

    def test_nonfinite_and_missing_rate_are_not_zero(self):
        for value in ('NaN', 'inf', '', None, True):
            with self.assertRaises((FundingHistoryError, ValueError)):
                FundingHistoryClient.fact(1234, value)
        self.assertEqual(FundingHistoryClient.fact(1234, "0"), (1234, 0))

    def test_scoped_alias_fallback_and_rate_errors_not_empty_history(self):
        end = int(self.now*1000)
        get = Mock(side_effect=[{"retCode":10001}, {"retCode":0, "result":{"list":[{"fundingRateTimestamp":str(end), "fundingRate":"0.00005"}]}}])
        self.assertEqual(self.client(get).fetch("Bybit", ["XECUSDT", "1000XECUSDT"], end-DAY_MS, end), ("1000XECUSDT", [(end, .005)]))
        get = Mock(return_value={"retCode":10006})
        client = self.client(get)
        with self.assertRaises(FundingHistoryError):
            client.fetch("Bybit", ["XECUSDT", "1000XECUSDT"], end-DAY_MS, end)
        with self.assertRaisesRegex(FundingHistoryError, 'rate_backoff'):
            client.fetch("Bybit", ["BTCUSDT"], end-DAY_MS, end)
        self.assertEqual(get.call_count, 1)

    def test_http_rate_limit_respects_retry_after_and_cooldown(self):
        get = Mock(side_effect=HTTPError('https://example.invalid', 429, 'limit', {'Retry-After': '120'}, None))
        client = self.client(get)
        with self.assertRaises(FundingHistoryError):
            client.binance_recent(int(self.now*1000))
        self.assertGreater(client.gates['Binance'].blocked_until, time.monotonic()+110)
        with self.assertRaisesRegex(FundingHistoryError, 'rate_backoff'):
            client.binance_recent(int(self.now*1000))
        self.assertEqual(get.call_count, 1)

    def test_gate_leaves_headroom_under_binance_shared_budget(self):
        clock = [0.]
        gate = RequestGate(.8, lambda: clock[0], lambda delay: clock.__setitem__(0, clock[0]+delay))
        starts = []
        for _ in range(376):
            gate.wait()
            starts.append(clock[0])
        self.assertGreaterEqual(starts[-1], 299.99)
        self.assertTrue(all(b-a >= .79999 for a, b in zip(starts, starts[1:])))


class FundingCollectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.context = module.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        for model in (module.FundingRateRecord, module.ExchangeFundingRateRecord):
            model.__table__.create(module.db.engine, checkfirst=True)
            model.query.filter(model.symbol.like("SYNCFIX%")).delete(synchronize_session=False)
        module.db.session.commit()
        self.addCleanup(module.db.session.remove)
        for name, cache in (("FUNDING_STATISTICS_CACHE", {"ts":0,"symbols":frozenset(),"data":{}}),
                            ("DUAL_FUNDING_STATISTICS_CACHE", {"ts":0,"key":frozenset(),"data":{}})):
            patched = patch.object(module, name, cache)
            patched.start()
            self.addCleanup(patched.stop)

    def test_commit_invalidates_both_caches_and_duplicate_does_not_double_count(self):
        stamp = int((time.time()-60)*1000)
        module.store_settled_funding("Binance", [("SYNCFIXUSDT", stamp-3600000, .005)])
        self.assertAlmostEqual(module.funding_statistics(["SYNCFIXUSDT"])["SYNCFIXUSDT"]["day_1"], .005)
        self.assertEqual(module.store_settled_funding("Binance", [("SYNCFIXUSDT", stamp, .005)]), 1)
        self.assertEqual(module.FUNDING_STATISTICS_CACHE['ts'], 0)
        self.assertAlmostEqual(module.funding_statistics(["SYNCFIXUSDT"])["SYNCFIXUSDT"]["day_1"], .010)
        self.assertEqual(module.store_settled_funding("Binance", [("SYNCFIXUSDT", stamp, .005)]), 0)
        self.assertEqual(module.FundingRateRecord.query.filter_by(symbol="SYNCFIXUSDT").count(), 2)

    def test_scope_includes_binance_contracts_without_spot_and_verified_raw_alias(self):
        spot = {"symbols": [{"symbol":"BTC/USDT", "rows":[{"funding_interval_hours":8}]}]}
        dual = {"symbols": [{"symbol":symbol,"rows":[{"long_exchange":"Bybit", "short_exchange":"Binance", "short_funding_interval_hours":4}]} for symbol in ("SYNCFIX/USDT", "XEC/USDT")]}
        aliases = [SimpleNamespace(**item, verified=True) for item in module.SEEDED_SYMBOL_ALIASES]
        cache = {"https://fapi.binance.com/fapi/v1/premiumIndex": {"payload":[{"symbol":"1000XECUSDT"},{"symbol":"SYNCFIXUSDT"}]}}
        with patch.object(module,"load_latest_market_snapshot",return_value=spot), patch.object(module,"load_latest_dual_futures_snapshot",return_value=dual), patch.object(module,"symbol_alias_rows",return_value=aliases), patch.object(module,"MARKET_PAYLOAD_CACHE",cache):
            catalog = module.funding_sync_catalog("Binance")
        self.assertEqual(set(catalog), {"BTCUSDT", "1000XECUSDT", "SYNCFIXUSDT"})
        self.assertEqual(catalog["1000XECUSDT"]["candidates"], ["1000XECUSDT"])

    def test_old_interior_gap_is_inserted_without_rewriting_existing_facts(self):
        stamp = int((time.time()-60)*1000)
        module.store_settled_funding("OKX", [("SYNCFIX/USDT", stamp, .005), ("SYNCFIX/USDT", stamp-30*DAY_MS, .001)])
        module.store_settled_funding("OKX", [("SYNCFIX/USDT", stamp, 99), ("SYNCFIX/USDT", stamp-7*DAY_MS, -.007)])
        rows = module.ExchangeFundingRateRecord.query.filter_by(exchange="OKX", symbol="SYNCFIX/USDT").all()
        self.assertEqual(len(rows), 3)
        self.assertEqual(next(row.funding_rate for row in rows if row.funding_time == stamp), .005)

    def test_invalidation_waits_for_inflight_aggregate_to_finish(self):
        module.FUNDING_STATISTICS_LOCK.acquire()
        done = threading.Event()
        worker = threading.Thread(target=lambda: (module.invalidate_funding_statistics(), done.set()))
        try:
            worker.start()
            self.assertFalse(done.wait(.02))
            module.FUNDING_STATISTICS_CACHE.update(ts=time.time(), data={'old': True})
        finally:
            module.FUNDING_STATISTICS_LOCK.release()
        worker.join(1)
        self.assertTrue(done.is_set())
        self.assertEqual(module.FUNDING_STATISTICS_CACHE['data'], {})

    def test_batch_fetches_outside_db_thread_and_preserves_failed_target_for_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            schedule = FundingSyncSchedule(Path(folder) / 'state.json')
            schedule.last_cleanup = time.time()
            targets = {key: {'candidates':[key.replace('/','')], 'interval':1} for key in ('SYNCFIX/USDT','SYNCFIXFAIL/USDT')}
            owner = threading.get_ident()
            seen = []
            stamp = int((time.time()-60)*1000)
            def fetch(venue, candidates, start, end):
                seen.append(threading.get_ident())
                self.assertEqual(venue, 'Bybit')
                if 'FAIL' in candidates[0]:
                    raise TimeoutError()
                return candidates[0], [(stamp, .005)]
            with patch.object(module,'funding_sync_catalog',return_value=targets), patch.object(module,'funding_sync_latest',return_value={}), patch.object(module.FUNDING_HISTORY_CLIENT,'fetch',side_effect=fetch), patch.object(module.app.logger,'warning'):
                status = module.run_funding_sync_batch('Bybit',schedule)
            self.assertEqual((status['attempted'], status['inserted'], status['errors']), (2,1,1))
            self.assertTrue(all(worker != owner for worker in seen))
            self.assertEqual(module.ExchangeFundingRateRecord.query.filter_by(exchange='Bybit',symbol='SYNCFIX/USDT').count(),1)
            self.assertNotIn('success', schedule.states['SYNCFIXFAIL/USDT'])
            self.assertGreater(schedule.states['SYNCFIXFAIL/USDT']['retry'],time.time())
            self.assertEqual(json.loads(schedule.path.read_text())['status']['errors'],1)

    def test_new_settlement_reaches_unchanged_spot_snapshot_on_next_request(self):
        from contextlib import ExitStack
        group = {"symbol":"SYNCFIX/USDT", "rows":[{"long_exchange":"Gate", "open_spread":1}]}
        stamp = int((time.time()-60)*1000)
        module.store_settled_funding("Binance", [("SYNCFIXUSDT",stamp-3600000,.005)])
        with module.app.test_request_context('/api/spot-futures'), ExitStack() as stack:
            stack.enter_context(patch.object(module,'load_latest_market_snapshot',return_value={'symbols':[group], 'updated_at':'same'}))
            stack.enter_context(patch.object(module,'SPOT_VIEW_CACHE',{'symbols':[group],'key':'same'}))
            for name in ('enrich_transfer_networks','mark_announced_delistings'):
                stack.enter_context(patch.object(module,name))
            stack.enter_context(patch.object(module,'is_rwa_stock_pair',return_value=False))
            first = module.spot_futures().get_json()
            self.assertAlmostEqual(first['symbols'][0]['rows'][0]['funding_24h'], .005)
            module.store_settled_funding('Binance', [('SYNCFIXUSDT',stamp,.005)])
            second = module.spot_futures().get_json()
            self.assertAlmostEqual(second['symbols'][0]['rows'][0]['funding_24h'], .010)


if __name__ == '__main__':
    unittest.main()

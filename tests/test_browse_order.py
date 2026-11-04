import copy
import json
import os
import unittest
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest.mock import patch
from urllib.parse import urlencode

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "browse-order-test")
os.environ.setdefault("CHAT_ENCRYPTION_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
import app as module


def group(index):
    return {"symbol": f"COIN{index:02}/USDT", "rows": [
        {"long_exchange": "Bybit", "short_exchange": "Binance", "open_spread": index,
         "funding_interval_hours": 4},
        {"long_exchange": "Bybit", "short_exchange": "OKX", "open_spread": index + .5,
         "funding_interval_hours": 4},
    ]}


class BrowseOrderApiTests(unittest.TestCase):
    def request(self, scope, groups, **params):
        snapshot = {"symbols": groups, "updated_at": "now", "captured_at_epoch": 123, "errors": {}}
        loader = "load_latest_market_snapshot" if scope == "spot" else "load_latest_dual_futures_snapshot"
        with module.app.test_request_context(f"/api/{scope}-futures?" + urlencode(params)), ExitStack() as stack:
            stack.enter_context(patch.object(module, loader, return_value=snapshot))
            for name in ("enrich_funding_statistics", "enrich_price_changes", "enrich_basis_openings",
                         "enrich_transfer_networks", "mark_announced_delistings", "enrich_dual_binance_reference",
                         "enrich_dual_basis_references", "enrich_next_funding_net", "enrich_dual_index_overlap",
                         "enrich_dual_funding_statistics"):
                stack.enter_context(patch.object(module, name))
            stack.enter_context(patch.object(module, "is_rwa_stock_pair", side_effect=lambda s: s.startswith("STOCK")))
            stack.enter_context(patch.object(module, "get_json", side_effect=AssertionError("No upstream request")))
            result = (module.spot_futures if scope == "spot" else module.dual_futures)()
            response, code = result if isinstance(result, tuple) else (result, 200)
            return code, response.get_json()

    def test_watched_coin_outside_ranked_page_gets_fresh_values_in_one_response(self):
        for scope in ("spot", "dual"):
            groups = [group(i) for i in range(65)]
            original = copy.deepcopy(groups)
            code, data = self.request(scope, groups, observed_symbols=json.dumps(["COIN00/USDT", "COIN01/USDT"]))
            self.assertEqual(code, 200)
            self.assertNotIn("COIN00/USDT", [g["symbol"] for g in data["symbols"]])
            self.assertNotIn("open_spread", data["symbols"][0]["rows"][0])  # compact rank identities
            self.assertEqual([g["symbol"] for g in data["observed_symbols"]], ["COIN00/USDT", "COIN01/USDT"])
            groups[0]["rows"][0]["open_spread"] = -123
            _, fresh = self.request(scope, groups, observed_symbols='["COIN00/USDT"]')
            watched = fresh["observed_symbols"][0]
            self.assertIn(-123, [r["open_spread"] for r in watched["rows"]])
            self.assertEqual(groups[1:], original[1:])
            self.assertEqual((data["total_symbols"], data["pages"]), (65, 3))

    def test_observed_lookup_cannot_bypass_search_rwa_or_path_filter(self):
        groups = [group(0), group(1), {**group(2), "symbol": "STOCK/USDT"}]
        for scope in ("spot", "dual"):
            _, data = self.request(scope, groups, symbol="COIN00", observed_symbols='["COIN00/USDT","COIN01/USDT","STOCK/USDT"]')
            self.assertEqual([g["symbol"] for g in data["observed_symbols"]], ["COIN00/USDT"])
            if scope == "dual":
                self.assertEqual(len(data["observed_symbols"][0]["rows"]), 1)
                _, data = self.request(scope, groups, include_bybit_okx=1, observed_symbols='["COIN00/USDT"]')
                self.assertEqual(len(data["observed_symbols"][0]["rows"]), 2)
        for params in ({"binance_spot_only": "1"}, {"funding_interval": "8H"}):
            _, data = self.request("spot", groups, observed_symbols='["COIN00/USDT"]', **params)
            self.assertEqual(data["observed_symbols"], [])
        with patch.object(module, "load_binance_alpha_symbols", return_value=(frozenset({"COIN01"}), "now", False)):
            _, data = self.request("spot", groups, binance_alpha_only=1, observed_symbols='["COIN00/USDT","COIN01/USDT"]')
            self.assertEqual([g["symbol"] for g in data["observed_symbols"]], ["COIN01/USDT"])

    def test_missing_empty_and_duplicate_symbols_are_bounded_without_fake_quotes(self):
        for scope in ("spot", "dual"):
            _, data = self.request(scope, [group(1)], observed_symbols='["MISSING/USDT","COIN01/USDT","COIN01/USDT"]')
            self.assertEqual(len(data["observed_symbols"]), 1)
            _, data = self.request(scope, [group(1)], observed_symbols='[]')
            self.assertEqual(data["observed_symbols"], [])
            _, data = self.request(scope, [group(1)])
            self.assertNotIn("observed_symbols", data)
            self.assertIn("open_spread", data["symbols"][0]["rows"][0])

    def test_invalid_batches_rejected_before_snapshot_read(self):
        invalid = ['oops', '{}', 'null', '[1]', '[true]', '[""]', '["a\\n"]', json.dumps(["x"] * 31),
                   json.dumps(["x" * 81]), " " * 4097]
        for scope in ("spot", "dual"):
            for raw in invalid:
                with self.subTest(scope=scope, raw=raw[:20]):
                    code, data = self.request(scope, [], observed_symbols=raw)
                    self.assertEqual(code, 400)
                    self.assertIn("error", data)

    def test_reader_keeps_rows_from_its_own_quote_generation(self):
        class Rows(list):
            pass

        for scope in ("spot", "dual"):
            route = "/api/" + ("spot-futures" if scope == "spot" else "dual-futures")
            view = module.spot_futures if scope == "spot" else module.dual_futures
            storage = module.SPOT_FUTURES_CACHE if scope == "spot" else module.DUAL_FUTURES_CACHE
            step = "enrich_funding_statistics" if scope == "spot" else "enrich_dual_funding_statistics"
            entered, release = threading.Event(), threading.Event()

            def quote(n):
                return module.PublishedMarketSnapshot(dict(updated_at="10:00:00", captured_at_epoch=n,
                    symbols=Rows([{"symbol": "TEST/USDT", "rows": [{"long_exchange": "Gate", "short_exchange": "Binance", "open_spread": n}]}])))

            def gate(groups):
                if groups[0]["rows"][0]["open_spread"] == 1:
                    entered.set()
                    self.assertTrue(release.wait(5))

            def read():
                with module.app.test_request_context(route):
                    value = view().get_json()
                    return (value["captured_at_epoch"], value["symbols"][0]["rows"][0]["open_spread"])

            with self.subTest(scope=scope), ExitStack() as stack:
                stack.enter_context(patch.dict(storage, {}))
                storage["snapshot"] = quote(1)
                for name in ("enrich_price_changes", "enrich_basis_openings", "enrich_transfer_networks",
                             "mark_announced_delistings", "enrich_dual_binance_reference", "enrich_dual_basis_references",
                             "enrich_next_funding_net", "enrich_dual_index_overlap"):
                    stack.enter_context(patch.object(module, name, new=lambda *args, **kwargs: None))
                stack.enter_context(patch.object(module, "is_rwa_stock_pair", new=lambda symbol: False))
                stack.enter_context(patch.object(module, step, new=gate))
                first_rows = weakref.ref(storage["snapshot"]["symbols"])
                with ThreadPoolExecutor(max_workers=1) as pool:
                    first = pool.submit(read)
                    try:
                        self.assertTrue(entered.wait(5))
                        storage["snapshot"] = quote(2)
                        self.assertEqual(read(), (2, 2))
                        self.assertIsNotNone(first_rows())
                    finally:
                        release.set()
                    self.assertEqual(first.result(5), (1, 1))
                self.assertIsNone(first_rows())
                old = weakref.ref(storage["snapshot"]["symbols"])
                storage["snapshot"] = quote(3)
                self.assertIsNone(old(), "a page cache retained the previous tick with no active reader")

    def test_overlapping_readers_prepare_a_generation_once_and_release_before_waiting(self):
        snapshot = module.PublishedMarketSnapshot({"symbols": [{}]})
        entered, release, second_released = (threading.Event() for _ in range(3))
        joined = threading.local()
        removed = module.db.session.remove
        runs = []

        def mark_removed():
            removed()
            if getattr(joined, "is_second", False):
                second_released.set()

        def prepare(groups):
            runs.append(len(groups))
            entered.set()
            self.assertTrue(release.wait(5))

        def reader(second):
            with module.app.app_context():
                joined.is_second = second
                return module.market_view_groups(snapshot, prepare)

        with patch.object(module.db.session, "remove", new=mark_removed), ThreadPoolExecutor(2) as pool:
            first = pool.submit(reader, False)
            try:
                self.assertTrue(entered.wait(5))
                other = pool.submit(reader, True)
                self.assertTrue(second_released.wait(5))
            finally:
                release.set()
            self.assertIs(first.result(5), snapshot["symbols"])
            self.assertIs(other.result(5), snapshot["symbols"])
        self.assertEqual(runs, [1])

    def test_isolated_http_verifier_rejects_current_header_with_previous_rows(self):
        from scripts.soak_http_peer import check_market_page
        page = dict(captured_at_epoch=2, total_symbols=2, page_size=30,
                    symbols=[{"rows": [{"captured_at_epoch": 2}]}, {"rows": [{"captured_at_epoch": 2}]}])
        self.assertEqual(check_market_page(page, 2), 2)
        page["symbols"][1]["rows"][0]["captured_at_epoch"] = 1
        with self.assertRaisesRegex(AssertionError, "mixed generations"):
            check_market_page(page, 2)

    def test_failed_enrichment_can_retry_without_marking_the_generation_ready(self):
        snapshot = module.PublishedMarketSnapshot({"symbols": [{}]})
        def failed(groups):
            raise RuntimeError("generated failure")
        calls = []
        with module.app.app_context():
            with self.assertRaisesRegex(RuntimeError, "generated failure"):
                module.market_view_groups(snapshot, failed)
            for _ in range(2):
                self.assertIs(module.market_view_groups(snapshot, lambda groups: calls.append(len(groups))), snapshot["symbols"])
        self.assertEqual(calls, [1])
        self.assertEqual(json.loads(json.dumps(snapshot)), {"symbols": [{}]})

    def test_observed_api_stays_authenticated_and_uncached(self):
        for scope in ("spot", "dual"):
            response = module.app.test_client().get(f'/api/{scope}-futures?observed_symbols=%5B%22BTC/USDT%22%5D')
            self.assertEqual(response.status_code, 401)
            self.assertIn("no-store", response.headers["Cache-Control"])


if __name__ == "__main__":
    unittest.main()

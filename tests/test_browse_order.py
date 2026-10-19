import copy
import json
import os
import unittest
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
        cache = "SPOT_VIEW_CACHE" if scope == "spot" else "DUAL_VIEW_CACHE"
        with module.app.test_request_context(f"/api/{scope}-futures?" + urlencode(params)), ExitStack() as stack:
            stack.enter_context(patch.object(module, loader, return_value=snapshot))
            stack.enter_context(patch.object(module, cache, {"key": None, "symbols": None}))
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

    def test_observed_api_stays_authenticated_and_uncached(self):
        for scope in ("spot", "dual"):
            response = module.app.test_client().get(f'/api/{scope}-futures?observed_symbols=%5B%22BTC/USDT%22%5D')
            self.assertEqual(response.status_code, 401)
            self.assertIn("no-store", response.headers["Cache-Control"])


if __name__ == "__main__":
    unittest.main()

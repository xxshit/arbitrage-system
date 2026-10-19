import copy
import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "spot-sort-test")
os.environ.setdefault("CHAT_ENCRYPTION_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")

import app as module


def group(symbol, values, **extra):
    return dict(symbol=symbol, rows=[dict(long_exchange=venue, open_spread=value, close_spread=value,
                basis=.1, funding_rate=.01, funding_interval_hours=4, spot_volume=index + 100,
                transfer_status={"exchange": venue, "state": "unknown"})
                for index, (venue, value) in enumerate(values)], **extra)


class SpotSortingTests(unittest.TestCase):
    def setUp(self):
        self.groups = [
            group("IDOL/USDT", [("Gate", .3354), ("Bitget", .2793)]),
            group("EPIC/USDT", [("Binance", .1103), ("Gate", .3316)]),
            group("SOPH/USDT", [("Binance", -.2), ("Gate", .1003), ("Bitget", .3015)]),
        ]

    def request(self, groups, query="", **overrides):
        snapshot = dict(symbols=groups, errors={}, updated_at="12:00:00", captured_at_epoch=123)
        with module.app.test_request_context("/api/spot-futures?" + query), ExitStack() as stack:
            stack.enter_context(patch.object(module, "load_latest_market_snapshot", return_value=snapshot))
            stack.enter_context(patch.object(module, "SPOT_VIEW_CACHE", {"key": None, "symbols": None}))
            for name in ("enrich_funding_statistics", "enrich_price_changes", "enrich_basis_openings",
                         "enrich_transfer_networks", "mark_announced_delistings"):
                stack.enter_context(patch.object(module, name))
            stack.enter_context(patch.object(module, "is_rwa_stock_pair", return_value=False))
            stack.enter_context(patch.object(module, "get_json", side_effect=AssertionError("No upstream read")))
            for name, value in overrides.items():
                stack.enter_context(patch.object(module, name, return_value=value))
            return module.spot_futures().get_json()

    def test_descending_uses_maximum_path_and_moves_it_to_first_row(self):
        result = module.sort_spot_groups(self.groups, "open_spread", "desc")
        self.assertEqual([g["symbol"] for g in result], ["IDOL/USDT", "EPIC/USDT", "SOPH/USDT"])
        self.assertEqual([g["rows"][0]["long_exchange"] for g in result], ["Gate", "Gate", "Bitget"])
        self.assertEqual([r["open_spread"] for r in result[2]["rows"]], [.3015, .1003, -.2])

    def test_ascending_uses_minimum_not_maximum_or_absolute_value(self):
        groups = [group("A", [("Gate", -.1), ("Bitget", -5)]), group("B", [("Gate", -.5)]),
                  group("C", [("Gate", 0)]), group("D", [("Gate", 10)])]
        for key in ("open_spread", "close_spread"):
            result = module.sort_spot_groups(groups, key, "asc")
            self.assertEqual([g["symbol"] for g in result], ["A", "B", "C", "D"])
            self.assertEqual([r[key] for r in result[0]["rows"]], [-5, -.1])
            self.assertEqual(module.sort_spot_groups(groups, key, "desc")[0]["symbol"], "D")

    def test_close_spread_uses_its_own_path_order_not_opening_order(self):
        self.groups[2]["rows"][0]["close_spread"] = .9
        result = module.sort_spot_groups(self.groups, "close_spread", "desc")
        self.assertEqual(result[0]["symbol"], "SOPH/USDT")
        self.assertEqual([r["long_exchange"] for r in result[0]["rows"]], ["Binance", "Bitget", "Gate"])

    def test_missing_and_nonfinite_paths_and_groups_remain_last_both_directions(self):
        values = [("missing", None), ("nan", float("nan")), ("positive-inf", float("inf")),
                  ("negative-inf", -float("inf")), ("string", "9"), ("bool", True), ("zero", 0), ("negative", -1)]
        groups = [group("missing", [("Gate", None)]), group("valid", values), group("empty", [])]
        for direction, first in (("asc", [-1, 0]), ("desc", [0, -1])):
            result = module.sort_spot_groups(groups, "open_spread", direction)
            self.assertEqual([g["symbol"] for g in result], ["valid", "missing", "empty"])
            self.assertEqual([r["open_spread"] for r in result[0]["rows"][:2]], first)
            self.assertEqual([r["long_exchange"] for r in result[0]["rows"][2:]], [v[0] for v in values[:6]])

    def test_sort_does_not_mutate_shared_cache_or_drop_path_metadata(self):
        self.groups[2]["rows"][0]["price_divergence"] = {"state": "confirmed", "blocked_chains": ["BSC 充关"]}
        original = copy.deepcopy(self.groups)
        descending = module.sort_spot_groups(self.groups, "open_spread", "desc")
        ascending = module.sort_spot_groups(self.groups, "open_spread", "asc")
        self.assertEqual(self.groups, original)
        self.assertEqual(descending[2]["rows"][0]["long_exchange"], "Bitget")
        self.assertEqual(ascending[0]["rows"][0]["price_divergence"]["blocked_chains"], ["BSC 充关"])
        for ordered in (descending, ascending):
            for item in ordered:
                source = next(g for g in original if g["symbol"] == item["symbol"])
                self.assertEqual({r["long_exchange"]: r for r in item["rows"]}, {r["long_exchange"]: r for r in source["rows"]})

    def test_equal_values_are_stable_and_shared_metrics_keep_original_venue_order(self):
        for direction in ("asc", "desc"):
            tied = [group("B", [("Binance", 0), ("Gate", 0)]), group("A", [("Gate", 0)])]
            self.assertEqual(module.sort_spot_groups(tied, "open_spread", direction), tied)
            result = module.sort_spot_groups(self.groups, "basis", direction)
            self.assertEqual(result, self.groups)

    def test_all_shared_sort_keys_keep_missing_last_and_paths_unmodified(self):
        for key in ("basis", "funding_rate", "funding_previous", "funding_24h", "funding_3d", "funding_7d", "funding_30d",
                    "change_5m", "change_15m", "change_30m", "change_1h", "change_12h", "change_24h", "change_3d", "change_7d"):
            groups = copy.deepcopy(self.groups)
            for item, value in zip(groups, (.2, -.1, None)):
                for row in item["rows"]:
                    row[key] = value
            for direction, names in (("desc", ["IDOL/USDT", "EPIC/USDT", "SOPH/USDT"]), ("asc", ["EPIC/USDT", "IDOL/USDT", "SOPH/USDT"])):
                result = module.sort_spot_groups(groups, key, direction)
                self.assertEqual([g["symbol"] for g in result], names)
                for item in result:
                    self.assertEqual(item["rows"], next(g["rows"] for g in groups if g["symbol"] == item["symbol"]))

    def test_api_returns_first_row_matching_requested_group_rank(self):
        for direction in ("asc", "desc"):
            data = self.request(self.groups, "sort_by=open_spread&sort_direction=" + direction)
            self.assertEqual(data["sort_direction"], direction)
            ordered = data["symbols"]
            keys = [g["rows"][0]["open_spread"] for g in ordered]
            self.assertEqual(keys, sorted(keys, reverse=direction == "desc"))
            for item in ordered:
                values = [r["open_spread"] for r in item["rows"]]
                self.assertEqual(values, sorted(values, reverse=direction == "desc"))

    def test_api_globally_sorts_before_pagination_without_splitting_coin_paths(self):
        groups = [group(f"TEST{i:02}/USDT", [("Binance", 0), ("Gate", i + 1), ("Bitget", -(i + 1))]) for i in range(65)]
        for direction in ("asc", "desc"):
            names = []
            for page in range(1, 4):
                data = self.request(groups, f"page={page}&sort_direction={direction}")
                self.assertEqual((data["total_symbols"], data["pages"]), (65, 3))
                self.assertTrue(all(len(g["rows"]) == 3 for g in data["symbols"]))
                names.extend(g["symbol"] for g in data["symbols"])
            self.assertEqual(names, [g["symbol"] for g in reversed(groups)])
            self.assertEqual(len(set(names)), 65)

    def test_api_search_period_and_bn_filters_apply_before_sorting(self):
        self.groups[0]["rows"][0]["funding_interval_hours"] = 8
        data = self.request(self.groups, "binance_spot_only=1&funding_interval=4H&symbol=SOPH&sort_direction=asc")
        self.assertEqual(data["total_symbols"], 1)
        self.assertEqual(data["symbols"][0]["symbol"], "SOPH/USDT")
        self.assertEqual(len(data["symbols"][0]["rows"]), 3)  # BN filter keeps a coin, not only its BN route.
        data = self.request(self.groups, "binance_alpha_only=1&sort_direction=desc",
                            load_binance_alpha_symbols=(frozenset({"SOPH"}), "12:00:00", False))
        self.assertEqual([g["symbol"] for g in data["symbols"]], ["SOPH/USDT"])
        self.assertEqual(data["symbols"][0]["rows"][0]["long_exchange"], "Bitget")

    def test_api_invalid_options_keep_default_signed_descending_sort(self):
        data = self.request(self.groups, "sort_by=unknown&sort_direction=invalid")
        self.assertEqual((data["sort_by"], data["sort_direction"]), ("open_spread", "desc"))
        self.assertEqual(data["symbols"][2]["rows"][0]["long_exchange"], "Bitget")


if __name__ == "__main__":
    unittest.main()

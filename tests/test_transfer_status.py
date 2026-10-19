import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "transfer-status-test")
os.environ.setdefault("CHAT_ENCRYPTION_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")

import app as module


class TransferStatusTests(unittest.TestCase):
    def setUp(self):
        module.app.config.update(TESTING=True)
        self.now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        self.aliases = patch.object(module, "symbol_alias_rows", return_value=[])
        self.aliases.start()
        with module.app.app_context():
            module.db.drop_all()
            module.db.create_all()
            user = module.UserAccount(username="transfer-test", password_hash="unused", role="admin", account_level="lv3", active_session_hash=module.session_digest("transfer-token"))
            module.db.session.add(user)
            module.db.session.commit()
            user_id = user.id
        self.client = module.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=user_id, auth_token="transfer-token")

    def tearDown(self):
        with module.app.app_context():
            module.db.session.remove()
            module.db.drop_all()
        self.aliases.stop()

    def record(self, symbol="DEMO/USDT", exchange="Gate", chains=None, captured_at=None):
        item = module.TransferNetworkSnapshot(symbol=symbol, exchange=exchange, chains_json=json.dumps(chains if chains is not None else [{"name": "BSC", "deposit_open": False, "withdraw_open": True}], ensure_ascii=False), captured_at=captured_at or self.now)
        module.db.session.add(item)
        module.db.session.commit()
        return item

    def status(self, symbol="DEMO/USDT", exchange="Gate"):
        return module.load_transfer_statuses([(symbol, exchange)], now=self.now)[(symbol, exchange)]

    def test_flags_are_tristate_not_truthy_strings_or_default_closed(self):
        for value in (True, 1, "true", "TRUE", "1"):
            self.assertIs(module.transfer_flag(value), True)
        for value in (False, 0, "false", "FALSE", "0"):
            self.assertIs(module.transfer_flag(value), False)
        for value in (None, "", "unknown", "closed?", 2):
            self.assertIsNone(module.transfer_flag(value))

    def test_gate_uses_per_chain_flags_and_does_not_split_distinct_underscore_assets(self):
        data = module.parse_transfer_networks("Gate", [{"currency": "TOKEN_OLD", "deposit_disabled": True, "chains": [{"name": "ETH", "deposit_disabled": "false", "withdraw_disabled": "true", "withdraw_delayed": True}, {"name": "BSC"}]}])
        self.assertEqual(list(data), ["TOKEN_OLD/USDT"])
        self.assertEqual(data["TOKEN_OLD/USDT"][0], {"name": "ETH", "deposit_open": True, "withdraw_open": False, "withdraw_delayed": True})
        self.assertIsNone(data["TOKEN_OLD/USDT"][1]["deposit_open"])
        self.assertIsNone(data["TOKEN_OLD/USDT"][1]["withdraw_open"])

    def test_bitget_flags_are_independent_of_internal_account_transfer_flag(self):
        parsed = module.parse_transfer_networks("Bitget", {"code": "00000", "data": [{"coin": "rBIIB", "transfer": "false", "chains": [{"chain": "BEP20", "rechargeable": "true", "withdrawable": "false"}, {"chain": "ERC20"}]}]})
        self.assertEqual(parsed["RBIIB/USDT"][0]["deposit_open"], True)
        self.assertEqual(parsed["RBIIB/USDT"][0]["withdraw_open"], False)
        self.assertIsNone(parsed["RBIIB/USDT"][1]["withdraw_open"])

    def test_binance_public_networks_use_only_explicit_chain_flags(self):
        payload = {"code": "000000", "success": True, "data": [{"coin": "DEMO", "depositAllEnable": False, "withdrawAllEnable": False, "free": "SYNTHETIC_BALANCE_MUST_DROP", "networkList": [{"network": "ETH", "depositEnable": True, "withdrawEnable": False, "busy": True}, {"network": "BSC", "depositEnable": "false", "withdrawEnable": "1"}, {"network": "OTHER"}]}]}
        parsed = module.parse_transfer_networks("Binance", payload)
        self.assertEqual(parsed["DEMO/USDT"][0], {"name": "ETH", "deposit_open": True, "withdraw_open": False, "withdraw_delayed": None})
        self.assertEqual(parsed["DEMO/USDT"][1]["deposit_open"], False)
        self.assertEqual(parsed["DEMO/USDT"][1]["withdraw_open"], True)
        self.assertIsNone(parsed["DEMO/USDT"][2]["deposit_open"])
        self.assertNotIn("SYNTHETIC_BALANCE_MUST_DROP", json.dumps(parsed))

    def test_binance_failed_challenge_empty_or_bad_public_payload_never_marks_fresh(self):
        for payload in ("<html>challenge</html>", {"code": "000001", "success": True, "data": [{}]}, {"code": "000000", "success": False, "data": [{}]}, {"code": "000000", "success": True, "data": []}, {"code": "000000", "success": True, "data": [{"coin": "DEMO", "networkList": False}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                module.parse_transfer_networks("Binance", payload)

    def test_binance_all_chains_survive_storage_and_no_key_or_private_fields_are_used(self):
        networks = [{"network": f"CHAIN{index}", "depositEnable": index % 2 == 0, "withdrawEnable": True} for index in range(30)]
        raw = {"code": "000000", "success": True, "data": [{"coin": "DEMO", "networkList": networks, "locked": "NEVER_PERSIST_THIS"}]}
        with module.app.app_context():
            module.store_transfer_networks("Binance", module.parse_transfer_networks("Binance", raw))
            status = self.status(exchange="Binance")
            self.assertEqual(len(status["chains"]), 30)
            self.assertEqual(status["chains"][-1]["name"], "CHAIN29")
            self.assertEqual(status["state"], "fresh")
            self.assertNotIn("NEVER_PERSIST_THIS", json.dumps(status))
            self.assertEqual(status["source_url"], "https://www.binance.com/bapi/capital/v1/public/capital/getNetworkCoinAll")

    def test_missing_binance_public_coin_does_not_claim_key_is_needed_or_coin_is_closed(self):
        with module.app.app_context():
            status = self.status(exchange="Binance")
            self.assertEqual(status["state"], "unknown")
            self.assertIn("公开充提源暂未提供", status["reason"])
            self.assertNotIn("账户只读", status["reason"])
            self.assertEqual(status["chains"], [])

    def test_public_binance_request_is_get_with_no_key_cookie_or_auth_header(self):
        payload = {"code": "000000", "success": True, "data": [{"coin": "DEMO", "networkList": []}]}
        with patch.object(module, "urlopen") as opened:
            opened.return_value.__enter__.return_value.headers = {}
            opened.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            module.get_json(module.TRANSFER_PUBLIC_SOURCES["Binance"], timeout=8)
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://www.binance.com/bapi/capital/v1/public/capital/getNetworkCoinAll")
        names = {name.lower() for name, _ in request.header_items()}
        self.assertFalse(names & {"authorization", "cookie", "x-mbx-apikey", "x-bapi-api-key", "ok-access-key"})

    def test_all_source_chains_survive_parse_storage_and_api_without_filtering_or_cap(self):
        names = [f"SYNTHETIC-NETWORK-{index:02d}" for index in range(30)]
        with module.app.app_context():
            for exchange in ("Gate", "Bitget"):
                raw_chains = [({"name": name, "deposit_disabled": index % 3 == 0, "withdraw_disabled": index % 2 == 0} if exchange == "Gate" else {"chain": name, "rechargeable": "false" if index % 3 == 0 else "true", "withdrawable": "false" if index % 2 == 0 else "true"}) for index, name in enumerate(names)]
                payload = [{"currency": "MANY", "chains": raw_chains}] if exchange == "Gate" else {"code": "00000", "data": [{"coin": "MANY", "chains": raw_chains}]}
                parsed = module.parse_transfer_networks(exchange, payload)
                module.store_transfer_networks(exchange, parsed)
                status = self.status("MANY/USDT", exchange)
                self.assertEqual(status["chains"], parsed["MANY/USDT"])
                self.assertEqual([chain["name"] for chain in status["chains"]], names)
                module.db.session.add(module.AlertEvent(symbol="MANY/USDT", strategy="spot_futures", alert_type="rapid_basis", message=f"{exchange} 确认后的合成报警", long_exchange=exchange, short_exchange="Binance", basis=1.2, created_at=self.now))
            module.db.session.commit()
        with patch.object(module, "is_rwa_stock_pair", return_value=False), patch.object(module, "get_json", side_effect=AssertionError("summary must not fetch upstream")):
            response = self.client.get("/api/alerts?view=summary")
        self.assertEqual(response.status_code, 200)
        statuses = {status["exchange"]: status for status in response.get_json()["events"][0]["transfer_statuses"]}
        for exchange in ("Gate", "Bitget"):
            self.assertEqual(len(statuses[exchange]["chains"]), 30)
            self.assertEqual([chain["name"] for chain in statuses[exchange]["chains"]], names)
            self.assertIs(statuses[exchange]["chains"][0]["deposit_open"], False)
        self.assertEqual(statuses["Binance"]["chains"], [])

    def test_invalid_or_failed_lists_raise_instead_of_refreshing_old_data_as_success(self):
        for exchange, payload in [("Gate", []), ("Gate", {}), ("Gate", [{"currency": "X", "chains": ["bad"]}]), ("Bitget", {"code": "40000", "data": []}), ("Bitget", {"code": "00000", "data": []})]:
            with self.subTest(exchange=exchange, payload=payload), self.assertRaises(ValueError):
                module.parse_transfer_networks(exchange, payload)

    def test_case_only_rename_updates_existing_row_without_duplicate_insert_or_batch_rollback(self):
        with module.app.app_context():
            old = self.record("RBIIB/USDT", "Bitget", captured_at=self.now - timedelta(days=70))
            old_id = old.id
            payload = {"code": "00000", "data": [{"coin": "rBIIB", "chains": [{"chain": "ERC20", "rechargeable": "false", "withdrawable": "true"}]}]}
            module.store_transfer_networks("Bitget", module.parse_transfer_networks("Bitget", payload))
            module.db.session.expire_all()
            updated = module.db.session.get(module.TransferNetworkSnapshot, old_id)
            self.assertEqual(module.TransferNetworkSnapshot.query.count(), 1)
            self.assertGreater(updated.captured_at, self.now - timedelta(minutes=1))
            self.assertFalse(json.loads(updated.chains_json)[0]["deposit_open"])

    def test_legacy_mixed_case_rows_are_also_found_without_duplicates(self):
        with module.app.app_context():
            self.record("rNvda/USDT", "Bitget")
            module.store_transfer_networks("Bitget", {"RNVDA/USDT": [{"name": "ETH", "deposit_open": True, "withdraw_open": True}]})
            self.assertEqual(module.TransferNetworkSnapshot.query.count(), 1)
            self.assertEqual(self.status("RNVDA/USDT", "Bitget")["chains"][0]["name"], "ETH")

    def test_one_exchange_failure_does_not_erase_or_block_the_other_and_is_reported(self):
        with module.app.app_context():
            old = self.record(captured_at=self.now - timedelta(hours=3))
            old_time = old.captured_at
            good = {"code": "00000", "data": [{"coin": "DEMO", "chains": [{"chain": "BEP20", "rechargeable": "true", "withdrawable": "true"}]}]}
            binance = {"code": "000000", "success": True, "data": [{"coin": "DEMO", "networkList": [{"network": "BSC", "depositEnable": True, "withdrawEnable": True}]}]}
            with patch.object(module, "get_json", side_effect=[TimeoutError("source timeout"), good, binance]), self.assertRaisesRegex(RuntimeError, "Gate.*TimeoutError"):
                module.refresh_public_transfer_networks()
            self.assertEqual(module.TransferNetworkSnapshot.query.filter_by(exchange="Gate").first().captured_at, old_time)
            self.assertEqual(self.status(exchange="Bitget")["state"], "fresh")
            self.assertEqual(self.status(exchange="Binance")["state"], "fresh")

    def test_failed_binance_public_fetch_keeps_prior_snapshot_and_other_sources_update(self):
        with module.app.app_context():
            old = self.record(exchange="Binance", captured_at=self.now - timedelta(hours=3))
            old_time = old.captured_at
            gate = [{"currency": "DEMO", "chains": [{"name": "BSC", "deposit_disabled": False, "withdraw_disabled": False}]}]
            bitget = {"code": "00000", "data": [{"coin": "DEMO", "chains": [{"chain": "BSC", "rechargeable": True, "withdrawable": True}]}]}
            with patch.object(module, "get_json", side_effect=[gate, bitget, ValueError("synthetic non-JSON response")]), self.assertRaisesRegex(RuntimeError, "Binance"):
                module.refresh_public_transfer_networks()
            self.assertEqual(module.TransferNetworkSnapshot.query.filter_by(exchange="Binance").first().captured_at, old_time)
            self.assertEqual(self.status(exchange="Gate")["state"], "fresh")
            self.assertEqual(self.status(exchange="Bitget")["state"], "fresh")

    def test_current_fresh_data_includes_explicit_utc_capture_time(self):
        with module.app.app_context():
            self.record()
            status = self.status()
            self.assertEqual(status["state"], "fresh")
            self.assertTrue(status["captured_at"].endswith(" UTC"))
            self.assertIs(status["chains"][0]["deposit_open"], False)
            self.assertIs(status["chains"][0]["withdraw_open"], True)

    def test_old_future_and_exact_expiry_boundary_have_explicit_freshness(self):
        with module.app.app_context():
            record = self.record()
            for seconds, expected in [(1800, "fresh"), (1801, "stale"), (-61, "stale")]:
                record.captured_at = self.now - timedelta(seconds=seconds)
                module.db.session.commit()
                self.assertEqual(self.status()["state"], expected)

    def test_missing_private_sources_empty_and_corrupt_data_are_unknown_not_open_or_closed(self):
        with module.app.app_context(), patch.object(module, "get_json", side_effect=AssertionError("HTTP reads may not fetch upstream")):
            for exchange in ("Gate", "Binance", "Bybit", "OKX"):
                self.assertEqual(self.status(exchange=exchange)["state"], "unknown")
            row = self.record(chains=[])
            self.assertEqual(self.status()["state"], "unknown")
            row.chains_json = '{"bad": "json shape"}'
            module.db.session.commit()
            self.assertEqual(self.status()["state"], "unknown")

    def test_aliases_are_verified_and_exchange_scoped_not_a_guessed_multiplier_strip(self):
        with module.app.app_context():
            self.record("XEC/USDT")
            self.assertEqual(self.status("1000XEC/USDT")["asset_symbol"], "XEC/USDT")
            self.record("RATS/USDT")
            self.assertEqual(self.status("1000RATS/USDT")["state"], "unknown")
            alias = SimpleNamespace(canonical_symbol="DEMO/USDT", alias_symbol="DEMO1/USDT", exchange="Bitget", market_type="spot", multiplier=1)
            self.record("DEMO1/USDT", "Gate")
            self.record("DEMO1/USDT", "Bitget")
            with patch.object(module, "symbol_alias_rows", return_value=[alias]):
                self.assertEqual(self.status(exchange="Gate")["state"], "unknown")
                self.assertEqual(self.status(exchange="Bitget")["asset_symbol"], "DEMO1/USDT")

    def test_spot_metadata_expires_even_if_the_price_cache_key_never_changes(self):
        symbols = [{"symbol": "DEMO/USDT", "rows": [{"long_exchange": "Gate", "open_spread": 1}]}]
        with module.app.app_context():
            self.record()
        with patch.object(module, "load_latest_market_snapshot", return_value={"symbols": symbols, "updated_at": "unchanged"}), patch.object(module, "SPOT_VIEW_CACHE", {"key": "unchanged", "symbols": symbols}), patch.object(module, "mark_announced_delistings"), patch.object(module, "is_rwa_stock_pair", return_value=False), patch.object(module, "get_json", side_effect=AssertionError("no upstream reads")):
            first = self.client.get("/api/spot-futures").get_json()
            self.assertEqual(first["symbols"][0]["rows"][0]["transfer_status"]["state"], "fresh")
            with module.app.app_context():
                row = module.TransferNetworkSnapshot.query.first()
                row.captured_at = self.now - timedelta(hours=3)
                module.db.session.commit()
            second = self.client.get("/api/spot-futures").get_json()
            self.assertEqual(second["symbols"][0]["rows"][0]["transfer_status"]["state"], "stale")
            self.assertEqual(second["symbols"][0]["rows"][0]["transfer_networks"], [])


if __name__ == "__main__":
    unittest.main()

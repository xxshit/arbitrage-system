import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "listing-history-test")
os.environ.setdefault("CHAT_ENCRYPTION_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")

import app as module


class ListingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.alias_patch = patch.object(module, "symbol_alias_rows", return_value=[])
        self.alias_patch.start()
        module.app.config.update(TESTING=True)
        self.now = datetime.now(module.SHANGHAI_TZ).replace(tzinfo=None)
        with module.app.app_context():
            module.db.drop_all()
            module.db.create_all()
            owner = module.UserAccount(username="listing-test", password_hash="unused", role="admin", account_level="lv3", active_session_hash=module.session_digest("listing-test-token"))
            module.db.session.add(owner)
            module.db.session.flush()
            self.user_id = owner.id
            module.db.session.add_all([
                self.event("ESPORTS/USDT", "Bitget", occurred=self.now - timedelta(days=44), effective=self.now - timedelta(days=40)),
                self.event("ESPORTS/USDT", "Gate", effective=self.now + timedelta(days=2)),
                self.event("ESPORTS/USDT", "Bybit", status="not_stated"),
                self.event("SATS/USDT", "Bitget", effective=self.now - timedelta(days=1)),
                self.event("METADATAUSDT", "Binance", official=False),
            ])
            module.db.session.commit()
        self.client = module.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
            session["auth_token"] = "listing-test-token"

    def tearDown(self):
        with module.app.app_context():
            module.db.session.remove()
            module.db.drop_all()
        self.alias_patch.stop()

    def event(self, symbol, exchange, occurred=None, effective=None, status="parsed", official=True):
        host = {"Bitget": "www.bitget.com", "Gate": "www.gate.com", "Bybit": "announcements.bybit.com"}.get(exchange, "www.binance.com")
        return module.ListingEvent(exchange=exchange, symbol=symbol, event_type="下架", title=f"Synthetic test: delist {symbol} futures" if official else None, source_url=f"https://{host}/support/example" if official else None, announcement=official, occurred_at=occurred or self.now, effective_at=effective, effective_time_status=status)

    def get(self, **params):
        response = self.client.get("/api/daily-report/listings", query_string=params)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_all_history_finds_esports_record_excluded_from_recent_30_days(self):
        recent = self.get(range="30d", symbol="ESPORTS")
        history = self.get(range="all", symbol="ESPORTS")
        self.assertEqual(recent["total"], 2)
        self.assertEqual(history["total"], 3)
        archived = next(item for item in history["events"] if item["exchange"] == "Bitget")
        self.assertEqual(archived["delisting_status"], "delisted")
        self.assertEqual(archived["market_scope"], "合约")
        self.assertTrue(archived["source_url"].startswith("https://www.bitget.com/"))

    def test_search_normalizes_case_and_pair_separators_before_pagination(self):
        for symbol in ("esports", "ESPORTS/USDT", "esports-usdt", "ESPORTS_USDT"):
            with self.subTest(symbol=symbol):
                self.assertEqual(self.get(range="all", symbol=symbol)["total"], 3)

    def test_history_is_paginated_without_a_hidden_100_record_limit(self):
        with module.app.app_context():
            module.db.session.add_all(self.event(f"COIN{index}/USDT", "Bitget") for index in range(126))
            module.db.session.commit()
        pages = [self.get(range="all", page=page, page_size=50) for page in (1, 2, 3)]
        self.assertEqual([len(page["events"]) for page in pages], [50, 50, 31])
        self.assertEqual(pages[0]["total"], 131)
        self.assertEqual(len({item["id"] for page in pages for item in page["events"]}), 131)
        self.assertFalse(pages[-1]["has_more"])

    def test_invalid_filters_are_rejected_and_page_sizes_bounded(self):
        for params in ({"range": "999d"}, {"symbol": "%"}, {"symbol": "' OR 1=1"}, {"symbol": "USDT"}, {"page": "bad"}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get("/api/daily-report/listings", query_string=params).status_code, 400)
        self.assertEqual(self.get(range="all", page_size=100000)["page_size"], 100)
        self.assertEqual(self.get(range="all", page=999)["page"], 1)

    def test_no_match_has_clear_empty_pagination(self):
        data = self.get(range="all", symbol="NOTAREALTOKEN")
        self.assertEqual((data["total"], data["pages"], data["page"], data["events"]), (0, 1, 1, []))

    def test_metadata_contains_exchange_specific_evidence_including_old_records(self):
        data = self.client.get("/api/market-metadata/delisted-symbols").get_json()
        rows = {item["exchange"]: item for item in data["events"] if item["symbol"] == "ESPORTS/USDT"}
        self.assertEqual(rows["Bitget"]["delisting_status"], "delisted")
        self.assertEqual(rows["Gate"]["delisting_status"], "scheduled")
        self.assertEqual(rows["Bybit"]["delisting_status"], "unconfirmed")
        self.assertIsNone(rows["Bybit"]["effective_at_iso"])
        self.assertIn("ESPORTS/USDT", data["symbols"])
        self.assertIn("+08:00", data["updated_at"])

    def test_multiplier_and_verified_alias_search_reach_the_same_evidence(self):
        aliases = [SimpleNamespace(canonical_base="ESPORTS", alias_base="ESPORTSALIAS", canonical_symbol="ESPORTS/USDT", alias_symbol="ESPORTSALIAS/USDT", exchange="ANY", market_type="contract", multiplier=1)]
        with patch.object(module, "symbol_alias_rows", return_value=aliases):
            self.assertEqual(self.get(range="all", symbol="ESPORTSALIAS")["total"], 3)
            data = self.client.get("/api/market-metadata/delisted-symbols").get_json()
            self.assertIn("ESPORTSALIAS/USDT", data["symbols"])
            self.assertTrue(any("ESPORTSALIAS/USDT" in item["matched_symbols"] for item in data["events"]))
        self.assertEqual(self.get(range="all", symbol="1000SATS")["total"], 1)
        self.assertEqual(self.get(range="all", symbol="SATSSTOCK")["total"], 0)

    def test_known_times_are_compared_in_utc_plus_8_including_exact_boundary(self):
        item = self.event("TEST/USDT", "Bitget", effective=datetime(2026, 10, 18, 15))
        for utc, expected in ((datetime(2026, 10, 18, 6, 59, tzinfo=timezone.utc), "scheduled"), (datetime(2026, 10, 18, 7, tzinfo=timezone.utc), "delisted")):
            self.assertEqual(module.listing_event_payload(item, now=utc, aliases=[])["delisting_status"], expected)

    def test_unparsed_or_unofficial_records_never_claim_execution(self):
        item = self.event("TEST/USDT", "Bitget", effective=self.now - timedelta(days=2), status="retry")
        self.assertEqual(module.listing_event_payload(item, aliases=[])["delisting_status"], "unconfirmed")
        item.announcement = False
        self.assertEqual(module.listing_event_payload(item, aliases=[])["delisting_status"], "detected")

    def test_source_urls_cannot_execute_script_or_spoof_an_official_host(self):
        for value in ("javascript:alert(1)", "https://bitget.com.evil.test/x", "https://bitget.com@evil.test/x", "http://www.bitget.com/x", "https://evil.test/x"):
            self.assertIsNone(module.safe_listing_source_url(value))
        self.assertEqual(module.safe_listing_source_url("https://www.bitget.com/support/x"), "https://www.bitget.com/support/x")

    def test_gets_do_not_fetch_remote_announcements_or_change_history(self):
        with module.app.app_context():
            before = module.ListingEvent.query.count()
        with patch.object(module, "fetch_announcement_page", side_effect=AssertionError("GET must not fetch upstream")):
            self.get(range="all")
            self.client.get("/api/market-metadata/delisted-symbols")
        with module.app.app_context():
            self.assertEqual(module.ListingEvent.query.count(), before)

    def test_metadata_detection_is_not_labeled_as_official_announcement(self):
        row = self.get(range="all", symbol="METADATA")["events"][0]
        self.assertEqual(row["source_kind"], "detected")
        self.assertEqual(row["delisting_status"], "detected")
        self.assertIsNone(row["source_url"])

    def test_both_read_endpoints_require_login_and_disable_browser_cache(self):
        client = module.app.test_client()
        for path in ("/api/daily-report/listings?range=all", "/api/market-metadata/delisted-symbols"):
            self.assertEqual(client.get(path).status_code, 401)
            self.assertIn("no-store", client.get(path).headers["Cache-Control"])
            self.assertIn("no-store", self.client.get(path).headers["Cache-Control"])


if __name__ == "__main__":
    unittest.main()

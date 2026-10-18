import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "alert-history-test")
os.environ.setdefault("CHAT_ENCRYPTION_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")

import app as module


class AlertHistoryTests(unittest.TestCase):
    def setUp(self):
        module.app.config.update(TESTING=True)
        self.now = datetime.now().replace(microsecond=0)
        fixed_now = self.now

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now.replace(tzinfo=timezone.utc).astimezone(tz) if tz else fixed_now

        self.clock = patch.object(module, "datetime", Clock)
        self.clock.start()
        self.rwa = patch.object(module, "is_rwa_stock_pair", return_value=False)
        self.rwa.start()
        with module.app.app_context():
            module.db.drop_all()
            module.db.create_all()
            owner = module.UserAccount(username="alert-test", password_hash="unused", role="admin", account_level="lv3", active_session_hash=module.session_digest("alert-test-token"))
            module.db.session.add(owner)
            module.db.session.commit()
            self.user_id = owner.id
        self.client = module.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
            session["auth_token"] = "alert-test-token"

    def tearDown(self):
        with module.app.app_context():
            module.db.session.remove()
            module.db.drop_all()
        self.clock.stop()
        self.rwa.stop()

    def event(self, symbol="DEMO/USDT", **values):
        return module.AlertEvent(**dict(dict(symbol=symbol, strategy="spot_futures", alert_type="rapid_basis", message="确认后的合成报警", open_spread=0.8, basis=1.2, created_at=self.now - timedelta(minutes=3)), **values))

    def insert(self, events):
        with module.app.app_context():
            module.db.session.add_all(events)
            module.db.session.commit()

    def get(self, **params):
        response = self.client.get("/api/alerts", query_string=params)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_all_24h_symbols_survive_more_than_80_coins_and_800_events(self):
        self.insert([self.event("BUSY/USDT", created_at=self.now - timedelta(seconds=i + 1)) for i in range(900)] + [self.event(f"COIN{i}/USDT", created_at=self.now - timedelta(hours=20)) for i in range(168)])
        data = self.get(view="summary")
        self.assertEqual((data["total_symbols"], data["total_alerts"]), (169, 1068))
        self.assertEqual(len(data["events"]), 169)
        self.assertIn("COIN167/USDT", {item["symbol"] for item in data["events"]})
        self.assertEqual(data["events"][0]["alert_count"], 900)

    def test_window_is_exactly_24_hours_and_does_not_include_future_events(self):
        self.insert([self.event("BOUNDARY/USDT", created_at=self.now - timedelta(hours=24)), self.event("OLD/USDT", created_at=self.now - timedelta(hours=24, seconds=1)), self.event("FUTURE/USDT", created_at=self.now + timedelta(seconds=1))])
        data = self.get(view="summary")
        self.assertEqual([item["symbol"] for item in data["events"]], ["BOUNDARY/USDT"])
        self.assertEqual(data["window_hours"], 24)

    def test_unconfirmed_legacy_and_rwa_alerts_remain_excluded(self):
        self.insert([self.event("RAW/USDT", alert_type="spot_pump", message="旧版未确认"), self.event("CONFIRMED/USDT", alert_type="spot_pump"), self.event("BASIS/USDT", alert_type="basis_threshold", message="基差越线"), self.event("STOCK/USDT")])
        with patch.object(module, "is_rwa_stock_pair", side_effect=lambda symbol: symbol == "STOCK/USDT"):
            self.assertEqual({item["symbol"] for item in self.get(view="summary")["events"]}, {"CONFIRMED/USDT", "BASIS/USDT"})

    def test_summary_has_signed_absolute_peaks_from_all_confirmed_events_and_true_first_time(self):
        self.insert([self.event(open_spread=1, basis=2), self.event(alert_type="spot_pump", open_spread=-12, basis=-8, created_at=self.now - timedelta(hours=20))] + [self.event(created_at=self.now - timedelta(minutes=i+4)) for i in range(25)])
        group = self.get(view="summary")["events"][0]
        self.assertEqual(group["alert_count"], 27)
        self.assertEqual((group["move_peaks"]["open_spread"], group["move_peaks"]["basis"]), (-12, -8))
        self.assertEqual(group["first_at"], (self.now - timedelta(hours=20)).strftime("%m-%d %H:%M:%S"))
        self.assertEqual(group["timeline"], [])
        self.assertEqual(len(self.get()["events"][0]["timeline"]), 20)

    def test_selected_coin_details_paginate_without_a_hidden_20_record_limit(self):
        self.insert([self.event(created_at=self.now - timedelta(seconds=i+1)) for i in range(123)] + [self.event("OTHER/USDT")])
        pages = [self.get(view="detail", symbol="DEMO/USDT", page=page, page_size=40) for page in range(1, 5)]
        self.assertEqual([len(page["items"]) for page in pages], [40, 40, 40, 3])
        self.assertEqual(pages[0]["total"], 123)
        rows = [item for page in pages for item in page["items"]]
        self.assertEqual(len({row["id"] for row in rows}), 123)
        self.assertTrue(all(row["symbol"] == "DEMO/USDT" for row in rows))
        self.assertTrue(all(rows[i]["created_at_epoch"] >= rows[i+1]["created_at_epoch"] for i in range(len(rows)-1)))

    def test_ranges_use_all_24h_confirmed_events_not_the_current_page_or_legacy_timeline(self):
        self.insert([self.event(created_at=self.now - timedelta(minutes=i + 1)) for i in range(90)] + [
            self.event(open_spread=10, basis=4, created_at=self.now - timedelta(hours=23)),
            self.event(open_spread=-50, basis=-8, created_at=self.now - timedelta(hours=24)),
            self.event(open_spread=999, basis=-999, created_at=self.now - timedelta(hours=24, seconds=1)),
            self.event(open_spread=888, basis=-888, created_at=self.now + timedelta(seconds=1)),
            self.event(alert_type="spot_pump", message="旧版未确认", open_spread=777, basis=-777),
        ])
        for params in ({"view": "summary"}, {}):
            group = self.get(**params)["events"][0]
            self.assertEqual(group["alert_count"], 92)
            self.assertEqual(group["value_ranges"], {"open_spread": {"max": 10, "min": -50}, "basis": {"max": 4, "min": -8}})
            self.assertEqual((group["move_peaks"]["open_spread"], group["move_peaks"]["basis"]), (-50, -8))
        self.assertTrue(all(item["open_spread"] == 0.8 for item in self.get(view="detail", symbol="DEMO/USDT")["items"]))

    def test_ranges_preserve_one_sided_zero_equal_and_missing_values(self):
        self.insert([
            self.event("POSITIVE/USDT", open_spread=10, basis=2), self.event("POSITIVE/USDT", open_spread=3, basis=1),
            self.event("NEGATIVE/USDT", open_spread=-2, basis=-3), self.event("NEGATIVE/USDT", open_spread=-50, basis=-8),
            self.event("ZERO/USDT", open_spread=0, basis=0),
            self.event("MISSING/USDT", open_spread=None, basis=None),
            self.event("PARTIAL/USDT", open_spread=None, basis=2), self.event("PARTIAL/USDT", open_spread=4, basis=None),
        ])
        groups = {group["symbol"]: group["value_ranges"] for group in self.get(view="summary")["events"]}
        self.assertEqual(groups["POSITIVE/USDT"], {"open_spread": {"max": 10, "min": 3}, "basis": {"max": 2, "min": 1}})
        self.assertEqual(groups["NEGATIVE/USDT"], {"open_spread": {"max": -2, "min": -50}, "basis": {"max": -3, "min": -8}})
        self.assertEqual(groups["ZERO/USDT"], {field: {"max": 0, "min": 0} for field in ("open_spread", "basis")})
        self.assertEqual(groups["MISSING/USDT"], {field: {"max": None, "min": None} for field in ("open_spread", "basis")})
        self.assertEqual(groups["PARTIAL/USDT"], {"open_spread": {"max": 4, "min": 4}, "basis": {"max": 2, "min": 2}})

    def test_ranges_ignore_nonfinite_values_without_inventing_zero(self):
        self.insert([self.event(open_spread=float("inf"), basis=float("-inf")), self.event(open_spread=3, basis=None)])
        self.assertEqual(self.get(view="summary")["events"][0]["value_ranges"], {"open_spread": {"max": 3, "min": 3}, "basis": {"max": None, "min": None}})

    def test_tied_timestamps_have_stable_id_order_and_page_bounds(self):
        self.insert([self.event() for _ in range(4)])
        data = self.get(view="detail", symbol="DEMO/USDT", page_size=2)
        following = self.get(view="detail", symbol="DEMO/USDT", page=2, page_size=2)
        ids = [item["id"] for item in data["items"] + following["items"]]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(self.get(view="detail", symbol="DEMO/USDT", page=999, page_size=2)["page"], 2)
        self.assertEqual(self.get(view="detail", symbol="DEMO/USDT", page_size=999)["page_size"], 100)

    def test_unicode_coin_and_invalid_parameters(self):
        self.insert([self.event("哈基米/USDT")])
        self.assertEqual(self.get(view="detail", symbol="哈基米/USDT")["total"], 1)
        for params in ({"view":"bad"}, {"view":"detail","symbol":"' OR 1=1"}, {"view":"detail","symbol":"DEMO/USDT","page":"bad"}, {"view":"detail"}):
            self.assertEqual(self.client.get("/api/alerts", query_string=params).status_code, 400)
        empty = self.get(view="detail", symbol="NONE/USDT")
        self.assertEqual((empty["items"], empty["total"], empty["pages"], empty["page"]), ([], 0, 1, 1))

    def test_active_events_keep_120_second_lifetime_and_existing_latest_per_coin_rule(self):
        self.insert([self.event("LIVE/USDT", created_at=self.now - timedelta(seconds=120)), self.event("EXPIRED/USDT", created_at=self.now - timedelta(seconds=121)), self.event("LIVE/USDT", created_at=self.now - timedelta(hours=1))])
        data = self.get(view="summary")
        self.assertEqual([item["symbol"] for item in data["active_events"]], ["LIVE/USDT"])
        self.assertEqual(data["active_events"][0]["remaining_seconds"], 0)

    def test_reads_require_login_are_uncached_and_do_not_write_or_fetch_upstream(self):
        self.insert([self.event()])
        anonymous = module.app.test_client()
        for path in ("/api/alerts?view=summary", "/api/alerts?view=detail&symbol=DEMO/USDT"):
            self.assertEqual(anonymous.get(path).status_code, 401)
            self.assertIn("no-store", anonymous.get(path).headers["Cache-Control"])
            with patch.object(module, "urlopen", side_effect=AssertionError("read-only must not fetch upstream")):
                self.assertIn("no-store", self.client.get(path).headers["Cache-Control"])
        with module.app.app_context():
            self.assertEqual(module.AlertEvent.query.count(), 1)


if __name__ == "__main__":
    unittest.main()

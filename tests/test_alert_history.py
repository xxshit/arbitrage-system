import os
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "alert-history-test")
os.environ.setdefault("CHAT_ENCRYPTION_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")

import app as module
from anomaly_engine import PulseDetector


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

    def test_basis_range_only_uses_binance_contract_samples_in_either_leg(self):
        self.insert([
            self.event(long_exchange="Gate", short_exchange="Binance", basis=1.4),
            self.event(long_exchange="Bitget", short_exchange="Binance", basis=-2.6),
            self.event(strategy="futures_futures", long_exchange="Binance", short_exchange="Bybit", basis=3),
            self.event(strategy="futures_futures", long_exchange="OKX", short_exchange="Binance", basis=-4),
            self.event(strategy="futures_futures", long_exchange="Bybit", short_exchange="OKX", basis=999, open_spread=20),
            self.event(long_exchange="Binance", short_exchange="Bybit", basis=-999),
            self.event(strategy="unrecognized", short_exchange="Binance", basis=888),
        ])
        for params in ({"view": "summary"}, {}):
            with patch.object(module, "get_json", side_effect=AssertionError("ranges must be historical and DB-only")):
                group = self.get(**params)["events"][0]
            self.assertEqual(group["basis_exchange"], "Binance")
            self.assertEqual(group["value_ranges"]["basis"], {"max": 3, "min": -4})
            self.assertEqual(group["value_ranges"]["open_spread"], {"max": 20, "min": 0.8})
            self.assertEqual(group["alert_count"], 7)

    def test_nonbinance_legacy_zero_does_not_widen_one_sided_binance_range(self):
        self.insert([
            self.event(long_exchange="Gate", short_exchange="Binance", basis=2),
            self.event(strategy="futures_futures", long_exchange="Bybit", short_exchange="OKX", basis=0),
            self.event("NEGATIVE/USDT", short_exchange="Binance", basis=-2),
            self.event("NEGATIVE/USDT", strategy="futures_futures", long_exchange="Bybit", short_exchange="OKX", basis=0),
            self.event("NO-BINANCE/USDT", strategy="futures_futures", long_exchange="Bybit", short_exchange="OKX", basis=0),
        ])
        groups = {group["symbol"]: group for group in self.get(view="summary")["events"]}
        self.assertEqual(groups["DEMO/USDT"]["value_ranges"]["basis"], {"max": 2, "min": 2})
        self.assertEqual(groups["NEGATIVE/USDT"]["value_ranges"]["basis"], {"max": -2, "min": -2})
        self.assertEqual(groups["NO-BINANCE/USDT"]["value_ranges"]["basis"], {"max": None, "min": None})

    def test_legacy_spot_binance_basis_and_genuine_zero_remain_valid(self):
        self.insert([self.event(basis=1.5), self.event(short_exchange="Binance", basis=0)])
        self.assertEqual(self.get(view="summary")["events"][0]["value_ranges"]["basis"], {"max": 1.5, "min": 0})

    def test_new_dual_spread_alerts_preserve_missing_binance_basis_as_null(self):
        cases = [
            ("NO-BINANCE/USDT", "Bybit", "OKX", 99, -99, None),
            ("MISSING-SHORT/USDT", "Bybit", "Binance", 99, None, None),
            ("MISSING-LONG/USDT", "Binance", "Bybit", None, -99, None),
            ("REAL-ZERO/USDT", "Bybit", "Binance", 99, 0, 0),
        ]
        with module.app.app_context():
            engine = PulseDetector()
            for symbol, long, short, _long_basis, _short_basis, expected in cases:
                path = ("futures_futures", symbol, long, short)
                context = {"open_spread": 0, "close_spread": .1, "mid_spread": .05, "basis": expected, "funding_rate": .01}
                epoch = self.now.replace(tzinfo=timezone.utc).timestamp()
                engine.observe(path, "spread", 0, epoch-2, context)
                module.persist_alert_pulses(engine.observe(path, "spread", .8, epoch-1, {**context, "open_spread": .8, "close_spread": .9, "mid_spread": .85}))
            events = {event.symbol: event for event in module.AlertEvent.query.all()}
            self.assertEqual(len(events), len(cases))
            for symbol, _long, _short, _long_basis, _short_basis, expected in cases:
                with self.subTest(symbol=symbol):
                    self.assertEqual(events[symbol].alert_type, "rapid_spread")
                    self.assertEqual(events[symbol].basis, expected)
                    self.assertEqual(events[symbol].open_spread, 0.8)

    def test_pulse_peak_and_retrace_survive_summary_detail_and_retry(self):
        engine = PulseDetector()
        updates = {}
        path = ("spot_futures", "DEMO/USDT", "Gate", "Binance")
        for i, value in enumerate((0, .55, 1.02, .1, .65)):
            context = {"open_spread": value, "close_spread": value+.02, "mid_spread": value+.01, "basis": -.32, "funding_rate": None}
            for pulse in engine.observe(path, "spread", value, self.now.replace(tzinfo=timezone.utc).timestamp()-10+i, context):
                updates[pulse["id"]] = pulse
        with module.app.app_context():
            module.persist_alert_pulses(list(updates.values()))
            module.persist_alert_pulses(list(updates.values()))
        data = self.get(view="summary")
        coin = data["events"][0]
        self.assertEqual(coin["alert_count"], 1)
        self.assertEqual(coin["observation_count"], 2)
        self.assertEqual(coin["move_peaks"]["open_spread"], 1.02)
        self.assertEqual(coin["paths"][0]["value_ranges"]["open_spread"], {"max": 1.02, "min": .1})
        self.assertEqual(data["active_events"][0]["pulse"]["ordinal"], 2)
        self.assertTrue(data["active_events"][0]["notice_key"].endswith(":restart:start"))
        detail = self.get(view="detail", symbol="DEMO/USDT")["items"]
        self.assertEqual(detail[1]["pulse"]["state"], "retraced")
        self.assertEqual(detail[1]["pulse"]["peak"], 1.02)
        self.assertNotEqual(detail[0]["notice_key"], detail[1]["notice_key"])
        self.assertEqual(detail[0]["pulse"]["baseline"], .1)
        self.assertNotIn("trigger_context", detail[0]["pulse"])

    def test_tied_timestamps_have_stable_id_order_and_page_bounds(self):
        self.insert([self.event() for _ in range(4)])
        data = self.get(view="detail", symbol="DEMO/USDT", page_size=2)
        following = self.get(view="detail", symbol="DEMO/USDT", page=2, page_size=2)
        ids = [item["id"] for item in data["items"] + following["items"]]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(self.get(view="detail", symbol="DEMO/USDT", page=999, page_size=2)["page"], 2)
        self.assertEqual(self.get(view="detail", symbol="DEMO/USDT", page_size=999)["page_size"], 100)

    def test_old_noise_is_excluded_consistently_without_deleting_evidence(self):
        from tests.test_alert_quality import historical_pulse
        epoch = self.now.replace(tzinfo=timezone.utc).timestamp()-10
        pulses = [historical_pulse(identifier="noise"), historical_pulse(trigger=.2, peak=1.02, current=.1, identifier="spike")]
        for index, pulse in enumerate(pulses):
            pulse["path"] = ("spot_futures", "NOISE/USDT" if index == 0 else "REAL/USDT", "Gate", "Binance")
            for key in ("baseline_at", "triggered_at", "peak_at", "current_at", "noticed_at", "retrace_at"):
                pulse[key] += epoch-1000
        with module.app.app_context():
            module.persist_alert_pulses(pulses)
        data = self.get(view="summary")
        self.assertEqual([item["symbol"] for item in data["events"]], ["REAL/USDT"])
        self.assertEqual([item["symbol"] for item in data["active_events"]], ["REAL/USDT"])
        self.assertEqual(self.get(view="detail", symbol="NOISE/USDT")["total"], 0)
        self.assertEqual(self.get(view="detail", symbol="REAL/USDT")["items"][0]["pulse"]["peak"], 1.02)
        with module.app.app_context():
            self.assertEqual(module.AlertPulse.query.count(), 2)
            self.assertEqual(module.AlertEvent.query.count(), 2)

    def test_synchronous_path_copies_count_once_without_combining_ranges(self):
        from tests.test_alert_quality import historical_pulse
        epoch = self.now.replace(tzinfo=timezone.utc).timestamp()-10
        pulses = [historical_pulse(trigger=.6, peak=.7, identifier="gate"), historical_pulse(trigger=1.1, peak=1.3, identifier="bitget")]
        for index, pulse in enumerate(pulses):
            pulse["path"] = ("spot_futures", "DEMO/USDT", "Gate" if index == 0 else "Bitget", "Binance")
            for key in ("baseline_at", "triggered_at", "peak_at", "current_at", "noticed_at", "retrace_at"):
                pulse[key] += epoch-1000+index
        with module.app.app_context():
            module.persist_alert_pulses(pulses)
        summary = self.get(view="summary")
        group = summary["events"][0]
        self.assertEqual((summary["total_alerts"], group["alert_count"], group["observation_count"]), (1, 1, 2))
        paths = {item["long_exchange"]: item for item in group["paths"]}
        self.assertEqual(paths["Gate"]["value_ranges"]["open_spread"]["max"], .7)
        self.assertEqual(paths["Bitget"]["value_ranges"]["open_spread"]["max"], 1.3)
        self.assertEqual(paths["Gate"]["latest"]["notice_key"], paths["Bitget"]["latest"]["notice_key"])
        self.assertEqual(self.get(view="detail", symbol="DEMO/USDT")["total"], 2)

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

    def test_summary_has_current_wallet_status_for_all_distinct_path_exchanges(self):
        self.insert([self.event(message="Gate 确认后的报警"), self.event(message="Bitget 确认后的报警"), self.event(strategy="futures_futures", long_exchange="Bybit", short_exchange="Binance")])
        with module.app.app_context():
            module.db.session.add(module.TransferNetworkSnapshot(exchange="Gate", symbol="DEMO/USDT", chains_json=json.dumps([{"name": "BSC", "deposit_open": False, "withdraw_open": True}]), captured_at=self.now))
            module.db.session.commit()
        with patch.object(module, "get_json", side_effect=AssertionError("summary must be DB-only")):
            group = self.get(view="summary")["events"][0]
        statuses = {item["exchange"]: item for item in group["transfer_statuses"]}
        self.assertEqual(set(statuses), {"Gate", "Bitget", "Bybit", "Binance"})
        self.assertEqual(statuses["Gate"]["state"], "fresh")
        self.assertIs(statuses["Gate"]["chains"][0]["deposit_open"], False)
        self.assertEqual(statuses["Binance"]["state"], "unknown")
        self.assertNotIn("transfer_statuses", self.get(view="detail", symbol="DEMO/USDT")["items"][0])

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

    def test_paths_separate_strategy_exchange_and_direction_without_changing_coin_totals(self):
        self.insert([
            self.event(long_exchange="Gate", short_exchange="Binance", open_spread=-50, basis=-2),
            self.event(long_exchange="Bitget", short_exchange="Binance", open_spread=2, basis=1),
            self.event(strategy="futures_futures", long_exchange="Bybit", short_exchange="Binance", open_spread=3, basis=4),
            self.event(strategy="futures_futures", long_exchange="Binance", short_exchange="Bybit", open_spread=5, basis=6),
        ])
        group = self.get(view="summary")["events"][0]
        self.assertEqual(group["alert_count"], 4)
        self.assertEqual(group["move_peaks"]["open_spread"], -50)
        paths = {tuple(json.loads(path["id"])): path for path in group["paths"]}
        self.assertEqual(len(paths), 4)
        self.assertEqual(sum(path["alert_count"] for path in paths.values()), 4)
        for key, spread in [(('spot_futures', 'Gate', 'Binance'), -50), (('spot_futures', 'Bitget', 'Binance'), 2), (('futures_futures', 'Bybit', 'Binance'), 3), (('futures_futures', 'Binance', 'Bybit'), 5)]:
            self.assertEqual(paths[key]["value_ranges"]["open_spread"], {"max": spread, "min": spread})
            self.assertEqual(paths[key]["latest"]["path_id"], paths[key]["id"])

    def test_only_strategies_with_actual_confirmed_samples_have_paths(self):
        self.insert([self.event("SPOT/USDT", long_exchange="Gate"), self.event("DUAL/USDT", strategy="futures_futures", long_exchange="Bybit", short_exchange="OKX"), self.event("SPOT/USDT", strategy="futures_futures", alert_type="futures_pump", message="未确认旧记录")])
        groups = {group["symbol"]: group for group in self.get(view="summary")["events"]}
        self.assertEqual([path["strategy"] for path in groups["SPOT/USDT"]["paths"]], ["spot_futures"])
        self.assertEqual([path["strategy"] for path in groups["DUAL/USDT"]["paths"]], ["futures_futures"])

    def test_path_ranges_cover_full_window_and_filtered_pagination_excludes_other_venues(self):
        self.insert([self.event(long_exchange="Gate", short_exchange="Binance", created_at=self.now - timedelta(minutes=i+1)) for i in range(91)] + [self.event(long_exchange="Gate", short_exchange="Binance", open_spread=-12, basis=-4, created_at=self.now-timedelta(hours=24)), self.event(long_exchange="Bitget", short_exchange="Binance", open_spread=999, basis=999), self.event(long_exchange="Gate", open_spread=-999, created_at=self.now-timedelta(hours=24, seconds=1))])
        path = next(path for path in self.get(view="summary")["events"][0]["paths"] if path["long_exchange"] == "Gate")
        self.assertEqual(path["alert_count"], 92)
        self.assertEqual(path["value_ranges"]["open_spread"], {"max": 0.8, "min": -12})
        self.assertEqual(path["value_ranges"]["basis"], {"max": 1.2, "min": -4})
        pages = [self.get(view="detail", symbol="DEMO/USDT", strategy="spot_futures", path=path["id"], page=page) for page in (1, 2, 3)]
        self.assertEqual([len(page["items"]) for page in pages], [40, 40, 12])
        self.assertEqual(len({item["id"] for page in pages for item in page["items"]}), 92)
        for page in pages:
            self.assertEqual((page["strategy"], page["path_id"], page["total"]), ("spot_futures", path["id"], 92))
            self.assertTrue(all(item["path_id"] == path["id"] and item["long_exchange"] == "Gate" for item in page["items"]))

    def test_nonbinance_path_never_inherits_another_path_basis_or_legacy_zero(self):
        self.insert([self.event(long_exchange="Gate", basis=20), self.event(strategy="futures_futures", long_exchange="Bybit", short_exchange="OKX", basis=0)])
        path = next(path for path in self.get(view="summary")["events"][0]["paths"] if path["strategy"] == "futures_futures")
        self.assertIsNone(path["basis_exchange"])
        self.assertEqual(path["value_ranges"]["basis"], {"max": None, "min": None})
        event = self.get(view="detail", symbol="DEMO/USDT", path=path["id"])["items"][0]
        self.assertEqual(event["basis"], 0)  # Preserve historical storage/legacy contract.
        self.assertIsNone(event["binance_basis"])
        self.assertIsNone(event["basis_exchange"])

    def test_explicit_path_columns_win_and_legacy_prefix_requires_a_real_boundary(self):
        self.insert([self.event(long_exchange="Bitget", short_exchange="Bybit", message="Gate 现货旧说明"), self.event(message="Gate 现货与 Binance 合约：确认后的报警"), self.event(message="Gatekeeper unrelated"), self.event(strategy="futures_futures", long_exchange=None, short_exchange=None)])
        paths = {tuple(json.loads(path["id"])) for path in self.get(view="summary")["events"][0]["paths"]}
        self.assertEqual(paths, {("spot_futures", "Bitget", "Bybit"), ("spot_futures", "Gate", "Binance"), ("spot_futures", None, "Binance"), ("futures_futures", None, None)})

    def test_path_filter_does_not_fall_back_to_all_when_the_selected_path_has_expired(self):
        self.insert([self.event(long_exchange="Gate", short_exchange="Binance")])
        result = self.get(view="detail", symbol="DEMO/USDT", path=json.dumps(["spot_futures", "Bitget", "Binance"]), page=999)
        self.assertEqual((result["items"], result["total"], result["page"], result["pages"]), ([], 0, 1, 1))

    def test_invalid_or_mismatched_path_filters_return_400_not_a_broader_history(self):
        for value in ["", "null", "{}", "[]", '["spot_futures","Gate"]', '[true,"Gate","Binance"]', '["spot_futures",{},"Binance"]', '["bad<script>","Gate","Binance"]', json.dumps(["spot_futures", "X"*31, "Binance"]), "x"*257]:
            with self.subTest(path=value):
                self.assertEqual(self.client.get("/api/alerts", query_string={"view": "detail", "symbol": "DEMO/USDT", "path": value}).status_code, 400)
        self.assertEqual(self.client.get("/api/alerts", query_string={"view":"detail", "symbol":"DEMO/USDT", "strategy":"futures_futures", "path":json.dumps(["spot_futures","Gate","Binance"])}).status_code, 400)

    def test_unknown_strategy_is_preserved_instead_of_mislabelled_as_spot_futures(self):
        self.insert([self.event(strategy="unrecognized", long_exchange="Gate", short_exchange="Binance", basis=999)])
        path = self.get(view="summary")["events"][0]["paths"][0]
        self.assertEqual(path["strategy"], "unrecognized")
        self.assertEqual(path["long_market"], "未记录")
        self.assertIsNone(path["basis_exchange"])

    def test_path_reads_are_authenticated_uncached_readonly_and_do_not_query_upstream(self):
        self.insert([self.event(long_exchange="Gate", short_exchange="Binance")])
        params = {"view":"detail", "symbol":"DEMO/USDT", "path":json.dumps(["spot_futures","Gate","Binance"])}
        self.assertEqual(module.app.test_client().get("/api/alerts", query_string=params).status_code, 401)
        self.get(**params)  # Complete the existing login last_seen heartbeat first.
        with patch.object(module, "get_json", side_effect=AssertionError("no upstream")), patch.object(module.db.session, "commit", side_effect=AssertionError("no writes")):
            self.assertEqual(self.get(**params)["total"], 1)
        self.assertIn("no-store", self.client.get("/api/alerts", query_string=params).headers["Cache-Control"])


if __name__ == "__main__":
    unittest.main()

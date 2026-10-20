import json
import unittest
from unittest.mock import patch

from tests import test_alert_history as fixture

module = fixture.module


class FundingIntegrationTests(unittest.TestCase):
    setUp = fixture.AlertHistoryTests.setUp
    tearDown = fixture.AlertHistoryTests.tearDown
    get = fixture.AlertHistoryTests.get

    def poll(self, offset=0, rate=.003, interval=4, **kwargs):
        at = self.now.replace(tzinfo=module.timezone.utc).timestamp() + offset
        boundary = self.now.replace(tzinfo=module.timezone.utc).timestamp() + 120
        instrument = dict(symbol="DEMOUSDT", status="TRADING", quoteAsset="USDT", contractType="PERPETUAL", underlyingType="COIN")
        instrument.update(kwargs.get("instrument", {}))
        payloads = {"exchangeInfo": {"symbols": [instrument]},
                    "premiumIndex": [{"symbol": "DEMOUSDT", "lastFundingRate": rate,
                                      "time": at * 1000, "nextFundingTime": kwargs.get("next_at", boundary) * 1000}],
                    "fundingInfo": [{"symbol": "DEMOUSDT", "adjustedFundingRateCap": .01, "adjustedFundingRateFloor": -.01, "fundingIntervalHours": interval}]}
        cache = {"https://fapi.binance.com/fapi/v1/" + key: {"payload": value, "fetched_at": 1000}
                 for key, value in payloads.items()}
        with patch.object(module, "MARKET_PAYLOAD_CACHE", cache), patch.object(module.time, "monotonic", return_value=1000 + kwargs.get("stale", 0)), patch.object(module.time, "time", return_value=at), module.app.app_context():
            return module.refresh_funding_alerts()

    def payload(self, offset=0):
        with module.app.app_context():
            return module.funding_alert_payload(self.now.replace(tzinfo=module.timezone.utc).timestamp() + offset)

    def test_initial_and_transition_survive_database_reload(self):
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.poll(5, rate=.01), 1)
        first = self.payload(5)
        self.assertEqual(len(first["groups"]), 1)
        self.assertEqual(len(first["active_events"]), 1)
        self.assertEqual(self.poll(10, rate=.01), 0)
        self.assertEqual(self.payload(10)["active_events"][0]["notice_key"], first["active_events"][0]["notice_key"])
        self.assertEqual(self.poll(15, interval=1), 1)
        group = self.payload(15)["groups"][0]
        self.assertEqual(group["change"]["from_interval"], 4)
        self.assertEqual(len(group["history"]), 2)

    def test_funding_is_present_without_any_spread_candidate_or_spot_route(self):
        self.poll()
        self.poll(5, rate=.01)
        with patch.object(module.time, "time", return_value=self.now.replace(tzinfo=module.timezone.utc).timestamp()+5):
            result = self.get(view="summary")
        self.assertEqual(result["events"], [])
        self.assertEqual(result["total_symbols"], 0)
        self.assertEqual(result["funding"]["groups"][0]["contract"], "DEMOUSDT")
        self.assertEqual(len(result["funding"]["active_events"]), 1)

    def test_current_hourly_without_change_does_not_claim_just_switched(self):
        self.poll(interval=1)
        result = self.payload()
        self.assertEqual(result["groups"][0]["status"], "hourly")
        self.assertIsNone(result["groups"][0]["change"])
        self.assertEqual(result["active_events"], [])

    def test_stale_metadata_never_refreshes_or_alerts(self):
        self.poll()
        self.poll(5, rate=.01)
        self.assertEqual(self.poll(180, interval=1, stale=180, next_at=self.now.timestamp()+500), 0)
        result = self.payload(180)
        self.assertEqual(result["groups"][0]["status"], "stale")
        self.assertEqual(result["active_events"], [])

    def test_equity_metadata_is_excluded_even_without_durable_catalog(self):
        self.assertEqual(self.poll(rate=.01, instrument={"underlyingType": "EQUITY"}), 0)
        with module.app.app_context():
            self.assertEqual(module.FundingAlertState.query.count(), 0)

    def test_read_layer_excludes_later_classified_equity(self):
        self.poll(rate=.01)
        with patch.object(module, "is_rwa_stock_pair", return_value=True):
            self.assertEqual(self.payload()["groups"], [])

    def test_exchange_contract_reminder_not_duplicated_by_spot_paths(self):
        self.poll()
        self.poll(5, rate=.01, interval=1)
        result = self.payload(5)
        self.assertEqual(len(result["active_events"]), 1)
        self.assertEqual(result["active_events"][0]["kind"], "interval_shortened")
        self.assertEqual(len(result["groups"]), 1)

    def test_failed_transaction_does_not_consume_reminder(self):
        self.poll()
        with module.app.app_context():
            with patch.object(module.db.session, "commit", side_effect=RuntimeError("synthetic rollback")):
                with self.assertRaises(RuntimeError):
                    self.poll(5, rate=.01)
            module.db.session.rollback()
            self.assertEqual(module.FundingAlertEvent.query.count(), 0)
        self.assertEqual(self.poll(5, rate=.01), 1)
        self.assertEqual(len(self.payload(5)["active_events"]), 1)

    def test_settled_history_confirmation_and_priority_use_real_database_records(self):
        self.poll(110, rate=.01)
        at = self.now.replace(tzinfo=module.timezone.utc).timestamp()
        self.poll(125, next_at=at+14520)
        with module.app.app_context():
            self.assertEqual(module.funding_alert_priority_symbols(at+125), ["DEMOUSDT"])
            module.db.session.add(module.FundingRateRecord(symbol="DEMOUSDT", funding_time=int((at+120)*1000), funding_rate=1))
            module.db.session.commit()
        self.poll(130, next_at=at+14520)
        result = self.payload(130)
        self.assertEqual(result["groups"][0]["status"], "settled_cap")
        self.assertEqual(result["groups"][0]["history"][0]["rate"], 1)
        with module.app.app_context():
            self.assertEqual(module.funding_alert_priority_symbols(at+130), [])

    def test_exact_24h_filter_retains_original_event_records(self):
        self.poll(rate=.01)
        at = self.now.replace(tzinfo=module.timezone.utc).timestamp()
        with module.app.app_context():
            for identity, stamp in (("boundary", at-86400), ("too-old", at-86401), ("future", at+1)):
                event = {"id": identity, "symbol": "DEMO/USDT", "exchange": "Binance", "contract": "DEMOUSDT", "observed_at": stamp, "notice": False}
                module.db.session.add(module.FundingAlertEvent(id=identity, contract="DEMOUSDT", observed_at_ms=int(stamp*1000), evidence_json=json.dumps(event)))
            module.db.session.commit()
        history = self.payload()["groups"][0]["history"]
        self.assertIn("boundary", {item["id"] for item in history})
        self.assertNotIn("too-old", {item["id"] for item in history})
        self.assertNotIn("future", {item["id"] for item in history})
        with module.app.app_context():
            self.assertEqual(module.FundingAlertEvent.query.count(), 4)

    def test_no_anonymous_access(self):
        self.assertEqual(module.app.test_client().get("/api/alerts?view=summary").status_code, 401)

    def test_normal_updates_are_live_without_rewriting_database_until_checkpoint(self):
        self.poll(interval=1)
        with module.app.app_context():
            initial = module.db.session.get(module.FundingAlertState, "DEMOUSDT").state_json
        self.poll(30, rate=.004, interval=1)
        with module.app.app_context():
            self.assertEqual(module.db.session.get(module.FundingAlertState, "DEMOUSDT").state_json, initial)
        self.assertAlmostEqual(self.payload(30)["groups"][0]["rate"], .4)
        self.poll(60, rate=.004, interval=1)
        with module.app.app_context():
            self.assertNotEqual(module.db.session.get(module.FundingAlertState, "DEMOUSDT").state_json, initial)

    def test_failed_commit_does_not_publish_uncommitted_live_state(self):
        self.poll(interval=1)
        before = module.FUNDING_ALERT_LIVE_STATES
        with module.app.app_context():
            with patch.object(module.db.session, "commit", side_effect=RuntimeError("failed")):
                with self.assertRaises(RuntimeError):
                    self.poll(5, rate=.01, interval=1)
            module.db.session.rollback()
        self.assertIs(module.FUNDING_ALERT_LIVE_STATES, before)
        self.assertEqual(self.poll(5, rate=.01, interval=1), 1)

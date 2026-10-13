import os
import unittest
from datetime import datetime

from werkzeug.security import generate_password_hash

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "hedge-position-test-secret"
os.environ["CHAT_ENCRYPTION_KEY"] = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

from app import HedgePosition, UserAccount, app, db, session_digest


class HedgePositionApiTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        with app.app_context():
            db.drop_all()
            db.create_all()
            owner = UserAccount(
                username="owner",
                password_hash=generate_password_hash("owner-password-123"),
                role="admin",
                account_level="lv3",
                active_session_hash=session_digest("owner-token"),
            )
            other = UserAccount(
                username="other",
                password_hash=generate_password_hash("other-password-123"),
                role="admin",
                account_level="lv3",
                active_session_hash=session_digest("other-token"),
            )
            db.session.add_all([owner, other])
            db.session.flush()
            db.session.add_all([
                HedgePosition(
                    user_id=owner.id,
                    symbol="BTC/USDT",
                    spot_exchange="Binance",
                    futures_exchange="Binance",
                    status="open",
                    spot_quantity=0.01,
                    futures_quantity=0.01,
                    spot_entry_price=60_000,
                    futures_entry_price=60_120,
                    spot_notional=600,
                    futures_notional=601.2,
                    hedge_gap_usdt=-1.2,
                    opening_spread=0.2,
                    funding_income=1.5,
                    spot_fee=0.3,
                    futures_fee=0.24,
                    opened_at=datetime(2026, 10, 13, 2, 0, 0),
                ),
                HedgePosition(
                    user_id=owner.id,
                    symbol="ETH/USDT",
                    spot_exchange="Gate",
                    futures_exchange="Binance",
                    status="closed",
                    spot_notional=500,
                    futures_notional=502,
                    hedge_gap_usdt=-2,
                    funding_income=4,
                    spot_fee=0.2,
                    futures_fee=0.25,
                    realized_pnl=3,
                    closed_at=datetime(2026, 10, 13, 3, 0, 0),
                ),
                HedgePosition(
                    user_id=other.id,
                    symbol="SOL/USDT",
                    spot_exchange="Binance",
                    futures_exchange="Binance",
                    status="open",
                    spot_notional=999,
                    futures_notional=999,
                ),
            ])
            db.session.commit()
            self.owner_id = owner.id
        self.client = app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = self.owner_id
            flask_session["auth_token"] = "owner-token"
            flask_session["csrf_token"] = "owner-csrf"

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def test_positions_are_isolated_and_split_between_current_and_history(self):
        response = self.client.get("/api/hedge-positions")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertFalse(payload["execution"]["enabled"])
        self.assertEqual(payload["execution"]["mode"], "preview_only")
        self.assertEqual([item["symbol"] for item in payload["current"]], ["BTC/USDT"])
        self.assertEqual([item["symbol"] for item in payload["history"]], ["ETH/USDT"])
        self.assertNotIn("SOL/USDT", str(payload))
        self.assertEqual(payload["summary"]["current_count"], 1)
        self.assertEqual(payload["summary"]["history_count"], 1)
        self.assertAlmostEqual(payload["summary"]["spot_notional"], 600)
        self.assertAlmostEqual(payload["summary"]["futures_notional"], 601.2)
        self.assertAlmostEqual(payload["summary"]["net_result"], 6.55)
        self.assertAlmostEqual(payload["history"][0]["net_result"], 6.55)


if __name__ == "__main__":
    unittest.main()

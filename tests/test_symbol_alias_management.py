import os
import unittest

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "symbol-alias-test-secret"
os.environ["CHAT_ENCRYPTION_KEY"] = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

from app import (
    SymbolAlias,
    UserAccount,
    app,
    canonical_market_symbol,
    db,
    normalize_contract_aliases,
    seed_symbol_aliases,
    session_digest,
)


class SymbolAliasManagementTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        with app.app_context():
            db.drop_all()
            db.create_all()
            seed_symbol_aliases()
            admin = UserAccount(
                username="alias-admin",
                password_hash="unused",
                role="admin",
                active_session_hash=session_digest("admin-token"),
            )
            viewer = UserAccount(
                username="alias-viewer",
                password_hash="unused",
                role="viewer",
                active_session_hash=session_digest("viewer-token"),
            )
            db.session.add_all([admin, viewer])
            db.session.commit()
            self.admin_id = admin.id
            self.viewer_id = viewer.id
        self.admin = app.test_client()
        self.viewer = app.test_client()
        self._login(self.admin, self.admin_id, "admin-token", "admin-csrf")
        self._login(self.viewer, self.viewer_id, "viewer-token", "viewer-csrf")

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()

    @staticmethod
    def _login(client, user_id, token, csrf):
        with client.session_transaction() as session:
            session["user_id"] = user_id
            session["auth_token"] = token
            session["csrf_token"] = csrf

    def test_seeded_aliases_include_verified_user_pairs(self):
        response = self.viewer.get("/api/symbol-aliases")
        self.assertEqual(response.status_code, 200)
        items = response.get_json()["items"]
        pairs = {(item["canonical_base"], item["alias_base"]): item for item in items}

        self.assertIn(("PLAY", "PLAYSOUT"), pairs)
        self.assertIn(("SKYAI", "SKYAI1"), pairs)
        self.assertTrue(pairs[("PLAY", "PLAYSOUT")]["seeded"])
        self.assertEqual(pairs[("SKYAI", "SKYAI1")]["multiplier"], 1)
        self.assertFalse(response.get_json()["is_admin"])

    def test_aliases_normalize_cross_exchange_contracts_and_multiplier_prices(self):
        with app.app_context():
            rows = SymbolAlias.query.filter_by(verified=True).all()
            bybit = normalize_contract_aliases(
                {
                    "PLAYSOUTUSDT": {"bid": 0.0347, "ask": 0.0348},
                    "SKYAI1USDT": {"bid": 0.0828, "ask": 0.0829},
                    "1000XECUSDT": {"bid": 0.025, "ask": 0.026},
                },
                "Bybit",
                rows,
            )

        self.assertIn("PLAYUSDT", bybit)
        self.assertIn("SKYAIUSDT", bybit)
        self.assertEqual(bybit["PLAYUSDT"]["source_symbol"], "PLAYSOUTUSDT")
        self.assertAlmostEqual(bybit["XECUSDT"]["bid"], 0.000025)

    def test_admin_can_add_and_delete_manual_alias_while_viewer_is_read_only(self):
        payload = {
            "canonical_base": "TEST",
            "alias_base": "TEST2",
            "exchange": "Bybit",
            "market_type": "contract",
            "multiplier": 1,
            "note": "官网和合约地址一致",
        }
        denied = self.viewer.post(
            "/api/symbol-aliases",
            json=payload,
            headers={"X-CSRF-Token": "viewer-csrf"},
        )
        self.assertEqual(denied.status_code, 403)

        created = self.admin.post(
            "/api/symbol-aliases",
            json=payload,
            headers={"X-CSRF-Token": "admin-csrf"},
        )
        self.assertEqual(created.status_code, 200)
        item = created.get_json()["item"]
        self.assertFalse(item["seeded"])
        with app.app_context():
            self.assertEqual(
                canonical_market_symbol("TEST2USDT", "Bybit", "contract"),
                "TEST/USDT",
            )

        deleted = self.admin.delete(
            f"/api/symbol-aliases/{item['id']}",
            headers={"X-CSRF-Token": "admin-csrf"},
        )
        self.assertEqual(deleted.status_code, 200)

    def test_conflicting_alias_and_seed_deletion_are_rejected(self):
        play = next(
            item
            for item in self.admin.get("/api/symbol-aliases").get_json()["items"]
            if item["alias_base"] == "PLAYSOUT"
        )
        protected = self.admin.delete(
            f"/api/symbol-aliases/{play['id']}",
            headers={"X-CSRF-Token": "admin-csrf"},
        )
        self.assertEqual(protected.status_code, 400)

        conflict = self.admin.post(
            "/api/symbol-aliases",
            json={
                "canonical_base": "OTHER",
                "alias_base": "PLAYSOUT",
                "exchange": "Bybit",
                "market_type": "contract",
                "multiplier": 1,
            },
            headers={"X-CSRF-Token": "admin-csrf"},
        )
        self.assertEqual(conflict.status_code, 409)


if __name__ == "__main__":
    unittest.main()

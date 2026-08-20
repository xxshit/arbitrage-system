import base64
import os
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from werkzeug.security import generate_password_hash

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "device-security-test-secret"
os.environ["CHAT_ENCRYPTION_KEY"] = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

from app import (
    AccountDevice,
    LoginSecurityAlert,
    UserAccount,
    app,
    db,
    session_digest,
)


def b64url(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class DeviceSecurityTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        with app.app_context():
            db.drop_all()
            db.create_all()
            admin = UserAccount(
                username="owner",
                password_hash=generate_password_hash("owner-password-123"),
                role="admin",
                active_session_hash=session_digest("owner-token"),
            )
            viewer = UserAccount(
                username="viewer",
                password_hash=generate_password_hash("viewer-password-123"),
                role="viewer",
            )
            db.session.add_all([admin, viewer])
            db.session.commit()
            self.admin_id = admin.id
            self.viewer_id = viewer.id
        self.admin = app.test_client()
        with self.admin.session_transaction() as flask_session:
            flask_session["user_id"] = self.admin_id
            flask_session["auth_token"] = "owner-token"
            flask_session["csrf_token"] = "owner-csrf"

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()

    def login_with_key(self, client, key, kind="desktop", password="viewer-password-123"):
        challenge_response = client.post("/api/auth/device/challenge", json={"username": "viewer"})
        self.assertEqual(challenge_response.status_code, 200)
        challenge = base64.urlsafe_b64decode(challenge_response.get_json()["challenge"] + "==")
        der_signature = key.sign(challenge, ec.ECDSA(hashes.SHA256()))
        r_value, s_value = decode_dss_signature(der_signature)
        raw_signature = r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
        public_key = key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        mobile = kind == "mobile"
        return client.post(
            "/api/auth/login",
            json={
                "username": "viewer",
                "password": password,
                "device_kind": kind,
                "touch_points": 5 if mobile else 0,
                "device_label": "Safari · 手机或平板" if mobile else "Chrome · Mac",
                "device_public_key": b64url(public_key),
                "device_signature": b64url(raw_signature),
            },
            headers={"User-Agent": "Mozilla/5.0 (iPhone) Mobile Safari" if mobile else "Mozilla/5.0 (Macintosh) Chrome"},
        )

    def test_one_desktop_and_one_mobile_can_stay_logged_in(self):
        desktop = app.test_client()
        mobile = app.test_client()
        desktop_response = self.login_with_key(desktop, ec.generate_private_key(ec.SECP256R1()))
        mobile_response = self.login_with_key(mobile, ec.generate_private_key(ec.SECP256R1()), "mobile")
        self.assertEqual(desktop_response.status_code, 200)
        self.assertEqual(mobile_response.status_code, 200)
        self.assertEqual(desktop.get("/api/auth/me").status_code, 200)
        self.assertEqual(mobile.get("/api/auth/me").status_code, 200)
        with app.app_context():
            devices = AccountDevice.query.filter_by(user_id=self.viewer_id).all()
            self.assertEqual({row.device_kind for row in devices}, {"desktop", "mobile"})

    def test_second_desktop_is_blocked_and_reported_after_correct_password(self):
        first = app.test_client()
        blocked = app.test_client()
        self.assertEqual(self.login_with_key(first, ec.generate_private_key(ec.SECP256R1())).status_code, 200)
        response = self.login_with_key(blocked, ec.generate_private_key(ec.SECP256R1()))
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["device_blocked"])
        self.assertIn("报告管理员", response.get_json()["error"])
        self.assertEqual(first.get("/api/auth/me").status_code, 200)
        alerts = self.admin.get("/api/admin/security-alerts").get_json()
        self.assertEqual(alerts["unread"], 1)
        self.assertEqual(alerts["items"][0]["username"], "viewer")
        self.assertTrue(alerts["items"][0]["attempted_device_id"].startswith("PC-"))

    def test_wrong_password_does_not_create_a_device_report(self):
        client = app.test_client()
        response = self.login_with_key(client, ec.generate_private_key(ec.SECP256R1()), password="wrong-password")
        self.assertEqual(response.status_code, 401)
        with app.app_context():
            self.assertEqual(LoginSecurityAlert.query.count(), 0)

    def test_device_challenge_is_single_use(self):
        client = app.test_client()
        key = ec.generate_private_key(ec.SECP256R1())
        first = self.login_with_key(client, key)
        self.assertEqual(first.status_code, 200)
        payload = {
            "username": "viewer",
            "password": "viewer-password-123",
            "device_kind": "desktop",
            "touch_points": 0,
            "device_label": "Chrome · Mac",
            "device_public_key": b64url(key.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )),
            "device_signature": "AA",
        }
        replay = client.post("/api/auth/login", json=payload)
        self.assertEqual(replay.status_code, 400)
        self.assertIn("过期", replay.get_json()["error"])

    def test_device_challenge_cannot_be_used_for_another_username(self):
        client = app.test_client()
        challenge_response = client.post("/api/auth/device/challenge", json={"username": "owner"})
        self.assertEqual(challenge_response.status_code, 200)
        response = client.post(
            "/api/auth/login",
            json={
                "username": "viewer",
                "password": "viewer-password-123",
                "device_kind": "desktop",
                "touch_points": 0,
                "device_label": "Chrome · Mac",
                "device_public_key": b64url(ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
                    serialization.Encoding.X962,
                    serialization.PublicFormat.UncompressedPoint,
                )),
                "device_signature": "AA",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("过期", response.get_json()["error"])

    def test_admin_can_see_and_unbind_device_then_allow_replacement(self):
        original = app.test_client()
        replacement = app.test_client()
        self.assertEqual(self.login_with_key(original, ec.generate_private_key(ec.SECP256R1())).status_code, 200)
        users = self.admin.get("/api/admin/users").get_json()["items"]
        viewer = next(item for item in users if item["id"] == self.viewer_id)
        self.assertEqual(len(viewer["devices"]), 1)
        self.assertTrue(viewer["devices"][0]["display_id"].startswith("PC-"))
        response = self.admin.delete(
            f"/api/admin/users/{self.viewer_id}/devices/desktop",
            headers={"X-CSRF-Token": "owner-csrf"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(original.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.login_with_key(replacement, ec.generate_private_key(ec.SECP256R1())).status_code, 200)

    def test_opening_security_notifications_can_mark_reports_read(self):
        first = app.test_client()
        blocked = app.test_client()
        self.login_with_key(first, ec.generate_private_key(ec.SECP256R1()))
        self.login_with_key(blocked, ec.generate_private_key(ec.SECP256R1()))
        response = self.admin.post(
            "/api/admin/security-alerts/read",
            headers={"X-CSRF-Token": "owner-csrf"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["updated"], 1)
        self.assertEqual(self.admin.get("/api/admin/security-alerts").get_json()["unread"], 0)

    def test_existing_legacy_session_can_cryptographically_enroll_current_device(self):
        legacy = app.test_client()
        with app.app_context():
            viewer = db.session.get(UserAccount, self.viewer_id)
            viewer.active_session_hash = session_digest("legacy-viewer-token")
            db.session.commit()
        with legacy.session_transaction() as flask_session:
            flask_session["user_id"] = self.viewer_id
            flask_session["auth_token"] = "legacy-viewer-token"
            flask_session["csrf_token"] = "legacy-viewer-csrf"
        key = ec.generate_private_key(ec.SECP256R1())
        challenge_response = legacy.post("/api/auth/device/challenge", json={"username": "viewer"})
        challenge = base64.urlsafe_b64decode(challenge_response.get_json()["challenge"] + "==")
        der_signature = key.sign(challenge, ec.ECDSA(hashes.SHA256()))
        r_value, s_value = decode_dss_signature(der_signature)
        raw_signature = r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
        public_key = key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        response = legacy.post(
            "/api/auth/device/enroll-current",
            json={
                "device_kind": "desktop",
                "touch_points": 0,
                "device_label": "Safari · Mac",
                "device_public_key": b64url(public_key),
                "device_signature": b64url(raw_signature),
            },
            headers={"X-CSRF-Token": "legacy-viewer-csrf", "User-Agent": "Mozilla/5.0 (Macintosh) Safari"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(legacy.get("/api/auth/me").get_json()["device_protected"])
        with app.app_context():
            self.assertIsNone(db.session.get(UserAccount, self.viewer_id).active_session_hash)


if __name__ == "__main__":
    unittest.main()

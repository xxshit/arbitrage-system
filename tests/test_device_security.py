import base64
import os
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from werkzeug.security import generate_password_hash

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "device-security-test-secret"
os.environ["CHAT_ENCRYPTION_KEY"] = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

import app as app_module
from app import (
    AccountDevice,
    InviteCode,
    LoginSecurityAlert,
    UserAccount,
    app,
    db,
    migrate_account_levels,
    request_source_ip,
    resolve_ip_location,
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
                account_level="lv3",
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

    def login_with_key(self, client, key, kind="desktop", password="viewer-password-123", source_ip=None):
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
        headers = {"User-Agent": "Mozilla/5.0 (iPhone) Mobile Safari" if mobile else "Mozilla/5.0 (Macintosh) Chrome"}
        if source_ip:
            headers["X-Real-IP"] = source_ip
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
            headers=headers,
        )

    def login_with_password_only(self, client, password="viewer-password-123", source_ip=None):
        return client.post(
            "/api/auth/login",
            json={"username": "viewer", "password": password},
            headers={"X-Real-IP": source_ip} if source_ip else None,
        )

    def test_source_ip_trusts_only_the_local_reverse_proxy(self):
        with app.test_request_context(
            "/", headers={"X-Forwarded-For": "198.51.100.7, 203.0.113.9"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        ):
            self.assertEqual(request_source_ip(), "203.0.113.9")
        with app.test_request_context(
            "/", headers={"X-Real-IP": "198.51.100.7"},
            environ_base={"REMOTE_ADDR": "192.0.2.44"},
        ):
            self.assertEqual(request_source_ip(), "192.0.2.44")
        with app.test_request_context(
            "/", headers={"X-Real-IP": "not-an-ip"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        ):
            self.assertEqual(request_source_ip(), "127.0.0.1")

    def test_public_ip_location_uses_city_postal_and_network_provider(self):
        payload = {
            "success": True,
            "ip": "8.8.8.8",
            "country": "美国",
            "region": "加利福尼亚州",
            "city": "山景城",
            "postal": "94043",
            "connection": {"isp": "Google LLC"},
        }

        class LookupResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return __import__("json").dumps(payload, ensure_ascii=False).encode("utf-8")

        app_module.IP_LOCATION_CACHE.clear()
        with patch.object(app_module, "urlopen", return_value=LookupResponse()) as lookup:
            location = resolve_ip_location("8.8.8.8")
        self.assertEqual(location, "美国 加利福尼亚州 山景城 邮编 94043 · Google LLC")
        self.assertIn("ipwho.is", lookup.call_args.args[0].full_url)

    def test_lv1_account_requests_device_proof_after_password_validation(self):
        response = self.login_with_password_only(app.test_client())
        self.assertEqual(response.status_code, 428)
        self.assertTrue(response.get_json()["device_proof_required"])

    def test_new_registration_defaults_to_lv1(self):
        invite_code = "ARBI-LV1TEST"
        with app.app_context():
            db.session.add(InviteCode(
                code_hash=session_digest(invite_code),
                code_prefix=invite_code[:10],
                code_value=invite_code,
                created_by=self.admin_id,
            ))
            db.session.commit()
        response = app.test_client().post(
            "/api/auth/register",
            json={"username": "newviewer", "password": "new-viewer-password", "invite_code": invite_code},
        )
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            created = UserAccount.query.filter_by(username="newviewer").one()
            self.assertEqual(created.account_level, "lv1")
            self.assertEqual(created.role, "viewer")

    def test_legacy_accounts_migrate_admin_to_lv3_and_viewer_to_lv2(self):
        with app.app_context():
            admin = db.session.get(UserAccount, self.admin_id)
            viewer = db.session.get(UserAccount, self.viewer_id)
            admin.account_level = "restricted"
            viewer.account_level = "restricted"
            db.session.commit()
            migrate_account_levels()
            self.assertEqual(admin.account_level, "lv3")
            self.assertEqual(viewer.account_level, "lv2")

    def test_lv2_account_can_switch_devices_without_binding(self):
        with app.app_context():
            viewer = db.session.get(UserAccount, self.viewer_id)
            viewer.account_level = "lv2"
            db.session.commit()
        first = app.test_client()
        second = app.test_client()
        first_response = self.login_with_password_only(first)
        self.assertEqual(first_response.status_code, 200)
        self.assertFalse(first_response.get_json()["device_lock_required"])
        self.assertEqual(first.get("/api/auth/me").status_code, 200)
        self.assertEqual(self.login_with_password_only(second).status_code, 200)
        self.assertEqual(first.get("/api/auth/me").status_code, 401)
        self.assertEqual(second.get("/api/auth/me").status_code, 200)
        with app.app_context():
            self.assertEqual(AccountDevice.query.filter_by(user_id=self.viewer_id).count(), 0)
            self.assertEqual(LoginSecurityAlert.query.count(), 0)

    def test_lv2_login_records_the_latest_ip(self):
        with app.app_context():
            viewer = db.session.get(UserAccount, self.viewer_id)
            viewer.account_level = "lv2"
            db.session.commit()
        self.assertEqual(self.login_with_password_only(app.test_client(), source_ip="203.0.113.21").status_code, 200)
        with app.app_context():
            self.assertEqual(db.session.get(UserAccount, self.viewer_id).last_login_ip, "203.0.113.21")

    def test_successful_login_records_and_returns_approximate_location(self):
        with app.app_context():
            viewer = db.session.get(UserAccount, self.viewer_id)
            viewer.account_level = "lv2"
            db.session.commit()
        with patch.object(app_module, "resolve_ip_location", return_value="中国 广东省 深圳市 · 中国电信"):
            response = self.login_with_password_only(app.test_client(), source_ip="8.8.8.8")
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            viewer = db.session.get(UserAccount, self.viewer_id)
            self.assertEqual(viewer.last_login_location, "中国 广东省 深圳市 · 中国电信")
        users = self.admin.get("/api/admin/users").get_json()["items"]
        viewer_payload = next(item for item in users if item["id"] == self.viewer_id)
        self.assertEqual(viewer_payload["last_login_location"], "中国 广东省 深圳市 · 中国电信")

    def test_admin_can_change_account_level_and_invalidate_current_session(self):
        viewer = app.test_client()
        self.assertEqual(self.login_with_key(viewer, ec.generate_private_key(ec.SECP256R1())).status_code, 200)
        response = self.admin.patch(
            f"/api/admin/users/{self.viewer_id}/account-level",
            json={"account_level": "lv2"},
            headers={"X-CSRF-Token": "owner-csrf"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["device_lock_required"])
        self.assertEqual(viewer.get("/api/auth/me").status_code, 401)
        users = self.admin.get("/api/admin/users").get_json()["items"]
        updated = next(item for item in users if item["id"] == self.viewer_id)
        self.assertEqual(updated["account_level"], "lv2")
        self.assertFalse(updated["device_lock_required"])

    def test_downgrading_restores_the_previous_device_binding(self):
        original_key = ec.generate_private_key(ec.SECP256R1())
        original = app.test_client()
        self.assertEqual(self.login_with_key(original, original_key).status_code, 200)
        for level in ("lv2", "lv1"):
            response = self.admin.patch(
                f"/api/admin/users/{self.viewer_id}/account-level",
                json={"account_level": level},
                headers={"X-CSRF-Token": "owner-csrf"},
            )
            self.assertEqual(response.status_code, 200)
        restored = app.test_client()
        blocked = app.test_client()
        self.assertEqual(self.login_with_key(restored, original_key).status_code, 200)
        self.assertEqual(self.login_with_key(blocked, ec.generate_private_key(ec.SECP256R1())).status_code, 409)

    def test_account_level_rejects_unknown_values(self):
        response = self.admin.patch(
            f"/api/admin/users/{self.viewer_id}/account-level",
            json={"account_level": "vip-unknown"},
            headers={"X-CSRF-Token": "owner-csrf"},
        )
        self.assertEqual(response.status_code, 400)

    def test_only_admin_can_be_lv3_and_admin_cannot_be_downgraded(self):
        viewer_response = self.admin.patch(
            f"/api/admin/users/{self.viewer_id}/account-level",
            json={"account_level": "lv3"},
            headers={"X-CSRF-Token": "owner-csrf"},
        )
        self.assertEqual(viewer_response.status_code, 400)
        admin_response = self.admin.patch(
            f"/api/admin/users/{self.admin_id}/account-level",
            json={"account_level": "lv2"},
            headers={"X-CSRF-Token": "owner-csrf"},
        )
        self.assertEqual(admin_response.status_code, 400)

    def test_admin_is_reported_as_lv3(self):
        users = self.admin.get("/api/admin/users").get_json()["items"]
        owner = next(item for item in users if item["id"] == self.admin_id)
        self.assertEqual(owner["account_level"], "lv3")
        self.assertFalse(owner["device_lock_required"])

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

    def test_lv1_account_records_account_and_device_ip(self):
        response = self.login_with_key(
            app.test_client(), ec.generate_private_key(ec.SECP256R1()), source_ip="2001:db8::8"
        )
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            viewer = db.session.get(UserAccount, self.viewer_id)
            device = AccountDevice.query.filter_by(user_id=self.viewer_id, device_kind="desktop").one()
            self.assertEqual(viewer.last_login_ip, "2001:db8::8")
            self.assertEqual(device.last_ip, "2001:db8::8")
            self.assertEqual(device.last_location, "内网地址")
        users = self.admin.get("/api/admin/users").get_json()["items"]
        viewer_payload = next(item for item in users if item["id"] == self.viewer_id)
        self.assertEqual(viewer_payload["last_login_ip"], "2001:db8::8")
        self.assertEqual(viewer_payload["devices"][0]["last_ip"], "2001:db8::8")

    def test_second_desktop_is_blocked_and_reported_after_correct_password(self):
        first = app.test_client()
        blocked = app.test_client()
        self.assertEqual(self.login_with_key(first, ec.generate_private_key(ec.SECP256R1())).status_code, 200)
        response = self.login_with_key(blocked, ec.generate_private_key(ec.SECP256R1()), source_ip="198.51.100.52")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["device_blocked"])
        self.assertIn("报告管理员", response.get_json()["error"])
        self.assertEqual(first.get("/api/auth/me").status_code, 200)
        alerts = self.admin.get("/api/admin/security-alerts").get_json()
        self.assertEqual(alerts["unread"], 1)
        self.assertEqual(alerts["items"][0]["username"], "viewer")
        self.assertTrue(alerts["items"][0]["attempted_device_id"].startswith("PC-"))
        self.assertEqual(alerts["items"][0]["source_ip"], "198.51.100.52")
        self.assertEqual(alerts["items"][0]["source_location"], "内网地址")

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

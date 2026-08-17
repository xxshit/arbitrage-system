import base64
import json
import os
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "web-push-test-secret"
os.environ["CHAT_ENCRYPTION_KEY"] = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

import app as app_module
from app import (
    ChatPushSubscription,
    UserAccount,
    app,
    db,
    decrypt_web_push_subscription,
    deliver_chat_web_push,
    encrypt_web_push_payload,
    session_digest,
)


VAPID_PRIVATE_NUMBER = 1
VAPID_PRIVATE_KEY = base64.urlsafe_b64encode(VAPID_PRIVATE_NUMBER.to_bytes(32, "big")).decode("ascii").rstrip("=")
VAPID_PUBLIC_KEY = base64.urlsafe_b64encode(
    ec.derive_private_key(VAPID_PRIVATE_NUMBER, ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
).decode("ascii").rstrip("=")
PUSH_ENV = {
    "WEB_PUSH_VAPID_PUBLIC_KEY": VAPID_PUBLIC_KEY,
    "WEB_PUSH_VAPID_PRIVATE_KEY": VAPID_PRIVATE_KEY,
    "WEB_PUSH_VAPID_SUBJECT": "mailto:push@example.test",
}
SUBSCRIBER_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
SUBSCRIBER_PUBLIC_KEY = base64.urlsafe_b64encode(
    SUBSCRIBER_PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
).decode("ascii").rstrip("=")
APPLE_SUBSCRIPTION = {
    "endpoint": "https://web.push.apple.com/QB-test-subscription-endpoint",
    "keys": {
        "p256dh": SUBSCRIBER_PUBLIC_KEY,
        "auth": base64.urlsafe_b64encode(bytes(range(16))).decode("ascii").rstrip("="),
    },
}


class WebPushTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        with app.app_context():
            db.drop_all()
            db.create_all()
            alice = UserAccount(
                username="alice",
                password_hash="unused",
                role="admin",
                active_session_hash=session_digest("alice-token"),
            )
            bob = UserAccount(
                username="bob",
                password_hash="unused",
                role="viewer",
                active_session_hash=session_digest("bob-token"),
            )
            db.session.add_all([alice, bob])
            db.session.commit()
            self.alice_id = alice.id
            self.bob_id = bob.id
        self.alice = app.test_client()
        with self.alice.session_transaction() as session:
            session["user_id"] = self.alice_id
            session["auth_token"] = "alice-token"
            session["csrf_token"] = "alice-csrf"

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()

    def create_subscription(self):
        with patch.dict(os.environ, PUSH_ENV, clear=False):
            return self.alice.post(
                "/api/chat/push/subscriptions",
                json={"subscription": APPLE_SUBSCRIPTION},
                headers={"X-CSRF-Token": "alice-csrf"},
            )

    def test_service_worker_and_manifest_are_public_but_contain_no_credentials(self):
        anonymous = app.test_client()
        worker = anonymous.get("/service-worker.js")
        self.assertEqual(worker.status_code, 200)
        self.assertEqual(worker.headers["Service-Worker-Allowed"], "/")
        self.assertIn(b"showNotification", worker.data)
        self.assertNotIn(b"VAPID", worker.data)

        manifest = anonymous.get("/manifest.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        payload = manifest.get_json()
        self.assertEqual(payload["display"], "standalone")
        self.assertEqual(payload["scope"], "/")

    def test_payload_uses_aes128gcm_record_header(self):
        payload = '{"type":"chat-message"}'
        encrypted = encrypt_web_push_payload(APPLE_SUBSCRIPTION, payload)
        self.assertGreater(len(encrypted), 86)
        salt = encrypted[:16]
        self.assertEqual(len(salt), 16)
        self.assertEqual(int.from_bytes(encrypted[16:20], "big"), 4096)
        self.assertEqual(encrypted[20], 65)
        self.assertEqual(encrypted[21], 4)
        server_public_bytes = encrypted[21:86]
        server_public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), server_public_bytes
        )
        subscriber_public_bytes = SUBSCRIBER_PRIVATE_KEY.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        auth_secret = bytes(range(16))
        shared_secret = SUBSCRIBER_PRIVATE_KEY.exchange(ec.ECDH(), server_public_key)
        ikm = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=auth_secret,
            info=b"WebPush: info\x00" + subscriber_public_bytes + server_public_bytes,
        ).derive(shared_secret)
        content_key = HKDF(
            algorithm=hashes.SHA256(),
            length=16,
            salt=salt,
            info=b"Content-Encoding: aes128gcm\x00",
        ).derive(ikm)
        nonce = HKDF(
            algorithm=hashes.SHA256(),
            length=12,
            salt=salt,
            info=b"Content-Encoding: nonce\x00",
        ).derive(ikm)
        decrypted = AESGCM(content_key).decrypt(nonce, encrypted[86:], None)
        self.assertEqual(decrypted, payload.encode("utf-8") + b"\x02")

    def test_subscription_requires_csrf_and_rejects_non_push_hosts(self):
        with patch.dict(os.environ, PUSH_ENV, clear=False):
            missing_csrf = self.alice.post(
                "/api/chat/push/subscriptions",
                json={"subscription": APPLE_SUBSCRIPTION},
            )
            self.assertEqual(missing_csrf.status_code, 403)
            invalid = dict(APPLE_SUBSCRIPTION, endpoint="https://127.0.0.1/private")
            rejected = self.alice.post(
                "/api/chat/push/subscriptions",
                json={"subscription": invalid},
                headers={"X-CSRF-Token": "alice-csrf"},
            )
            self.assertEqual(rejected.status_code, 400)

    def test_subscription_is_encrypted_and_can_be_removed(self):
        created = self.create_subscription()
        self.assertEqual(created.status_code, 200)
        with app.app_context():
            row = ChatPushSubscription.query.one()
            self.assertNotIn("web.push.apple.com", row.subscription_data)
            self.assertEqual(decrypt_web_push_subscription(row.subscription_data), APPLE_SUBSCRIPTION)

        with patch.dict(os.environ, PUSH_ENV, clear=False):
            status = self.alice.get("/api/chat/push/config").get_json()
        self.assertTrue(status["available"])
        self.assertEqual(status["subscription_count"], 1)
        removed = self.alice.delete(
            "/api/chat/push/subscriptions",
            json={"subscription": APPLE_SUBSCRIPTION},
            headers={"X-CSRF-Token": "alice-csrf"},
        )
        self.assertEqual(removed.status_code, 200)
        with app.app_context():
            self.assertEqual(ChatPushSubscription.query.count(), 0)

    def test_delivery_contains_no_chat_body_and_updates_success_state(self):
        self.create_subscription()
        sender = Mock()
        with patch.dict(os.environ, PUSH_ENV, clear=False), patch.object(app_module, "send_web_push", sender):
            with app.app_context():
                result = deliver_chat_web_push(self.alice_id, 77)
                row = ChatPushSubscription.query.one()
                self.assertIsNotNone(row.last_success_at)
        self.assertEqual(result["sent"], 1)
        payload = json.loads(sender.call_args.args[1])
        self.assertEqual(payload["message_id"], 77)
        self.assertEqual(payload["body"], "打开协作记录查看内容")
        self.assertNotIn("message_body", payload)

    def test_sending_message_queues_recipient_push(self):
        with patch.object(app_module, "queue_chat_web_push") as queued:
            response = self.alice.post(
                "/api/chat/messages",
                json={"recipient_id": self.bob_id, "body": "不会进入系统通知的正文"},
                headers={"X-CSRF-Token": "alice-csrf"},
            )
        self.assertEqual(response.status_code, 200)
        queued.assert_called_once_with(self.bob_id, response.get_json()["item"]["id"])

    def test_logout_removes_only_the_current_device_subscription(self):
        self.create_subscription()
        response = self.alice.post(
            "/api/auth/logout",
            json={"push_endpoint": APPLE_SUBSCRIPTION["endpoint"]},
        )
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            self.assertEqual(ChatPushSubscription.query.count(), 0)


if __name__ == "__main__":
    unittest.main()

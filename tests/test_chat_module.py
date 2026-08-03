import os
import io
import unittest
from datetime import datetime, timedelta

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "chat-test-secret"

from app import (
    ChatAttachment,
    ChatMessage,
    ChatViewState,
    UserAccount,
    app,
    cleanup_expired_chat_history,
    db,
    session_digest,
)


class ChatModuleTests(unittest.TestCase):
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
        self.bob = app.test_client()
        self._login(self.alice, self.alice_id, "alice-token", "alice-csrf")
        self._login(self.bob, self.bob_id, "bob-token", "bob-csrf")

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()
            # Other unit-test modules share the imported Flask app in the same
            # discovery process, so leave an empty schema behind for them.
            db.create_all()

    @staticmethod
    def _login(client, user_id, token, csrf):
        with client.session_transaction() as session:
            session["user_id"] = user_id
            session["auth_token"] = token
            session["csrf_token"] = csrf

    def test_clear_only_hides_current_users_copy(self):
        response = self.alice.post(
            "/api/chat/messages",
            json={"recipient_id": self.bob_id, "body": "只清空自己的记录"},
            headers={"X-CSRF-Token": "alice-csrf"},
        )
        self.assertEqual(response.status_code, 200)

        response = self.alice.post(
            f"/api/chat/conversations/{self.bob_id}/clear",
            headers={"X-CSRF-Token": "alice-csrf"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.alice.get(f"/api/chat/messages/{self.bob_id}").get_json()["items"], [])

        bob_contacts = self.bob.get("/api/chat/users").get_json()
        self.assertEqual(bob_contacts["unread_total"], 1)
        bob_messages = self.bob.get(f"/api/chat/messages/{self.alice_id}").get_json()["items"]
        self.assertEqual([item["body"] for item in bob_messages], ["只清空自己的记录"])

    def test_history_can_page_backward_without_crossing_clear_cursor(self):
        with app.app_context():
            db.session.add(ChatViewState(user_id=self.alice_id, peer_id=self.bob_id, cleared_through_id=5))
            db.session.add_all([
                ChatMessage(sender_id=self.bob_id, recipient_id=self.alice_id, body=f"消息 {index}")
                for index in range(106)
            ])
            db.session.commit()

        latest = self.alice.get(f"/api/chat/messages/{self.bob_id}").get_json()
        self.assertEqual(len(latest["items"]), 100)
        self.assertTrue(latest["has_more_before"])
        self.assertGreater(latest["oldest_id"], 5)

        older = self.alice.get(
            f"/api/chat/messages/{self.bob_id}?before_id={latest['oldest_id']}"
        ).get_json()
        self.assertEqual(len(older["items"]), 1)
        self.assertFalse(older["has_more_before"])
        self.assertGreater(older["items"][0]["id"], 5)

    def test_chat_navigation_concealment_is_account_scoped_and_admin_only(self):
        viewer_rules = self.bob.get("/api/runtime-rules").get_json()["items"]
        viewer_rule = next(item for item in viewer_rules if item["key"] == "chat_nav_hidden")
        self.assertEqual(viewer_rule["value"], "0")
        self.assertFalse(viewer_rule["editable"])

        denied = self.bob.patch(
            "/api/runtime-rules/chat_nav_hidden",
            json={"value": "1"},
            headers={"X-CSRF-Token": "bob-csrf"},
        )
        self.assertEqual(denied.status_code, 403)

        updated = self.alice.patch(
            "/api/runtime-rules/chat_nav_hidden",
            json={"value": "1"},
            headers={"X-CSRF-Token": "alice-csrf"},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(self.alice.get("/api/auth/me").get_json()["chat_nav_hidden"])
        self.assertIn(b'class="chat-nav-entry hidden"', self.alice.get("/").data)
        self.assertFalse(self.bob.get("/api/auth/me").get_json()["chat_nav_hidden"])

    def test_collaboration_entry_uses_new_badge_without_explanatory_copy(self):
        page = self.alice.get("/").get_data(as_text=True)
        self.assertIn('class="nav-unread chat-new hidden">NEW</span>', page)
        self.assertNotIn("账号之间的一对一协作留言", page)

    def test_image_message_is_private_and_has_mysql_metadata(self):
        image_bytes = b"\x89PNG\r\n\x1a\n" + b"chat-image-test"
        response = self.alice.post(
            "/api/chat/messages",
            data={
                "recipient_id": str(self.bob_id),
                "body": "图片说明 😊",
                "image": (io.BytesIO(image_bytes), "截图.png"),
            },
            headers={"X-CSRF-Token": "alice-csrf"},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        item = response.get_json()["item"]
        self.assertEqual(item["body"], "图片说明 😊")
        self.assertEqual(item["image"]["mime_type"], "image/png")

        with app.app_context():
            attachment = db.session.get(ChatAttachment, item["image"]["id"])
            self.assertIsNotNone(attachment)
            self.assertEqual(attachment.data, image_bytes)

        image_response = self.bob.get(item["image"]["url"])
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(image_response.mimetype, "image/png")
        image_response.close()
        self.assertEqual(app.test_client().get(item["image"]["url"]).status_code, 401)

    def test_chat_retention_removes_message_metadata_and_file_after_30_days(self):
        now = datetime(2026, 8, 3, 12, 0, 0)
        with app.app_context():
            old = ChatMessage(
                sender_id=self.alice_id,
                recipient_id=self.bob_id,
                body="过期图片",
                created_at=now - timedelta(days=31),
            )
            fresh = ChatMessage(
                sender_id=self.alice_id,
                recipient_id=self.bob_id,
                body="仍在保留期",
                created_at=now - timedelta(days=29),
            )
            db.session.add_all([old, fresh])
            db.session.flush()
            image_bytes = b"\x89PNG\r\n\x1a\nexpired"
            db.session.add(ChatAttachment(
                message_id=old.id,
                original_name="expired.png",
                mime_type="image/png",
                file_size=len(image_bytes),
                data=image_bytes,
                created_at=old.created_at,
            ))
            db.session.commit()
            old_id = old.id
            fresh_id = fresh.id

            result = cleanup_expired_chat_history(now=now)
            self.assertEqual(result["messages"], 1)
            self.assertEqual(result["images"], 1)
            self.assertIsNone(db.session.get(ChatMessage, old_id))
            self.assertIsNotNone(db.session.get(ChatMessage, fresh_id))


if __name__ == "__main__":
    unittest.main()

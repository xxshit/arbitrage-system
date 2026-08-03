import os
import io
import unittest
from datetime import datetime, timezone, timedelta
from werkzeug.security import check_password_hash

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "chat-test-secret"
os.environ["CHAT_ENCRYPTION_KEY"] = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

from app import (
    ChatAttachment,
    ChatContactRemark,
    ChatMessage,
    ChatViewState,
    AccountSecurityEvent,
    UserAccount,
    app,
    backfill_chat_read_times,
    cleanup_expired_chat_history,
    migrate_chat_content_encryption,
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

    def test_chat_timestamps_are_utc_plus_8_and_delivery_is_logged(self):
        sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
        expected_full = (sent_at + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        expected_short = (sent_at + timedelta(hours=8)).strftime("%m-%d %H:%M")
        with app.app_context():
            row = ChatMessage(
                sender_id=self.alice_id,
                recipient_id=self.bob_id,
                body="北京时间验证",
                created_at=sent_at,
            )
            db.session.add(row)
            db.session.commit()
            message_id = row.id

        with self.assertLogs(app.logger, level="INFO") as logs:
            messages = self.bob.get(f"/api/chat/messages/{self.alice_id}").get_json()["items"]
        self.assertEqual(messages[-1]["created_at"], expected_full)
        self.assertTrue(any(f"message_id={message_id}" in line for line in logs.output))

        contacts = self.bob.get("/api/chat/users").get_json()["items"]
        alice = next(item for item in contacts if item["id"] == self.alice_id)
        self.assertEqual(alice["last_message_at"], expected_short)

    def test_sender_sees_unread_then_read_after_peer_opens_conversation(self):
        sent = self.alice.post(
            "/api/chat/messages",
            json={"recipient_id": self.bob_id, "body": "已读状态验证"},
            headers={"X-CSRF-Token": "alice-csrf"},
        )
        self.assertEqual(sent.status_code, 200)
        sent_item = sent.get_json()["item"]
        self.assertFalse(sent_item["read_by_peer"])
        self.assertIsNone(sent_item["read_at"])

        before_read = self.alice.get(f"/api/chat/messages/{self.bob_id}").get_json()
        self.assertFalse(before_read["items"][-1]["read_by_peer"])
        self.assertEqual(before_read["peer_last_read_message_id"], 0)

        received = self.bob.get(f"/api/chat/messages/{self.alice_id}").get_json()
        self.assertEqual(received["items"][-1]["body"], "已读状态验证")

        after_read = self.alice.get(f"/api/chat/messages/{self.bob_id}").get_json()
        self.assertTrue(after_read["items"][-1]["read_by_peer"])
        self.assertIsNotNone(after_read["items"][-1]["read_at"])
        self.assertEqual(after_read["items"][-1]["read_at"], after_read["peer_last_read_at"])
        self.assertGreaterEqual(after_read["peer_last_read_message_id"], sent_item["id"])

        with app.app_context():
            self.assertIsNotNone(db.session.get(ChatMessage, sent_item["id"]).read_at)

    def test_legacy_read_receipt_time_is_backfilled_once(self):
        legacy_read_at = datetime(2026, 8, 3, 12, 34, 56)
        with app.app_context():
            message = ChatMessage(
                sender_id=self.alice_id,
                recipient_id=self.bob_id,
                body="旧已读回执",
            )
            db.session.add(message)
            db.session.flush()
            db.session.add(ChatViewState(
                user_id=self.bob_id,
                peer_id=self.alice_id,
                last_read_message_id=message.id,
                updated_at=legacy_read_at,
            ))
            db.session.commit()
            message_id = message.id

            self.assertEqual(backfill_chat_read_times(), 1)
            self.assertEqual(backfill_chat_read_times(), 0)
            self.assertEqual(db.session.get(ChatMessage, message_id).read_at, legacy_read_at)

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

    def test_collaboration_entry_uses_motion_instead_of_visible_new_badge(self):
        page = self.alice.get("/").get_data(as_text=True)
        self.assertIn('class="chat-nav-entry', page)
        self.assertNotIn('id="chatNavUnread"', page)
        self.assertNotIn('>NEW</span>', page)
        self.assertIn('id="alarmSoundToggle" type="checkbox"', page)
        self.assertIn('id="chatSoundToggle" type="checkbox"', page)
        self.assertIn("协作声音", page)
        self.assertNotIn("账号之间的一对一协作留言", page)

    def test_admin_can_reset_forgotten_password_without_storing_plaintext(self):
        denied = self.bob.patch(
            f"/api/admin/users/{self.alice_id}/password",
            json={"mode": "generate"},
            headers={"X-CSRF-Token": "bob-csrf"},
        )
        self.assertEqual(denied.status_code, 403)

        response = self.alice.patch(
            f"/api/admin/users/{self.bob_id}/password",
            json={"mode": "generate"},
            headers={"X-CSRF-Token": "alice-csrf"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        temporary_password = response.get_json()["temporary_password"]
        self.assertGreaterEqual(len(temporary_password), 10)

        with app.app_context():
            bob = db.session.get(UserAccount, self.bob_id)
            self.assertIsNone(bob.active_session_hash)
            self.assertIsNotNone(bob.password_changed_at)
            self.assertNotEqual(bob.password_hash, temporary_password)
            self.assertTrue(check_password_hash(bob.password_hash, temporary_password))
            event = AccountSecurityEvent.query.filter_by(target_user_id=self.bob_id).one()
            self.assertEqual(event.actor_user_id, self.alice_id)
            self.assertEqual(event.event_type, "admin_password_reset_generated")

        self.assertEqual(self.bob.get("/api/auth/me").status_code, 401)

    def test_admin_can_disable_and_restore_account_without_self_lockout(self):
        denied = self.bob.patch(
            f"/api/admin/users/{self.alice_id}/active",
            json={"active": False},
            headers={"X-CSRF-Token": "bob-csrf"},
        )
        self.assertEqual(denied.status_code, 403)

        self_lockout = self.alice.patch(
            f"/api/admin/users/{self.alice_id}/active",
            json={"active": False},
            headers={"X-CSRF-Token": "alice-csrf"},
        )
        self.assertEqual(self_lockout.status_code, 400)

        disabled = self.alice.patch(
            f"/api/admin/users/{self.bob_id}/active",
            json={"active": False},
            headers={"X-CSRF-Token": "alice-csrf"},
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.get_json()["active"])
        self.assertTrue(disabled.get_json()["signed_out"])
        self.assertEqual(self.bob.get("/api/auth/me").status_code, 401)

        with app.app_context():
            bob = db.session.get(UserAccount, self.bob_id)
            self.assertFalse(bob.active)
            self.assertIsNone(bob.active_session_hash)
            event = AccountSecurityEvent.query.filter_by(
                target_user_id=self.bob_id,
                event_type="admin_account_disabled",
            ).one()
            self.assertEqual(event.actor_user_id, self.alice_id)

        restored = self.alice.patch(
            f"/api/admin/users/{self.bob_id}/active",
            json={"active": True},
            headers={"X-CSRF-Token": "alice-csrf"},
        )
        self.assertEqual(restored.status_code, 200)
        self.assertTrue(restored.get_json()["active"])

        with app.app_context():
            self.assertTrue(db.session.get(UserAccount, self.bob_id).active)
            self.assertEqual(
                AccountSecurityEvent.query.filter_by(
                    target_user_id=self.bob_id,
                    event_type="admin_account_enabled",
                ).count(),
                1,
            )

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
            message = db.session.get(ChatMessage, item["id"])
            self.assertEqual(message.encryption_version, 1)
            self.assertNotEqual(message.body, "图片说明 😊")
            attachment = db.session.get(ChatAttachment, item["image"]["id"])
            self.assertIsNotNone(attachment)
            self.assertEqual(attachment.encryption_version, 1)
            self.assertNotEqual(attachment.data, image_bytes)
            self.assertFalse(attachment.data.startswith(b"\x89PNG"))

        image_response = self.bob.get(item["image"]["url"])
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(image_response.mimetype, "image/png")
        image_response.close()
        self.assertEqual(app.test_client().get(item["image"]["url"]).status_code, 401)

    def test_legacy_plaintext_chat_is_migrated_once(self):
        image_bytes = b"\x89PNG\r\n\x1a\nlegacy-image"
        with app.app_context():
            message = ChatMessage(
                sender_id=self.alice_id,
                recipient_id=self.bob_id,
                body="旧消息明文",
                encryption_version=0,
            )
            db.session.add(message)
            db.session.flush()
            attachment = ChatAttachment(
                message_id=message.id,
                original_name="private-name.png",
                mime_type="image/png",
                file_size=len(image_bytes),
                data=image_bytes,
                encryption_version=0,
            )
            db.session.add(attachment)
            db.session.commit()
            message_id = message.id
            attachment_id = attachment.id

            first = migrate_chat_content_encryption()
            second = migrate_chat_content_encryption()
            migrated_message = db.session.get(ChatMessage, message_id)
            migrated_attachment = db.session.get(ChatAttachment, attachment_id)
            self.assertEqual(first, {"messages": 1, "images": 1})
            self.assertEqual(second, {"messages": 0, "images": 0})
            self.assertEqual(migrated_message.encryption_version, 1)
            self.assertNotIn("旧消息明文", migrated_message.body)
            self.assertEqual(migrated_attachment.original_name, "image.png")
            self.assertFalse(migrated_attachment.data.startswith(b"\x89PNG"))

        messages = self.bob.get(f"/api/chat/messages/{self.alice_id}").get_json()["items"]
        self.assertEqual(messages[-1]["body"], "旧消息明文")
        image_response = self.bob.get(f"/api/chat/attachments/{attachment_id}")
        self.assertEqual(image_response.data, image_bytes)

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

    def test_contact_remark_is_private_encrypted_and_can_be_cleared(self):
        response = self.alice.patch(
            f"/api/chat/users/{self.bob_id}/remark",
            json={"remark": "盘友小林"},
            headers={"X-CSRF-Token": "alice-csrf"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["display_name"], "盘友小林")

        alice_contacts = self.alice.get("/api/chat/users").get_json()["items"]
        bob_for_alice = next(item for item in alice_contacts if item["id"] == self.bob_id)
        self.assertEqual(bob_for_alice["remark"], "盘友小林")
        self.assertEqual(bob_for_alice["display_name"], "盘友小林")
        conversation = self.alice.get(f"/api/chat/messages/{self.bob_id}").get_json()
        self.assertEqual(conversation["peer"]["remark"], "盘友小林")

        bob_contacts = self.bob.get("/api/chat/users").get_json()["items"]
        alice_for_bob = next(item for item in bob_contacts if item["id"] == self.alice_id)
        self.assertEqual(alice_for_bob["remark"], "")
        self.assertEqual(alice_for_bob["display_name"], "alice")

        with app.app_context():
            stored = ChatContactRemark.query.filter_by(
                user_id=self.alice_id,
                peer_id=self.bob_id,
            ).one()
            self.assertNotEqual(stored.remark, "盘友小林")
            self.assertEqual(stored.encryption_version, 1)

        cleared = self.alice.patch(
            f"/api/chat/users/{self.bob_id}/remark",
            json={"remark": ""},
            headers={"X-CSRF-Token": "alice-csrf"},
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.get_json()["display_name"], "bob")
        with app.app_context():
            self.assertIsNone(ChatContactRemark.query.filter_by(
                user_id=self.alice_id,
                peer_id=self.bob_id,
            ).first())

    def test_chat_frontend_coalesces_reads_and_deduplicates_rendering(self):
        script_path = os.path.join(os.path.dirname(__file__), "..", "static", "app.js")
        with open(script_path, "r", encoding="utf-8") as handle:
            script = handle.read()
        style_path = os.path.join(os.path.dirname(__file__), "..", "static", "style.css")
        with open(style_path, "r", encoding="utf-8") as handle:
            style = handle.read()
        page = self.alice.get("/").get_data(as_text=True)

        self.assertIn("chatMessageRequests.has(requestKey)", script)
        self.assertIn("chatMessageIsRendered(item.id)", script)
        self.assertIn("appendNewChatMessages([data.item],true)", script)
        self.assertIn("let chatSendInFlight=false", script)
        self.assertIn("function isChatNetworkError", script)
        self.assertIn("chatFetch('/api/chat/users')", script)
        self.assertIn("网络短暂波动，正在自动恢复", script)
        self.assertIn("连接刚刚中断，正在核对发送结果", script)
        self.assertIn("announceIncomingChatMessage", script)
        self.assertIn("observeIncomingChatMessages", script)
        self.assertIn("updateChatReadReceipts", script)
        self.assertIn("chatNavAttentionPending", script)
        self.assertIn("let chatSoundEnabled=", script)
        self.assertIn("playChatSound()", script)
        self.assertIn("oscillator.type='sine'", script)
        self.assertIn("function chatReceiptTitle", script)
        self.assertIn('class="chat-read-state', script)
        self.assertIn("data.peer_last_read_at", script)
        self.assertIn("/active`,{method:'PATCH'", script)
        self.assertIn('class="chat-message-time"', script)
        self.assertIn("animation-duration:1.6s", style)
        self.assertIn("font-size:10px", style)
        self.assertIn("extendedChatEmojis", script)
        self.assertIn("'🤫'", script)
        self.assertIn("max-height:min(300px,45vh)", style)
        self.assertIn("arbi-chat-sound", script)
        self.assertIn("function openChatRemarkEditor", script)
        self.assertIn("/remark`,{method:'PATCH'", script)
        self.assertIn("chatDisplayName(item).toLowerCase()", script)
        self.assertIn('id="chatRemarkModal"', page)
        self.assertIn('placeholder="搜索账号或备注"', page)
        self.assertNotIn("alert(error.message)}finally{chatSendInFlight", script)
        self.assertIn("visibilitychange", script)
        self.assertIn("window.addEventListener('focus',syncChatAfterWake)", script)
        self.assertNotIn("await loadChatMessages('newer',true);await loadChatUsers()", script)


if __name__ == "__main__":
    unittest.main()

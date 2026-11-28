"""PostgreSQL integration smoke tests for the production database adapter.

This file is run in a dedicated CI job against an ephemeral PostgreSQL service.
It is intentionally not named test_*.py because the regular unit suite forces
SQLite mode for its isolated fixtures.
"""
import os
import time
import unittest
import uuid

os.environ.setdefault("BOT_TOKEN", "ci-test-token")
os.environ.setdefault("CLOUDFLARE_ACCOUNT_ID", "ci-test-account")
os.environ.setdefault("CLOUDFLARE_API_TOKEN", "ci-test-token")
os.environ.setdefault("NEPTON_DISABLE_AUTOSTART", "1")
os.environ.setdefault("NEPTON_AI_MODE", "rules")
os.environ.setdefault("SENTRY_DSN", "")

import bot


class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not bot.USE_POSTGRES:
            raise RuntimeError("PostgreSQL integration tests require DATABASE_URL")
        bot.init_database()
        bot.init_database()  # migrations must be safe to repeat
        cls.suffix = uuid.uuid4().hex[:12]

    def _chat_id(self, label):
        return f"pg-ci-{self.suffix}-{label}"

    def _update_id(self):
        return int(time.time() * 1000) + int(uuid.uuid4().hex[:4], 16)

    def test_admin_identity_round_trip_uses_persistent_state(self):
        admin_chat = self._chat_id("admin")
        self.assertTrue(bot.db_set_admin_chat_id(admin_chat))
        bot.load_persistent_runtime_state()
        self.assertEqual(bot.get_admin_chat_id(), admin_chat)

    def test_queue_outbox_and_completion_lifecycle(self):
        chat_id = self._chat_id("outbox")
        update_id = self._update_id()
        self.assertTrue(
            bot.db_enqueue_updates(
                [(update_id, chat_id, "postgres integration message")],
                update_id + 1,
            )
        )

        item = bot.db_claim_next_message("postgres-ci-worker")
        self.assertIsNotNone(item)
        self.assertEqual(item["chat_id"], chat_id)

        response_plan = [{"chat_id": chat_id, "text": "postgres integration reply"}]
        self.assertTrue(
            bot.db_prepare_response_plan(
                item["id"],
                "postgres-ci-worker",
                response_plan,
                history_user="postgres integration message",
                history_assistant="postgres integration reply",
            )
        )
        receipt = bot.db_get_delivery_state(item["id"], 0)
        self.assertEqual(receipt["status"], "pending")
        self.assertTrue(bot.db_mark_delivery_sending(item["id"], 0))
        self.assertTrue(bot.db_mark_delivery_sent(item["id"], 0))
        self.assertTrue(bot.db_advance_response(item["id"], "postgres-ci-worker", 0))
        self.assertTrue(bot.db_finish_message(item["id"], "postgres-ci-worker"))

        self.assertEqual(bot.db_get_delivery_state(item["id"], 0)["status"], "sent")
        history = bot.get_history(chat_id)
        self.assertEqual([entry["role"] for entry in history], ["user", "assistant"])
        self.assertEqual(bot.db_get_offset(), update_id + 1)

    def test_dead_letter_can_be_requeued_without_losing_record(self):
        chat_id = self._chat_id("deadletter")
        update_id = self._update_id()
        self.assertTrue(
            bot.db_enqueue_updates(
                [(update_id, chat_id, "dead-letter integration message")],
                update_id + 1,
            )
        )
        item = bot.db_claim_next_message("postgres-ci-dead-worker")
        self.assertIsNotNone(item)
        bot.db_dead_letter(item["id"], "postgres-ci-dead-worker", "integration test")

        matches = [
            entry for entry in bot.db_list_dead_letters(20)
            if entry["chat_id"] == chat_id
        ]
        self.assertEqual(len(matches), 1)
        dead_letter_id = matches[0]["id"]

        result = bot.db_requeue_dead_letter(dead_letter_id)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            bot.db_requeue_dead_letter(dead_letter_id)["reason"],
            "not_found",
        )

        with bot.db_connect() as db:
            queued = db.execute(
                "SELECT chat_id, message_text, status, update_id "
                "FROM message_queue WHERE id = ?",
                (result["queue_id"],),
            ).fetchone()
        self.assertEqual(queued["chat_id"], chat_id)
        self.assertEqual(queued["message_text"], "dead-letter integration message")
        self.assertEqual(queued["status"], "pending")
        self.assertIsNone(queued["update_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

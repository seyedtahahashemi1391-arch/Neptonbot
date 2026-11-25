import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_TEST_DIR = tempfile.TemporaryDirectory(prefix="neptonbot-tests-")
os.environ.update({
    "BOT_TOKEN": "test-token",
    "CLOUDFLARE_ACCOUNT_ID": "test-account",
    "CLOUDFLARE_API_TOKEN": "test-api-token",
    "DATABASE_URL": "",
    "NEPTON_DB_PATH": str(Path(_TEST_DIR.name) / "test.sqlite3"),
    "NEPTON_DISABLE_AUTOSTART": "1",
    "SENTRY_DSN": "",
    "NEPTON_LOG_LEVEL": "CRITICAL",
})

import bot


class ParsingTests(unittest.TestCase):
    def test_private_message_extraction(self):
        update = {
            "update_id": 12,
            "message": {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123, "username": "regular_user"},
                "text": "سلام",
            },
        }
        self.assertEqual(bot.extract_message(update), ("123", "سلام"))
        self.assertEqual(bot.get_update_id(update), 12)

    def test_weather_location_parsing(self):
        self.assertEqual(bot._weather_location("هواشناسی کاشمر"), "کاشمر")
        self.assertEqual(bot._weather_location("هوا در مشهد چطوره؟"), "مشهد")

    def test_split_message_preserves_content_and_limit(self):
        source = "آزمون پیام " * 500
        chunks = bot.split_message(source, limit=120)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 120 for chunk in chunks))
        self.assertEqual("".join(chunks), source)

    def test_update_container_validation(self):
        self.assertTrue(bot.has_update_container({"ok": True, "result": []}))
        self.assertFalse(bot.has_update_container({"ok": False, "result": []}))
        self.assertFalse(bot.has_update_container({"unexpected": "shape"}))


class DatabaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bot.init_database()

    def setUp(self):
        with bot.db_connect() as db:
            for table in (
                "message_queue",
                "processed_updates",
                "dead_letters",
                "conversation_history",
                "feedback_pending",
                "user_memory",
                "bot_state",
            ):
                db.execute(f"DELETE FROM {table}")

    def test_queue_deduplication_and_durable_completion(self):
        update = (1001, "chat-1", "سلام")
        self.assertTrue(bot.db_enqueue_updates([update], 1002))
        self.assertTrue(bot.db_enqueue_updates([update], 1002))
        self.assertEqual(bot.db_queue_stats()["queued_messages"], 1)

        item = bot.db_claim_next_message("unit-test-worker")
        self.assertIsNotNone(item)
        plan = [{"chat_id": "chat-1", "text": "سلام!"}]
        self.assertTrue(bot.db_prepare_response_plan(
            item["id"],
            "unit-test-worker",
            plan,
            history_user="سلام",
            history_assistant="سلام!",
        ))
        self.assertTrue(bot.db_advance_response(item["id"], "unit-test-worker", 0))
        self.assertTrue(bot.db_finish_message(item["id"], "unit-test-worker"))

        self.assertEqual(bot.db_queue_stats()["queued_messages"], 0)
        history = bot.get_history("chat-1")
        self.assertEqual([entry["role"] for entry in history], ["user", "assistant"])

    def test_weather_cancel_does_not_break_feedback_cancel(self):
        bot.db_set_state("weather_pending_123", str(int(time.time()) + 300))
        weather_result = bot.build_response_plan("123", "/cancel")
        self.assertIn("هواشناسی لغو شد", weather_result["responses"][0][1])

        with bot.db_connect() as db:
            db.execute(
                "INSERT INTO feedback_pending(chat_id) VALUES(?)",
                ("456",),
            )
        feedback_result = bot.build_response_plan("456", "/cancel")
        self.assertIn("ارسال نظر لغو شد", feedback_result["responses"][0][1])
        self.assertEqual(feedback_result["feedback_action"], "clear")

    def test_expired_weather_state_does_not_capture_normal_chat(self):
        bot.db_set_state("weather_pending_789", str(int(time.time()) - 1))
        result = bot.build_response_plan("789", "سلام")
        self.assertIn("سلام", result["responses"][0][1])
        self.assertIsNone(bot.db_get_state("weather_pending_789"))

    def test_market_command_uses_structured_market_provider(self):
        with patch.object(bot, "get_live_market_prices", return_value="market snapshot") as provider:
            result = bot.build_response_plan("123", "/market")
        self.assertEqual(result["responses"], [("123", "market snapshot")])
        provider.assert_called_once_with()

    def test_cleanup_removes_expired_weather_states(self):
        bot.db_set_state("weather_pending_expired", "0")
        bot.db_set_state("weather_pending_active", str(int(time.time()) + 300))
        bot.db_cleanup_old_records()
        self.assertIsNone(bot.db_get_state("weather_pending_expired"))
        self.assertIsNotNone(bot.db_get_state("weather_pending_active"))


if __name__ == "__main__":
    unittest.main()

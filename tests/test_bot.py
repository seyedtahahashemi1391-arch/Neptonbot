import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
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

_CONFIG_SPEC = importlib.util.spec_from_file_location(
    "nepton_gunicorn_config",
    Path(__file__).resolve().parents[1] / "gunicorn.conf.py",
)
GUNICORN_CONFIG = importlib.util.module_from_spec(_CONFIG_SPEC)
_CONFIG_SPEC.loader.exec_module(GUNICORN_CONFIG)


class ParsingTests(unittest.TestCase):
    def test_system_prompt_enforces_clean_school_essay_format(self):
        self.assertIn("تعداد خط یا کلمه", bot.SYSTEM_PROMPT)
        self.assertIn("ترتیب رویدادها", bot.SYSTEM_PROMPT)
        self.assertIn("متن تکراری", bot.SYSTEM_PROMPT)
        self.assertEqual(bot.MAX_COMPLETION_TOKENS, 1536)

    def test_chamran_40_line_essay_uses_reviewed_historical_facts(self):
        request = "یه انشا ۴۰ خطی درباره شهید چمران بنویس"
        self.assertTrue(bot._is_chamran_40_line_essay_request(request))
        result = bot.call_ai("test-chat", request)
        lines = result.splitlines()
        self.assertEqual(len(lines), 40)
        self.assertIn("مصطفی چمران", result)
        self.assertIn("دهم مهر سال ۱۳۱۱ در تهران", result)
        self.assertIn("دانشگاه کالیفرنیا، برکلی", result)
        self.assertIn("وزیر دفاع", result)
        self.assertIn("سی‌ویکم خرداد سال ۱۳۶۰ در دهلاویه", result)
        for false_claim in ("محمدعلی جعفری", "دانشگاه لیدز", "لشکر ۱۱ امام حسین", "کربلای ۴", "بیت‌المقدس"):
            self.assertNotIn(false_claim, result)

    def test_chamran_request_without_40_line_requirement_uses_normal_model_path(self):
        self.assertFalse(bot._is_chamran_40_line_essay_request("درباره شهید چمران توضیح بده"))

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

    def test_weather_geocoding_retries_without_strict_country_filter(self):
        geo_fallback = {
            "results": [{
                "name": "Bushehr",
                "country": "Iran",
                "country_code": "IR",
                "latitude": 28.9684,
                "longitude": 50.8385,
            }]
        }
        forecast = {
            "current": {
                "temperature_2m": 25,
                "apparent_temperature": 26,
                "relative_humidity_2m": 60,
                "wind_speed_10m": 12,
                "time": "2026-11-26T09:00",
            },
            "daily": {
                "temperature_2m_min": [20],
                "temperature_2m_max": [28],
                "precipitation_probability_max": [0],
            },
        }
        with patch.object(bot, "_get_json", side_effect=[{"results": []}, geo_fallback, forecast]) as provider:
            result = bot.get_live_weather("هواشناسی بوشهر")

        self.assertIn("Bushehr", result)
        self.assertIn("25°C", result)
        self.assertEqual(provider.call_count, 3)
        self.assertNotIn("countryCode", provider.call_args_list[1].kwargs.get("params", {}))

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



class GunicornLifecycleTests(unittest.TestCase):
    def test_runtime_starts_only_after_worker_initialization(self):
        worker = SimpleNamespace(pid=123)
        with patch.object(bot, "start_runtime_services", return_value=False) as start, patch.object(
            bot, "_start_runtime_acquirer"
        ) as retry:
            GUNICORN_CONFIG.post_worker_init(worker)
        start.assert_called_once_with()
        retry.assert_called_once_with()

    def test_worker_exit_releases_lease_for_that_worker_pid(self):
        worker = SimpleNamespace(pid=321)
        GUNICORN_CONFIG.worker_exit(SimpleNamespace(log=SimpleNamespace(exception=lambda *a, **k: None)), worker)
        # SQLite test mode intentionally has no PostgreSQL lease to release.
        self.assertFalse(bot.USE_POSTGRES)
        self.assertFalse(bot.release_runtime_lease_for_process(worker.pid))

    def test_gunicorn_does_not_preload_application(self):
        self.assertFalse(GUNICORN_CONFIG.preload_app)
        self.assertEqual(GUNICORN_CONFIG.graceful_timeout, 35)


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
        self.assertEqual(result["responses"][0][0], "123")
        self.assertIn("market snapshot", result["responses"][0][1])
        provider.assert_called_once_with()

    def test_malformed_search_results_are_filtered(self):
        response = {
            "web": {
                "results": [
                    None,
                    "invalid row",
                    {"title": "Valid", "url": "https://example.com", "description": "Snippet"},
                    {"title": "Unsafe URL", "url": "javascript:alert(1)", "description": "Ignore"},
                ]
            }
        }
        with patch.object(bot, "BRAVE_SEARCH_API_KEY", "test-key"), patch.object(
            bot, "_get_json", return_value=response
        ):
            result = bot.get_live_web_results("test query")
        self.assertIn("Valid", result)
        self.assertIn("https://example.com", result)
        self.assertNotIn("javascript:", result)

    def test_malformed_market_payload_returns_no_prices(self):
        with patch.object(bot, "_get_json", return_value={"data": "invalid", "meta": []}):
            result = bot.get_live_market_prices()
        self.assertIn("دادهٔ قابل‌استفاده نداد", result)

    def test_cleanup_removes_expired_weather_states(self):
        bot.db_set_state("weather_pending_expired", "0")
        bot.db_set_state("weather_pending_active", str(int(time.time()) + 300))
        bot.db_cleanup_old_records()
        self.assertIsNone(bot.db_get_state("weather_pending_expired"))
        self.assertIsNotNone(bot.db_get_state("weather_pending_active"))

    def test_weather_pending_does_not_capture_other_commands_or_buttons(self):
        bot.db_set_state("weather_pending_123", str(int(time.time()) + 300))
        with patch.object(bot, "get_live_market_prices", return_value="market snapshot"):
            market_result = bot.build_response_plan("123", "💰 قیمت بازار")
        self.assertEqual(market_result["responses"][0][0], "123")
        self.assertIn("market snapshot", market_result["responses"][0][1])
        self.assertIsNone(bot.db_get_state("weather_pending_123"))

        bot.db_set_state("weather_pending_123", str(int(time.time()) + 300))
        help_result = bot.build_response_plan("123", "/help")
        self.assertIn("راهنمای NeptonBot", help_result["responses"][0][1])
        self.assertIsNone(bot.db_get_state("weather_pending_123"))


    def test_start_and_help_remain_available_while_feedback_is_pending(self):
        with bot.db_connect() as db:
            db.execute(
                "INSERT INTO feedback_pending(chat_id) VALUES(?)",
                ("feedback-user",),
            )

        start_result = bot.build_response_plan("feedback-user", "/start")
        self.assertIn("NEPTON", start_result["responses"][0][1])
        self.assertIsNotNone(start_result.get("reply_markup"))
        self.assertTrue(bot.db_feedback_is_pending("feedback-user"))

        help_result = bot.build_response_plan("feedback-user", "/help")
        self.assertIn("راهنمای NeptonBot", help_result["responses"][0][1])
        self.assertTrue(bot.db_feedback_is_pending("feedback-user"))

    def test_queue_preserves_fifo_order_within_each_chat(self):
        updates = [
            (2001, "ordered-chat", "first"),
            (2002, "ordered-chat", "second"),
            (2003, "other-chat", "parallel"),
        ]
        self.assertTrue(bot.db_enqueue_updates(updates, 2004))

        first = bot.db_claim_next_message("worker-one")
        self.assertIsNotNone(first)
        self.assertEqual(first["message_text"], "first")

        # A later message from the same chat must not overtake the first.
        second_worker_claim = bot.db_claim_next_message("worker-two")
        self.assertIsNotNone(second_worker_claim)
        self.assertEqual(second_worker_claim["chat_id"], "other-chat")
        self.assertEqual(second_worker_claim["message_text"], "parallel")

        self.assertTrue(bot.db_prepare_response_plan(
            first["id"], "worker-one", []
        ))
        self.assertTrue(bot.db_finish_message(first["id"], "worker-one"))

        next_same_chat = bot.db_claim_next_message("worker-three")
        self.assertIsNotNone(next_same_chat)
        self.assertEqual(next_same_chat["message_text"], "second")

    def test_health_endpoint_reports_runtime_unavailable(self):
        client = bot.app.test_client()
        response = client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertIn(response.get_json()["status"], {"degraded", "unhealthy"})


if __name__ == "__main__":
    unittest.main()

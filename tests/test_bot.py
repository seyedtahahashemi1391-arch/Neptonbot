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
        self.assertEqual(bot.MAX_COMPLETION_TOKENS, 512)

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
        self.assertIn("چهل‌وهشت سال داشت", result)
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

    def test_ai_offline_fallback_keeps_fixed_replies_available(self):
        self.assertEqual(bot._offline_fallback_answer("سلام"), bot.get_fixed_conversation_answer("سلام"))
        self.assertEqual(bot._offline_fallback_answer("سازنده‌ات کیه؟"), bot.CREATOR_ANSWER)
        self.assertIn("اتصال سرویس هوش مصنوعی", bot._offline_fallback_answer("یک سؤال پیچیده و ناشناخته"))

    def test_cloudflare_circuit_breaker_opens_and_recovers(self):
        original_threshold = bot.AI_CIRCUIT_FAILURE_THRESHOLD
        original_cooldown = bot.AI_CIRCUIT_COOLDOWN
        try:
            bot.AI_CIRCUIT_FAILURE_THRESHOLD = 2
            bot.AI_CIRCUIT_COOLDOWN = 15
            with bot._ai_circuit_lock:
                bot._ai_circuit_failures = 0
                bot._ai_circuit_open_until = 0.0
                bot._ai_circuit_probe_in_progress = False
            self.assertTrue(bot._cloudflare_circuit_allows_request())
            bot._cloudflare_circuit_record_failure()
            self.assertTrue(bot._cloudflare_circuit_allows_request())
            bot._cloudflare_circuit_record_failure()
            self.assertFalse(bot._cloudflare_circuit_allows_request())
            with bot._ai_circuit_lock:
                bot._ai_circuit_open_until = 0.0
            self.assertTrue(bot._cloudflare_circuit_allows_request())
            self.assertFalse(bot._cloudflare_circuit_allows_request())
            bot._cloudflare_circuit_record_success()
            self.assertTrue(bot._cloudflare_circuit_allows_request())
        finally:
            bot.AI_CIRCUIT_FAILURE_THRESHOLD = original_threshold
            bot.AI_CIRCUIT_COOLDOWN = original_cooldown
            with bot._ai_circuit_lock:
                bot._ai_circuit_failures = 0
                bot._ai_circuit_open_until = 0.0
                bot._ai_circuit_probe_in_progress = False

    def test_call_ai_falls_back_when_cloudflare_network_fails(self):
        with patch.object(bot, "get_history", return_value=[]), patch.object(
            bot, "get_live_context", return_value=""
        ), patch.object(
            bot, "cloudflare_post_with_retry", side_effect=bot.TransientAPIError("offline")
        ):
            answer = bot.call_ai("offline-chat", "یک سؤال ناشناخته")
        self.assertIn("اتصال سرویس هوش مصنوعی", answer)

    def test_call_ai_uses_rules_when_cloudflare_credentials_are_missing(self):
        with patch.object(bot, "CLOUDFLARE_ACCOUNT_ID", ""), patch.object(
            bot, "CLOUDFLARE_API_TOKEN", ""
        ), patch.object(bot, "get_history", side_effect=AssertionError("history should not be read")):
            self.assertIn("اتصال سرویس هوش مصنوعی", bot.call_ai("offline-chat", "یک سؤال ناشناخته"))

    def test_database_error_detection_and_backoff(self):
        self.assertTrue(bot._is_database_error(bot.sqlite3.OperationalError("database is locked")))
        self.assertFalse(bot._is_database_error(bot.TransientAPIError("network down")))
        delays = [bot._database_retry_delay(n) for n in (1, 2, 3, 10)]
        self.assertTrue(all(0 < delay <= bot.DB_RETRY_BACKOFF_MAX for delay in delays))
        self.assertEqual(delays, sorted(delays))

    def test_worker_retries_database_claim_without_busy_loop(self):
        event = bot._polling_stop
        event.clear()
        wait_calls = {"count": 0}

        def stop_after_idle(delay):
            wait_calls["count"] += 1
            if wait_calls["count"] >= 2:
                event.set()
                return True
            return False

        try:
            with patch.object(bot, "_worker_id", return_value="test-worker"), patch.object(
                bot, "db_claim_next_message",
                side_effect=[bot.sqlite3.OperationalError("db offline"), None],
            ) as claim, patch.object(
                bot._queue_wakeup, "wait", side_effect=stop_after_idle
            ):
                bot.chat_worker()
            self.assertEqual(claim.call_count, 2)
            self.assertGreaterEqual(wait_calls["count"], 2)
        finally:
            event.clear()

    def test_polling_retries_loading_offset_after_database_failure(self):
        event = bot._polling_stop
        event.clear()
        wait_calls = {"count": 0}

        def stop_after_empty_poll(delay):
            wait_calls["count"] += 1
            if wait_calls["count"] >= 2:
                event.set()
                return True
            return False

        try:
            with patch.object(
                bot, "db_get_offset",
                side_effect=[bot.sqlite3.OperationalError("db offline"), 42],
            ) as get_offset, patch.object(
                bot, "soroush_request", return_value={"ok": True, "result":[]}
            ), patch.object(bot, "has_update_container", return_value=True), patch.object(
                bot, "extract_updates", return_value=[]
            ), patch.object(event, "wait", side_effect=stop_after_empty_poll):
                bot.polling_loop()
            self.assertEqual(get_offset.call_count, 2)
        finally:
            event.clear()

    def test_health_endpoint_stays_live_when_database_is_unavailable(self):
        with patch.object(bot, "db_queue_stats", side_effect=bot.sqlite3.OperationalError("db offline")):
            response = bot.app.test_client().get("/health")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(payload["database_connected"])

    def test_audio_messages_are_ignored(self):
        update = {
            "message": {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123},
                "voice": {"file_id": "audio-file-id"},
            }
        }
        self.assertIsNone(bot.extract_message(update))

    def test_reply_context_is_preserved_for_ai(self):
        update = {
            "message": {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123, "username": "regular_user"},
                "text": "منظورت چیه؟",
                "reply_to_message": {"text": "این پیام اصلی ربات است"},
            }
        }
        chat_id, encoded = bot.extract_message(update)
        self.assertEqual(chat_id, "123")
        with patch.object(bot, "call_ai", return_value="پاسخ آزمایشی") as ai:
            result = bot.build_response_plan(chat_id, encoded)
        ai.assert_called_once_with(
            "123", "منظورت چیه؟", reply_context="این پیام اصلی ربات است"
        )
        self.assertEqual(result["history_user"], "منظورت چیه؟")
        self.assertEqual(result["responses"][0][0], "123")

    def test_weather_location_parsing(self):
        self.assertEqual(bot._weather_location("هواشناسی کاشمر"), "کاشمر")
        self.assertEqual(bot._weather_location("هوا در مشهد چطوره؟"), "مشهد")

    def test_weather_province_prefix_is_removed_before_geocoding(self):
        geocoding = {
            "results": [{
                "name": "Mashhad",
                "country": "Iran",
                "country_code": "IR",
                "latitude": 36.2972,
                "longitude": 59.6067,
            }]
        }
        forecast = {
            "current": {
                "temperature_2m": 12,
                "apparent_temperature": 11,
                "relative_humidity_2m": 40,
                "wind_speed_10m": 8,
                "time": "2026-11-28T14:00",
            },
            "daily": {
                "temperature_2m_min": [7],
                "temperature_2m_max": [15],
                "precipitation_probability_max": [10],
            },
        }
        with patch.object(bot, "_get_json", side_effect=[geocoding, forecast]) as provider:
            result = bot.get_live_weather("هواشناسی استان خراسان رضوی")

        self.assertIn("Mashhad", result)
        self.assertEqual(
            provider.call_args_list[0].kwargs["params"]["name"],
            "مشهد",
        )

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
        self.assertEqual(provider.call_args_list[1].kwargs["params"]["name"], "Bushehr")

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
                "delivery_outbox",
                "processed_updates",
                "dead_letters",
                "conversation_history",
                "feedback_pending",
                "bot_state",
            ):
                db.execute(f"DELETE FROM {table}")

    def test_dead_letter_can_be_listed_and_safely_requeued(self):
        self.assertTrue(bot.db_enqueue_updates([(3101, "chat-dead", "retry me")], 3102))
        item = bot.db_claim_next_message("dead-worker")
        bot.db_dead_letter(item["id"], "dead-worker", "temporary processing failure")
        listed = bot.db_list_dead_letters()
        self.assertEqual(len(listed), 1)
        dead_id = listed[0]["id"]
        result = bot.db_requeue_dead_letter(dead_id)
        self.assertTrue(result["ok"])
        self.assertEqual(bot.db_list_dead_letters(), [])
        with bot.db_connect() as db:
            queued = db.execute(
                "SELECT chat_id, message_text, status, update_id FROM message_queue WHERE id = ?",
                (result["queue_id"],),
            ).fetchone()
        self.assertEqual(queued["chat_id"], "chat-dead")
        self.assertEqual(queued["message_text"], "retry me")
        self.assertEqual(queued["status"], "pending")
        self.assertIsNone(queued["update_id"])
        self.assertEqual(bot.db_requeue_dead_letter(dead_id)["reason"], "not_found")

    def test_dead_letter_requeue_refuses_partial_or_uncertain_delivery(self):
        self.assertTrue(bot.db_enqueue_updates([(3103, "chat-dead", "retry me")], 3104))
        item = bot.db_claim_next_message("dead-worker")
        bot.db_prepare_response_plan(
            item["id"], "dead-worker", [{"chat_id": "chat-dead", "text": "maybe sent"}]
        )
        bot.db_mark_delivery_sending(item["id"], 0)
        bot.db_mark_delivery_uncertain(item["id"], 0, "connection reset")
        bot.db_dead_letter(item["id"], "dead-worker", "uncertain delivery")
        result = bot.db_requeue_dead_letter(bot.db_list_dead_letters()[0]["id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "delivery_ambiguous")

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

    def test_response_plan_is_atomically_written_to_outbox(self):
        self.assertTrue(bot.db_enqueue_updates([(2001, "chat-outbox", "hello")], 2002))
        item = bot.db_claim_next_message("outbox-worker")
        plan = [
            {"chat_id": "chat-outbox", "text": "first"},
            {"chat_id": "chat-outbox", "text": "second", "reply_markup": {"inline_keyboard": []}},
        ]
        self.assertTrue(bot.db_prepare_response_plan(item["id"], "outbox-worker", plan))
        first = bot.db_get_delivery_state(item["id"], 0)
        second = bot.db_get_delivery_state(item["id"], 1)
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["message_text"], "first")
        self.assertEqual(second["status"], "pending")
        self.assertEqual(bot.json.loads(second["reply_markup"]), {"inline_keyboard": []})

    def test_legacy_response_plan_migration_quarantines_ambiguous_cursor(self):
        self.assertTrue(bot.db_enqueue_updates([(2002, "chat-legacy", "hello")], 2003))
        item = bot.db_claim_next_message("legacy-worker")
        legacy_plan = [
            {"chat_id": "chat-legacy", "text": "already sent"},
            {"chat_id": "chat-legacy", "text": "possibly sent"},
            {"chat_id": "chat-legacy", "text": "not yet sent"},
        ]
        with bot.db_connect() as db:
            db.execute(
                "UPDATE message_queue SET response_plan = ?, response_index = 1 WHERE id = ?",
                (bot.json.dumps(legacy_plan), item["id"]),
            )
            db.execute("DELETE FROM delivery_outbox WHERE queue_id = ?", (item["id"],))
        bot.init_database()
        self.assertEqual(bot.db_get_delivery_state(item["id"], 0)["status"], "sent")
        self.assertEqual(bot.db_get_delivery_state(item["id"], 1)["status"], "uncertain")
        self.assertEqual(bot.db_get_delivery_state(item["id"], 2)["status"], "pending")

    def test_outbox_sent_receipt_prevents_duplicate_send_after_cursor_gap(self):
        self.assertTrue(bot.db_enqueue_updates([(2003, "chat-outbox", "hello")], 2004))
        item = bot.db_claim_next_message("outbox-worker")
        self.assertTrue(bot.db_prepare_response_plan(
            item["id"], "outbox-worker", [{"chat_id": "chat-outbox", "text": "once"}]
        ))
        self.assertTrue(bot.db_mark_delivery_sending(item["id"], 0))
        self.assertTrue(bot.db_mark_delivery_sent(item["id"], 0))
        receipt = bot.db_get_delivery_state(item["id"], 0)
        self.assertEqual(receipt["status"], "sent")
        self.assertFalse(bot.db_mark_delivery_sending(item["id"], 0))

    def test_uncertain_delivery_error_is_non_retryable(self):
        error = bot.UncertainDeliveryError("ambiguous send result")
        self.assertIsInstance(error, bot.PermanentAPIError)
        self.assertNotIsInstance(error, bot.TransientAPIError)

    def test_outbox_interrupted_delivery_is_quarantined_not_resent(self):
        self.assertTrue(bot.db_enqueue_updates([(2005, "chat-outbox", "hello")], 2006))
        item = bot.db_claim_next_message("outbox-worker")
        self.assertTrue(bot.db_prepare_response_plan(
            item["id"], "outbox-worker", [{"chat_id": "chat-outbox", "text": "maybe delivered"}]
        ))
        self.assertTrue(bot.db_mark_delivery_sending(item["id"], 0))
        bot.db_recover_interrupted_jobs()
        receipt = bot.db_get_delivery_state(item["id"], 0)
        self.assertEqual(receipt["status"], "uncertain")
        self.assertIn("interrupted", receipt["last_error"])

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

    def test_postgres_cleanup_query_has_no_unescaped_placeholder_marker(self):
        query = (
            "SELECT key, value FROM bot_state "
            "WHERE substr(key, 1, 16) = 'weather_pending_'"
        )
        self.assertNotIn("%", bot.PostgresConnection._sql(query))
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
        keyboard = start_result["reply_markup"]["keyboard"]
        self.assertEqual(len(keyboard), 2)
        self.assertEqual(keyboard[1], [{"text": "💡 ارسال نظر"}, {"text": "❔ راهنما"}])

        help_result = bot.build_response_plan("feedback-user", "/help")
        self.assertIn("راهنمای NeptonBot", help_result["responses"][0][1])
        self.assertTrue(bot.db_feedback_is_pending("feedback-user"))

        help_button = bot.build_response_plan("feedback-user", "❔ راهنما")
        self.assertIn("راهنمای NeptonBot", help_button["responses"][0][1])
        feedback_button = bot.build_response_plan("feedback-user", "💡 ارسال نظر")
        self.assertEqual(feedback_button["feedback_action"], "set")

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

    def test_health_endpoint_reports_degraded_runtime_without_failing_liveness(self):
        client = bot.app.test_client()
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "degraded")


if __name__ == "__main__":
    unittest.main()

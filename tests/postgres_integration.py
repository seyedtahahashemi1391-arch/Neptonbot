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

    def test_a_queue_outbox_and_completion_lifecycle(self):
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
        stats = bot.db_queue_stats()
        self.assertGreaterEqual(stats["outbox_sent"], 1)
        self.assertEqual(stats["outbox_pending"], 0)
        self.assertEqual(stats["processing_messages"], 0)
        history = bot.get_history(chat_id)
        self.assertEqual([entry["role"] for entry in history], ["user", "assistant"])
        self.assertEqual(bot.db_get_offset(), update_id + 1)


    def test_postgres_workers_claim_distinct_messages_concurrently(self):
        from concurrent.futures import ThreadPoolExecutor

        updates = []
        expected_chats = set()
        for index in range(8):
            chat_id = self._chat_id(f"parallel-{index}")
            expected_chats.add(chat_id)
            updates.append((self._update_id(), chat_id, f"parallel message {index}"))

        self.assertTrue(bot.db_enqueue_updates(updates, max(row[0] for row in updates) + 1))
        claimed = []
        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [
                    executor.submit(bot.db_claim_next_message, f"postgres-parallel-{index}")
                    for index in range(8)
                ]
                claimed = [future.result(timeout=20) for future in futures]

            self.assertTrue(all(item is not None for item in claimed))
            self.assertEqual(len({item["id"] for item in claimed}), 8)
            self.assertEqual({item["chat_id"] for item in claimed}, expected_chats)
            self.assertTrue(all(item["status"] == "pending" for item in claimed))
        finally:
            ids = [item["id"] for item in claimed if item is not None]
            if ids:
                with bot.db_connect() as db:
                    db.execute(
                        "DELETE FROM message_queue WHERE id IN (" +
                        ",".join("?" for _ in ids) + ")",
                        tuple(ids),
                    )

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
        self.assertTrue(
            bot.db_prepare_response_plan(
                item["id"],
                "postgres-ci-dead-worker",
                [{"chat_id": chat_id, "text": "unsent pending outbox reply"}],
                history_user="dead-letter integration message",
                history_assistant="unsent pending outbox reply",
            )
        )
        bot.db_dead_letter(item["id"], "postgres-ci-dead-worker", "integration test")

        matches = [
            entry for entry in bot.db_list_dead_letters(20)
            if entry["chat_id"] == chat_id
        ]
        self.assertEqual(len(matches), 1)
        dead_letter_id = matches[0]["id"]

        result = bot.db_requeue_dead_letter(dead_letter_id)
        self.assertTrue(result["ok"], result)
        with bot.db_connect() as db:
            orphaned_outbox = db.execute(
                "SELECT COUNT(*) AS n FROM delivery_outbox WHERE queue_id = ?",
                (item["id"],),
            ).fetchone()["n"]
        self.assertEqual(orphaned_outbox, 0)
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

        # Keep this test isolated: the requeued item is verified above, but
        # must not be claimed by the queue/outbox lifecycle test.
        with bot.db_connect() as db:
            db.execute("DELETE FROM message_queue WHERE id = ?", (result["queue_id"],))


    def test_restart_recovery_quarantines_only_in_flight_delivery(self):
        chat_id = self._chat_id("restart-recovery")
        update_id = self._update_id()
        self.assertTrue(
            bot.db_enqueue_updates(
                [(update_id, chat_id, "restart recovery message")],
                update_id + 1,
            )
        )
        item = bot.db_claim_next_message("postgres-restart-worker")
        self.assertIsNotNone(item)
        plan = [
            {"chat_id": chat_id, "text": "already sent"},
            {"chat_id": chat_id, "text": "interrupted send"},
            {"chat_id": chat_id, "text": "not yet attempted"},
        ]
        self.assertTrue(
            bot.db_prepare_response_plan(
                item["id"],
                "postgres-restart-worker",
                plan,
                history_user="restart recovery message",
                history_assistant="restart recovery reply",
            )
        )

        self.assertTrue(bot.db_mark_delivery_sending(item["id"], 0))
        self.assertTrue(bot.db_mark_delivery_sent(item["id"], 0))
        self.assertTrue(
            bot.db_advance_response(item["id"], "postgres-restart-worker", 0)
        )
        self.assertTrue(bot.db_mark_delivery_sending(item["id"], 1))

        # Simulate process restart after an external send began but before its
        # outcome was durably recorded. Recovery must not blindly resend it.
        bot.db_recover_interrupted_jobs()

        with bot.db_connect() as db:
            queue_row = db.execute(
                "SELECT status, response_index, locked_by, lease_until "
                "FROM message_queue WHERE id = ?",
                (item["id"],),
            ).fetchone()
        self.assertEqual(queue_row["status"], "pending")
        self.assertEqual(queue_row["response_index"], 1)
        self.assertIsNone(queue_row["locked_by"])
        self.assertEqual(queue_row["lease_until"], 0)

        self.assertEqual(bot.db_get_delivery_state(item["id"], 0)["status"], "sent")
        self.assertEqual(bot.db_get_delivery_state(item["id"], 1)["status"], "uncertain")
        self.assertEqual(bot.db_get_delivery_state(item["id"], 2)["status"], "pending")

        with bot.db_connect() as db:
            db.execute("DELETE FROM delivery_outbox WHERE queue_id = ?", (item["id"],))
            db.execute("DELETE FROM message_queue WHERE id = ?", (item["id"],))


    def test_runtime_lease_blocks_active_owner_and_recovers_expired_owner(self):
        previous_owner = bot._runtime_owner_id
        previous_acquired = bot._runtime_lock_acquired
        test_owner = f"postgres-lease-test-{self.suffix}"
        bot._runtime_owner_id = test_owner
        bot._runtime_lock_acquired = False
        try:
            # The production acquisition path owns creation of this table.
            self.assertTrue(bot.acquire_runtime_lock())
            now = time.time()
            with bot.db_connect() as db:
                db.execute("""
                    INSERT INTO nepton_runtime_leases(
                        lock_name, owner_id, lease_until, updated_at
                    ) VALUES('primary', ?, ?, ?)
                    ON CONFLICT(lock_name) DO UPDATE SET
                        owner_id = excluded.owner_id,
                        lease_until = excluded.lease_until,
                        updated_at = excluded.updated_at
                """, ("another-active-owner", now + 300, now))

            self.assertFalse(bot.acquire_runtime_lock())
            self.assertFalse(bot._runtime_lock_acquired)

            with bot.db_connect() as db:
                db.execute("""
                    UPDATE nepton_runtime_leases
                    SET lease_until = ?, updated_at = ?
                    WHERE lock_name = 'primary'
                """, (time.time() - 10, time.time()))

            self.assertTrue(bot.acquire_runtime_lock())
            self.assertTrue(bot._runtime_lock_acquired)
            self.assertTrue(bot._renew_runtime_lock())

            # A stale process must not delete a lease now owned by another node.
            with bot.db_connect() as db:
                db.execute("""
                    UPDATE nepton_runtime_leases
                    SET owner_id = ?, lease_until = ?
                    WHERE lock_name = 'primary'
                """, ("replacement-owner", time.time() + 300))

            # Once another owner takes over, this stale process must not renew.
            self.assertFalse(bot._renew_runtime_lock())

            bot._release_runtime_lock()
            with bot.db_connect() as db:
                row = db.execute("""
                    SELECT owner_id FROM nepton_runtime_leases
                    WHERE lock_name = 'primary'
                """).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["owner_id"], "replacement-owner")
        finally:
            bot._release_runtime_lock()
            with bot.db_connect() as db:
                db.execute(
                    "DELETE FROM nepton_runtime_leases WHERE lock_name = 'primary' "
                    "AND owner_id IN (?, ?)",
                    (test_owner, "replacement-owner"),
                )
            bot._runtime_owner_id = previous_owner
            bot._runtime_lock_acquired = previous_acquired


if __name__ == "__main__":
    unittest.main(verbosity=2)

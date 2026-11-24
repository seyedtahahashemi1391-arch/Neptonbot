import atexit
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from flask import Flask, jsonify

try:
    import fcntl
except ImportError:  # Windows development fallback; Render/Linux supports fcntl.
    fcntl = None


# =========================================================
# Configuration
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()

MODEL = os.getenv("NEPTON_MODEL", "@cf/zai-org/glm-4.7-flash").strip()
VERSION = "2.6.0"
ADMIN_USERNAME = os.getenv("NEPTON_ADMIN_USERNAME", "t91m62").strip().lstrip("@").lower()
ADMIN_USER_ID = os.getenv("NEPTON_ADMIN_USER_ID", "").strip()

# Management controls are deliberately private-chat only. No admin keyboard or
# management command is sent to a group. The group is selected from the admin DM.
BTN_PANEL = "🛠 پنل مدیریت"
BTN_STATUS = "📊 وضعیت سرویس و گروه"
BTN_SELECT_GROUP = "👥 انتخاب گروه"
BTN_PAUSE_AI = "⏸ توقف هوش مصنوعی"
BTN_ENABLE_AI = "▶️ فعال‌سازی هوش مصنوعی"
BTN_MODE_REPLY = "🎯 فقط پاسخ به ریپلای ربات"
BTN_MODE_ALL = "💬 پاسخ به همه پیام‌ها"
BTN_RESET_GROUP = "🧹 پاک‌کردن حافظه گروه"
BTN_MUTE_MEMBER = "🔇 سکوت عضو"
BTN_KICK_MEMBER = "🚪 اخراج عضو"
BTN_CONFIRM_RESET = "✅ تأیید پاک‌کردن حافظه"
BTN_CANCEL = "↩️ انصراف"
BTN_CONFIRM_KICK = "⚠️ تأیید عملیات"
BTN_CLOSE_PANEL = "❌ بستن پنل"

SOROUSH_API = f"https://api.splus.ir/bot{BOT_TOKEN}"
CLOUDFLARE_URL = (
    "https://api.cloudflare.com/client/v4/accounts/"
    f"{CLOUDFLARE_ACCOUNT_ID}/ai/v1/chat/completions"
)

DB_PATH = os.getenv("NEPTON_DB_PATH", "nepton_state.sqlite3").strip()

AI_TIMEOUT = (15, 150)
SOROUSH_TIMEOUT = (10, 35)

MAX_HISTORY_MESSAGES = 12
MAX_CONVERSATIONS = 500
HISTORY_RETENTION_DAYS = 30
MAX_MESSAGE_LENGTH = 3500
MAX_PARALLEL_CHATS = 3
MAX_QUEUED_MESSAGES = 1000

POLL_EMPTY_WAIT = 1.5
POLL_EMPTY_WAIT_MAX = 5.0
POLL_ERROR_BACKOFF_START = 2
POLL_ERROR_BACKOFF_MAX = 30

MAX_WORKER_RETRIES = 5
RETRY_BACKOFF_START = 2
RETRY_BACKOFF_MAX = 120
WORKER_IDLE_WAIT = 0.25
WORKER_LEASE_SECONDS = 420
SUPERVISOR_INTERVAL = 5

PROCESSED_UPDATE_RETENTION_DAYS = 30
DEAD_LETTER_RETENTION_DAYS = 30
DB_CLEANUP_INTERVAL = 6 * 60 * 60

FEEDBACK_COMMAND = "/feedback"
FEEDBACK_CANCEL_COMMAND = "/cancel"
FEEDBACK_COMMAND_DESCRIPTION = "ارسال نظر"
FEEDBACK_TRIGGER_TEXT = "ارسال نظر"

START_MESSAGE = (
    "سلام! من نپتون بات هستم😊👋\n"
    "برای ارسال نظر، پیشنهاد یا انتقاد دستور /feedback را بفرست. "
    "در گفت‌وگوی خصوصی می‌توانی از دکمهٔ «ارسال نظر» هم استفاده کنی."
)

CREATOR_ANSWER = (
    "من توسط نپتون ساخته شدم. "
    "آیدی سازنده‌ام در بیوگرافی من قرار دارد."
)

SYSTEM_PROMPT = (
    "تو دستیار هوش مصنوعی Nepton هستی. "
    "به زبان پیام کاربر پاسخ بده و پاسخ را دقیق، مستقیم، روشن و متناسب با سؤال بنویس. "
    "از مقدمه‌های کلیشه‌ای، تکرار سؤال و توضیحات غیرضروری پرهیز کن. "
    "برای سؤال‌های کوتاه، پاسخ کوتاه و روشن بده و فقط در صورت نیاز جزئیات اضافه کن. "
    "درستی اطلاعات را بر روانی متن مقدم بدان و هیچ واقعیت، منبع، آزمون یا تجربه‌ای را جعل نکن. "
    "اطلاعات متغیر مانند قیمت، رتبه‌بندی و وضعیت روز را بدون منبع به‌روز قطعی اعلام نکن. "
    "اگر به اطلاعات زنده دسترسی نداری، این محدودیت را صریح بگو و وضعیت فعلی را حدس نزن. "
    "اگر دربارهٔ واقعیتی مطمئن نیستی، عدم‌قطعیت را صریح بیان کن. "
    "درخواست‌هایی را که می‌توانند به آسیب واقعی، سوءاستفاده یا فعالیت غیرقانونی کمک کنند، "
    "با راهنمایی ایمن و غیرعملیاتی پاسخ بده."
)

_missing = [
    name
    for name, value in (
        ("BOT_TOKEN", BOT_TOKEN),
        ("CLOUDFLARE_ACCOUNT_ID", CLOUDFLARE_ACCOUNT_ID),
        ("CLOUDFLARE_API_TOKEN", CLOUDFLARE_API_TOKEN),
    )
    if not value
]
if _missing:
    raise RuntimeError("Missing environment variables: " + ", ".join(_missing))

if not DB_PATH:
    raise RuntimeError("NEPTON_DB_PATH cannot be empty")


# =========================================================
# Logging
# =========================================================
logging.basicConfig(
    level=os.getenv("NEPTON_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s pid=%(process)d %(threadName)s %(name)s: %(message)s",
)
logger = logging.getLogger("NeptonBot")

logger.info("Starting NeptonBot version=%s model=%s", VERSION, MODEL)


# =========================================================
# HTTP sessions (one requests.Session per thread)
# =========================================================
_thread_local = threading.local()


def _new_session():
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=4,
        pool_maxsize=4,
        max_retries=0,
        pool_block=True,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": f"NeptonBot/{VERSION}",
        "Accept": "application/json",
    })
    return session


def get_http_session(name):
    session = getattr(_thread_local, name, None)
    if session is None:
        session = _new_session()
        setattr(_thread_local, name, session)
    return session


# =========================================================
# SQLite database
# =========================================================
_db_init_lock = threading.Lock()


@contextmanager
def db_connect():
    connection = sqlite3.connect(
        DB_PATH,
        timeout=30,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = FULL")
    try:
        yield connection
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _table_columns(db, table_name):
    return {
        row["name"]
        for row in db.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _add_column_if_missing(db, table_name, column_name, definition):
    if column_name not in _table_columns(db, table_name):
        db.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
        )


def init_database():
    with _db_init_lock:
        db_parent = Path(DB_PATH).expanduser().resolve().parent
        db_parent.mkdir(parents=True, exist_ok=True)

        with db_connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA wal_autocheckpoint = 1000")

            db.execute("""
                CREATE TABLE IF NOT EXISTS message_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    update_id INTEGER,
                    chat_id TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    sender_id TEXT,
                    sender_username TEXT,
                    chat_type TEXT,
                    chat_title TEXT,
                    reply_to_bot INTEGER NOT NULL DEFAULT 0,
                    is_private INTEGER NOT NULL DEFAULT 0,
                    queued_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retry_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    response_plan TEXT,
                    response_index INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0,
                    locked_by TEXT,
                    history_user TEXT,
                    history_assistant TEXT,
                    clear_history INTEGER NOT NULL DEFAULT 0,
                    feedback_action TEXT
                )
            """)

            # Safe, additive migrations from the 2.4.x database.
            migrations = (
                ("retry_at", "REAL NOT NULL DEFAULT 0"),
                ("last_error", "TEXT"),
                ("response_plan", "TEXT"),
                ("response_index", "INTEGER NOT NULL DEFAULT 0"),
                ("lease_until", "REAL NOT NULL DEFAULT 0"),
                ("locked_by", "TEXT"),
                ("history_user", "TEXT"),
                ("history_assistant", "TEXT"),
                ("clear_history", "INTEGER NOT NULL DEFAULT 0"),
                ("feedback_action", "TEXT"),
                ("sender_id", "TEXT"),
                ("sender_username", "TEXT"),
                ("chat_type", "TEXT"),
                ("chat_title", "TEXT"),
                ("reply_to_bot", "INTEGER NOT NULL DEFAULT 0"),
                ("is_private", "INTEGER NOT NULL DEFAULT 0"),
            )
            for column_name, definition in migrations:
                _add_column_if_missing(
                    db, "message_queue", column_name, definition
                )

            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_queue_status_retry_id
                ON message_queue(status, retry_at, id)
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_queue_chat_id
                ON message_queue(chat_id, id)
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS processed_updates (
                    update_id INTEGER PRIMARY KEY,
                    processed_at REAL NOT NULL
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS feedback_pending (
                    chat_id TEXT PRIMARY KEY
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS known_chats (
                    chat_id TEXT PRIMARY KEY,
                    chat_type TEXT NOT NULL,
                    title TEXT,
                    username TEXT,
                    last_seen REAL NOT NULL
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS known_members (
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    username TEXT,
                    display_name TEXT,
                    last_seen REAL NOT NULL,
                    PRIMARY KEY(chat_id, user_id)
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS group_settings (
                    chat_id TEXT PRIMARY KEY,
                    ai_enabled INTEGER NOT NULL DEFAULT 1,
                    response_mode TEXT NOT NULL DEFAULT 'all',
                    updated_at REAL NOT NULL
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS conversation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_history_chat_id
                ON conversation_history(chat_id, id)
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS dead_letters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_queue_id INTEGER,
                    update_id INTEGER,
                    chat_id TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    response_plan TEXT,
                    response_index INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL,
                    last_error TEXT,
                    failed_at REAL NOT NULL
                )
            """)

            db.execute("PRAGMA user_version = 6")

    logger.info("SQLite initialized at %s", str(Path(DB_PATH).resolve()))


def db_get_state(key, default=None):
    with db_connect() as db:
        row = db.execute(
            "SELECT value FROM bot_state WHERE key = ?",
            (key,),
        ).fetchone()
    return row["value"] if row else default


def db_set_state(key, value):
    with db_connect() as db:
        db.execute("""
            INSERT INTO bot_state(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))


def db_get_offset():
    value = db_get_state("polling_offset")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        logger.exception("Invalid saved polling offset")
        return None


def db_get_admin_chat_id():
    return db_get_state("admin_chat_id")


def db_set_admin_chat_id(chat_id):
    if chat_id is None:
        return False
    db_set_state("admin_chat_id", str(chat_id))
    return True


def db_feedback_is_pending(chat_id):
    with db_connect() as db:
        row = db.execute(
            "SELECT 1 FROM feedback_pending WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone()
    return row is not None



def db_list_known_groups():
    with db_connect() as db:
        rows = db.execute("""
            SELECT chat_id, chat_type, title, username, last_seen
            FROM known_chats
            WHERE chat_type NOT IN ('private', 'personal', 'user', 'channel')
            ORDER BY last_seen DESC
            LIMIT 100
        """).fetchall()
    return [dict(row) for row in rows]


def db_get_known_group(chat_id):
    with db_connect() as db:
        row = db.execute(
            "SELECT chat_id, chat_type, title, username, last_seen FROM known_chats WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone()
    return dict(row) if row else None


def db_get_group_settings(chat_id):
    with db_connect() as db:
        row = db.execute(
            "SELECT ai_enabled, response_mode FROM group_settings WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone()
    if row is None:
        return {"ai_enabled": True, "response_mode": "all"}
    mode = row["response_mode"] if row["response_mode"] in {"all", "reply"} else "all"
    return {"ai_enabled": bool(row["ai_enabled"]), "response_mode": mode}


def db_set_group_settings(chat_id, ai_enabled=None, response_mode=None):
    current = db_get_group_settings(chat_id)
    enabled = current["ai_enabled"] if ai_enabled is None else bool(ai_enabled)
    mode = current["response_mode"] if response_mode is None else str(response_mode)
    if mode not in {"all", "reply"}:
        raise ValueError("Invalid group response mode")
    with db_connect() as db:
        db.execute("""
            INSERT INTO group_settings(chat_id, ai_enabled, response_mode, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                ai_enabled = excluded.ai_enabled,
                response_mode = excluded.response_mode,
                updated_at = excluded.updated_at
        """, (str(chat_id), 1 if enabled else 0, mode, time.time()))
    return {"ai_enabled": enabled, "response_mode": mode}


def db_recover_interrupted_jobs():
    """Recover claimed rows only after this process owns the singleton runtime lock."""
    with db_connect() as db:
        cursor = db.execute("""
            UPDATE message_queue
            SET status = 'pending',
                retry_at = 0,
                lease_until = 0,
                locked_by = NULL
            WHERE status = 'processing'
        """)
    if cursor.rowcount:
        logger.warning("Recovered %s interrupted queue item(s)", cursor.rowcount)

def db_queue_stats():
    now = time.time()
    with db_connect() as db:
        rows = db.execute(
            "SELECT status, COUNT(*) AS n FROM message_queue GROUP BY status"
        ).fetchall()
        oldest = db.execute(
            "SELECT MIN(queued_at) AS oldest FROM message_queue"
        ).fetchone()["oldest"]
        dead_count = db.execute(
            "SELECT COUNT(*) AS n FROM dead_letters"
        ).fetchone()["n"]
    by_status = {row["status"]: int(row["n"]) for row in rows}
    queued = sum(by_status.values())
    age = max(0, now - oldest) if oldest is not None else 0
    return {
        "queued_messages": queued,
        "pending_messages": by_status.get("pending", 0),
        "processing_messages": by_status.get("processing", 0),
        "oldest_queue_age_seconds": round(age, 1),
        "dead_letters": int(dead_count),
    }


def db_enqueue_updates(prepared_updates, next_offset):
    """Persist updates and sender metadata atomically, with deduplication."""
    with db_connect() as db:
        db.execute("BEGIN IMMEDIATE")

        current_count = db.execute(
            "SELECT COUNT(*) AS total FROM message_queue"
        ).fetchone()["total"]

        unique_updates = []
        seen_in_batch = set()

        for item in prepared_updates:
            if isinstance(item, dict):
                update_id = item.get("update_id")
                chat_id = item.get("chat_id")
                message_text = item.get("message_text", "")
                sender_id = item.get("sender_id")
                sender_username = item.get("sender_username")
                chat_type = item.get("chat_type") or "group"
                chat_title = item.get("chat_title")
                reply_to_bot = bool(item.get("reply_to_bot", False))
                is_private = bool(item.get("is_private", False))
            else:
                values = tuple(item)
                if len(values) >= 9:
                    (update_id, chat_id, message_text, sender_id, sender_username,
                     chat_type, chat_title, reply_to_bot, is_private) = values[:9]
                elif len(values) >= 3:
                    update_id, chat_id, message_text = values[:3]
                    sender_id = sender_username = chat_title = None
                    chat_type = "group"
                    reply_to_bot = False
                    is_private = False
                else:
                    logger.warning("Ignoring malformed prepared update")
                    continue

            if chat_id is None or not isinstance(message_text, str):
                continue
            if update_id is not None:
                if update_id in seen_in_batch:
                    continue
                seen_in_batch.add(update_id)
                exists = db.execute(
                    "SELECT 1 FROM processed_updates WHERE update_id = ?",
                    (update_id,),
                ).fetchone()
                if exists:
                    continue

            unique_updates.append((
                update_id, str(chat_id), message_text,
                str(sender_id) if sender_id is not None else None,
                sender_username, str(chat_type or ("private" if is_private else "group")),
                chat_title, 1 if reply_to_bot else 0, 1 if is_private else 0,
            ))

        if current_count + len(unique_updates) > MAX_QUEUED_MESSAGES:
            db.rollback()
            logger.warning("Persistent message queue is full")
            return False

        now = time.time()
        for (update_id, chat_id, message_text, sender_id, sender_username,
             chat_type, chat_title, reply_to_bot, is_private) in unique_updates:
            db.execute("""
                INSERT INTO message_queue
                    (update_id, chat_id, message_text, sender_id, sender_username,
                     chat_type, chat_title, reply_to_bot, is_private, queued_at,
                     attempts, status, retry_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', 0)
            """, (
                update_id, chat_id, message_text, sender_id, sender_username,
                chat_type, chat_title, reply_to_bot, is_private, now,
            ))

            if not is_private:
                db.execute("""
                    INSERT INTO known_chats(chat_id, chat_type, title, username, last_seen)
                    VALUES(?, ?, ?, NULL, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        chat_type = excluded.chat_type,
                        title = COALESCE(excluded.title, known_chats.title),
                        last_seen = excluded.last_seen
                """, (chat_id, chat_type, chat_title, now))
                if sender_id is not None:
                    db.execute("""
                        INSERT INTO known_members(chat_id, user_id, username, display_name, last_seen)
                        VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(chat_id, user_id) DO UPDATE SET
                            username = COALESCE(excluded.username, known_members.username),
                            display_name = COALESCE(excluded.display_name, known_members.display_name),
                            last_seen = excluded.last_seen
                    """, (chat_id, str(sender_id), sender_username, None, now))
                db.execute("""
                    INSERT OR IGNORE INTO group_settings
                        (chat_id, ai_enabled, response_mode, updated_at)
                    VALUES(?, 1, 'all', ?)
                """, (chat_id, now))

            if update_id is not None:
                db.execute("""
                    INSERT OR IGNORE INTO processed_updates(update_id, processed_at)
                    VALUES(?, ?)
                """, (update_id, now))

        if next_offset is not None:
            db.execute("""
                INSERT INTO bot_state(key, value)
                VALUES('polling_offset', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (str(next_offset),))

        db.commit()
        return True


def _worker_id():
    return f"{os.getpid()}:{threading.get_ident()}"


def db_claim_next_message(worker_id):
    """Atomically claim the oldest eligible message while preserving per-chat order."""
    now = time.time()
    with db_connect() as db:
        db.execute("BEGIN IMMEDIATE")

        # Recover work whose owner disappeared without releasing its claim.
        db.execute("""
            UPDATE message_queue
            SET status = 'pending', locked_by = NULL, lease_until = 0
            WHERE status = 'processing' AND lease_until < ?
        """, (now,))

        row = db.execute("""
            SELECT q.*
            FROM message_queue AS q
            WHERE q.status = 'pending'
              AND q.retry_at <= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM message_queue AS earlier
                  WHERE earlier.chat_id = q.chat_id
                    AND earlier.id < q.id
              )
            ORDER BY q.id
            LIMIT 1
        """, (now,)).fetchone()

        if row is None:
            db.commit()
            return None

        changed = db.execute("""
            UPDATE message_queue
            SET status = 'processing',
                locked_by = ?,
                lease_until = ?
            WHERE id = ? AND status = 'pending'
        """, (worker_id, now + WORKER_LEASE_SECONDS, row["id"])).rowcount

        db.commit()
        return dict(row) if changed else None


def db_renew_lease(message_id, worker_id):
    with db_connect() as db:
        changed = db.execute("""
            UPDATE message_queue
            SET lease_until = ?
            WHERE id = ? AND status = 'processing' AND locked_by = ?
        """, (time.time() + WORKER_LEASE_SECONDS, message_id, worker_id)).rowcount
    return changed == 1


def db_prepare_response_plan(
    message_id,
    worker_id,
    response_plan,
    history_user=None,
    history_assistant=None,
    clear_history=False,
    feedback_action=None,
):
    serialized = json.dumps(response_plan, ensure_ascii=False, separators=(",", ":"))
    with db_connect() as db:
        changed = db.execute("""
            UPDATE message_queue
            SET response_plan = ?,
                response_index = 0,
                history_user = ?,
                history_assistant = ?,
                clear_history = ?,
                feedback_action = ?,
                lease_until = ?
            WHERE id = ? AND status = 'processing' AND locked_by = ?
        """, (
            serialized,
            history_user,
            history_assistant,
            1 if clear_history else 0,
            feedback_action,
            time.time() + WORKER_LEASE_SECONDS,
            message_id,
            worker_id,
        )).rowcount
    return changed == 1


def db_advance_response(message_id, worker_id, expected_index):
    with db_connect() as db:
        changed = db.execute("""
            UPDATE message_queue
            SET response_index = response_index + 1,
                lease_until = ?
            WHERE id = ?
              AND status = 'processing'
              AND locked_by = ?
              AND response_index = ?
        """, (
            time.time() + WORKER_LEASE_SECONDS,
            message_id,
            worker_id,
            expected_index,
        )).rowcount
    return changed == 1


def db_finish_message(message_id, worker_id):
    with db_connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""
            SELECT * FROM message_queue
            WHERE id = ? AND status = 'processing' AND locked_by = ?
        """, (message_id, worker_id)).fetchone()

        if row is None:
            db.rollback()
            return False

        try:
            plan = json.loads(row["response_plan"] or "[]")
        except (TypeError, ValueError):
            plan = []

        if int(row["response_index"] or 0) < len(plan):
            db.rollback()
            return False

        chat_id = row["chat_id"]

        if row["clear_history"]:
            db.execute(
                "DELETE FROM conversation_history WHERE chat_id = ?",
                (chat_id,),
            )
        elif row["history_user"] is not None and row["history_assistant"] is not None:
            now = time.time()
            db.executemany("""
                INSERT INTO conversation_history(chat_id, role, content, created_at)
                VALUES(?, ?, ?, ?)
            """, (
                (chat_id, "user", row["history_user"], now),
                (chat_id, "assistant", row["history_assistant"], now),
            ))
            db.execute("""
                DELETE FROM conversation_history
                WHERE chat_id = ? AND id NOT IN (
                    SELECT id FROM conversation_history
                    WHERE chat_id = ? ORDER BY id DESC LIMIT ?
                )
            """, (chat_id, chat_id, MAX_HISTORY_MESSAGES))

        if row["feedback_action"] == "set":
            db.execute(
                "INSERT OR IGNORE INTO feedback_pending(chat_id) VALUES(?)",
                (chat_id,),
            )
        elif row["feedback_action"] == "clear":
            db.execute(
                "DELETE FROM feedback_pending WHERE chat_id = ?",
                (chat_id,),
            )

        # Bound history storage by both age and number of active chats.
        history_cutoff = time.time() - HISTORY_RETENTION_DAYS * 86400
        db.execute(
            "DELETE FROM conversation_history WHERE created_at < ?",
            (history_cutoff,),
        )
        db.execute("""
            DELETE FROM conversation_history
            WHERE chat_id NOT IN (
                SELECT chat_id FROM conversation_history
                GROUP BY chat_id
                ORDER BY MAX(created_at) DESC
                LIMIT ?
            )
        """, (MAX_CONVERSATIONS,))

        db.execute("DELETE FROM message_queue WHERE id = ?", (message_id,))
        db.commit()
        return True


def db_retry_message(message_id, worker_id, attempts, error_text=None):
    delay = retry_delay(attempts)
    with db_connect() as db:
        changed = db.execute("""
            UPDATE message_queue
            SET status = 'pending',
                attempts = attempts + 1,
                retry_at = ?,
                last_error = ?,
                lease_until = 0,
                locked_by = NULL
            WHERE id = ? AND status = 'processing' AND locked_by = ?
        """, (
            time.time() + delay,
            (error_text or "")[:500],
            message_id,
            worker_id,
        )).rowcount

    if changed:
        logger.warning(
            "Queue item scheduled for retry id=%s delay=%.1fs",
            message_id,
            delay,
        )
    return changed == 1


def db_dead_letter(message_id, worker_id, reason):
    with db_connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""
            SELECT * FROM message_queue
            WHERE id = ? AND status = 'processing' AND locked_by = ?
        """, (message_id, worker_id)).fetchone()
        if row is None:
            db.rollback()
            return False

        db.execute("""
            INSERT INTO dead_letters(
                original_queue_id, update_id, chat_id, message_text,
                response_plan, response_index, attempts, last_error, failed_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["id"],
            row["update_id"],
            row["chat_id"],
            row["message_text"],
            row["response_plan"],
            row["response_index"],
            row["attempts"],
            (reason or row["last_error"] or "retry limit reached")[:500],
            time.time(),
        ))
        db.execute("DELETE FROM message_queue WHERE id = ?", (message_id,))
        db.commit()
        logger.error("Queue item moved to dead-letter storage id=%s", message_id)
        return True


def retry_delay(attempts):
    exponent = min(max(0, int(attempts)), 10)
    base = min(RETRY_BACKOFF_START * (2 ** exponent), RETRY_BACKOFF_MAX)
    return base + random.uniform(0, min(base * 0.25, 5))


def db_cleanup_old_records():
    now = time.time()
    processed_cutoff = now - PROCESSED_UPDATE_RETENTION_DAYS * 86400
    dead_cutoff = now - DEAD_LETTER_RETENTION_DAYS * 86400
    history_cutoff = now - HISTORY_RETENTION_DAYS * 86400

    with db_connect() as db:
        db.execute(
            "DELETE FROM processed_updates WHERE processed_at < ?",
            (processed_cutoff,),
        )
        db.execute(
            "DELETE FROM dead_letters WHERE failed_at < ?",
            (dead_cutoff,),
        )
        db.execute(
            "DELETE FROM conversation_history WHERE created_at < ?",
            (history_cutoff,),
        )
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")


# =========================================================
# Runtime state and Flask app
# =========================================================
app = Flask(__name__)

admin_lock = threading.Lock()
admin_chat_id = None

_runtime_lock_file = None
_runtime_lock_acquired = False
_runtime_active = False

_polling_thread = None
_polling_stop = threading.Event()
_worker_threads = []
_supervisor_thread = None
_service_lock = threading.Lock()


class UserVisibleError(Exception):
    pass


class RetryableDeliveryError(Exception):
    pass


def load_persistent_runtime_state():
    global admin_chat_id
    saved_admin = db_get_admin_chat_id()
    with admin_lock:
        admin_chat_id = saved_admin


def set_admin_chat_id(chat_id):
    global admin_chat_id
    if chat_id is None:
        return False

    chat_id = str(chat_id)
    db_set_admin_chat_id(chat_id)
    with admin_lock:
        admin_chat_id = chat_id
    logger.info("Admin private chat registered")
    return True


def get_expected_admin_user_id():
    """Return the configured or first-verified immutable admin user ID."""
    return ADMIN_USER_ID or db_get_state("admin_user_id")


def register_admin_identity(sender_id, username, chat_id, is_private):
    """Bootstrap admin identity from the configured username in a private chat.

    Once an ID is stored, username changes do not transfer administrator access.
    """
    if not is_private or sender_id is None or chat_id is None:
        return False

    sender_id = str(sender_id)
    expected_id = get_expected_admin_user_id()
    if expected_id is None:
        normalized = (username or "").strip().lstrip("@").lower()
        if not ADMIN_USERNAME or normalized != ADMIN_USERNAME:
            return False
        db_set_state("admin_user_id", sender_id)
        expected_id = sender_id

    if str(expected_id) != sender_id:
        return False

    set_admin_chat_id(chat_id)
    return True


def is_admin_user(sender_id, is_private):
    if not is_private or sender_id is None:
        return False
    expected_id = get_expected_admin_user_id()
    return expected_id is not None and str(sender_id) == str(expected_id)


def get_admin_chat_id():
    with admin_lock:
        return admin_chat_id


def acquire_runtime_lock():
    """Prevent multiple Gunicorn processes from polling and processing the same bot."""
    global _runtime_lock_file, _runtime_lock_acquired

    lock_path = str(Path(DB_PATH).expanduser().resolve()) + ".runtime.lock"
    lock_parent = Path(lock_path).parent
    lock_parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")

    if fcntl is None:
        logger.warning(
            "fcntl is unavailable; process-level runtime locking is disabled. "
            "Run exactly one application worker."
        )
        _runtime_lock_file = handle
        _runtime_lock_acquired = True
        return True

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        logger.warning(
            "Another process owns the Nepton runtime lock; this process will serve HTTP only"
        )
        _runtime_lock_acquired = False
        return False

    _runtime_lock_file = handle
    _runtime_lock_acquired = True
    logger.info("Runtime lock acquired")
    return True


@app.get("/")
def home():
    return jsonify({
        "status": "online",
        "bot": "Nepton",
        "version": VERSION,
        "provider": "Cloudflare Workers AI",
        "model": MODEL,
    })


@app.get("/health")
def health():
    try:
        stats = db_queue_stats()
        polling_running = _polling_thread is not None and _polling_thread.is_alive()
        live_workers = sum(worker.is_alive() for worker in _worker_threads)
        healthy = _runtime_active and polling_running and live_workers > 0

        payload = {
            "status": "healthy" if healthy else "degraded",
            "runtime_owner": _runtime_active,
            "runtime_lock_acquired": _runtime_lock_acquired,
            "polling": "running" if polling_running else "stopped",
            "workers_alive": live_workers,
            "workers_expected": MAX_PARALLEL_CHATS,
            "version": VERSION,
            "model": MODEL,
        }
        payload.update(stats)
        return jsonify(payload), 200 if healthy else 503
    except Exception:
        logger.exception("Health check failed")
        return jsonify({"status": "unhealthy", "version": VERSION}), 503


# =========================================================
# Conversation history
# =========================================================
def get_history(chat_id):
    with db_connect() as db:
        rows = db.execute("""
            SELECT role, content
            FROM conversation_history
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (str(chat_id), MAX_HISTORY_MESSAGES)).fetchall()
    return [
        {"role": row["role"], "content": row["content"]}
        for row in reversed(rows)
    ]


def reset_history(chat_id):
    with db_connect() as db:
        db.execute(
            "DELETE FROM conversation_history WHERE chat_id = ?",
            (str(chat_id),),
        )


# =========================================================
# Cloudflare Workers AI
# =========================================================
_cloudflare_headers = {
    "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def cloudflare_post_with_retry(session, payload):
    """One bounded retry for transient transport/server failures."""
    last_exception = None

    for attempt in range(2):
        try:
            response = session.post(
                CLOUDFLARE_URL,
                headers=_cloudflare_headers,
                json=payload,
                timeout=AI_TIMEOUT,
                allow_redirects=False,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exception = exc
            if attempt == 0:
                logger.warning("Temporary Cloudflare transport failure; retrying once")
                time.sleep(0.4 + random.uniform(0, 0.4))
                continue
            raise

        if response.status_code in (500, 502, 503, 504) and attempt == 0:
            status = response.status_code
            response.close()
            logger.warning("Cloudflare returned HTTP %s; retrying once", status)
            time.sleep(0.4 + random.uniform(0, 0.4))
            continue

        return response

    raise requests.exceptions.ConnectionError("Cloudflare request failed") from last_exception


def _extract_ai_text(data):
    if not isinstance(data, dict):
        return ""

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""

    first = choices[0]
    if not isinstance(first, dict):
        return ""

    message = first.get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                part = item["text"].strip()
                if part:
                    parts.append(part)
        return "\n".join(parts).strip()

    return ""


def call_ai(chat_id, user_text):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(get_history(chat_id))
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.4,
        "max_completion_tokens": 1536,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": False,
    }

    started = time.perf_counter()
    try:
        with cloudflare_post_with_retry(
            get_http_session("cloudflare_session"), payload
        ) as response:
            if not 200 <= response.status_code < 300:
                status = response.status_code
                logger.error("Cloudflare HTTP error status=%s", status)

                if status == 429:
                    raise UserVisibleError(
                        "تعداد درخواست‌ها به سرویس هوش مصنوعی زیاد شده است. کمی بعد دوباره تلاش کن."
                    )
                if status in (401, 403):
                    raise UserVisibleError(
                        "دسترسی سرویس هوش مصنوعی با مشکل روبه‌رو شده است. مدیر باید تنظیمات اتصال را بررسی کند."
                    )
                raise UserVisibleError(
                    "سرویس هوش مصنوعی در حال حاضر پاسخ‌گوی درخواست نیست. کمی بعد دوباره تلاش کن."
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise UserVisibleError(
                    "پاسخ سرویس هوش مصنوعی قابل پردازش نبود."
                ) from exc

    except UserVisibleError:
        raise
    except requests.exceptions.Timeout as exc:
        raise UserVisibleError(
            "زمان پاسخ‌گویی هوش مصنوعی بیش از حد طول کشید. دوباره تلاش کن."
        ) from exc
    except requests.exceptions.RequestException as exc:
        logger.error("Cloudflare network error type=%s", type(exc).__name__)
        raise UserVisibleError(
            "ارتباط با سرویس هوش مصنوعی برقرار نشد. کمی بعد دوباره تلاش کن."
        ) from exc
    finally:
        logger.info(
            "TIMING cloudflare_request_ms=%.0f",
            (time.perf_counter() - started) * 1000,
        )

    answer = _extract_ai_text(data)
    if not answer:
        raise UserVisibleError(
            "مدل این بار پاسخ متنی تولید نکرد. لطفاً دوباره تلاش کن."
        )
    return answer


# =========================================================
# Soroush Plus API
# =========================================================
def soroush_request(method, payload=None):
    session = get_http_session("soroush_session")
    try:
        with session.post(
            f"{SOROUSH_API}/{method}",
            json=payload or {},
            timeout=SOROUSH_TIMEOUT,
            allow_redirects=False,
        ) as response:
            if not 200 <= response.status_code < 300:
                logger.error(
                    "Soroush HTTP error method=%s status=%s",
                    method,
                    response.status_code,
                )
                return None

            try:
                data = response.json()
            except ValueError:
                logger.error("Soroush returned invalid JSON method=%s", method)
                return None

            if isinstance(data, dict) and data.get("ok") is False:
                logger.warning("Soroush API rejected method=%s", method)
            return data

    except requests.exceptions.RequestException as exc:
        logger.error(
            "Soroush network error method=%s type=%s",
            method,
            type(exc).__name__,
        )
        return None


def api_result_is_success(result):
    if result is None:
        return False
    if isinstance(result, dict) and "ok" in result:
        return result.get("ok") is True
    # Some Soroush-compatible responses may omit the Telegram-style `ok` key.
    return isinstance(result, (dict, list))


def register_bot_commands():
    result = soroush_request(
        "setMyCommands",
        {
            "commands": [{
                "command": "feedback",
                "description": FEEDBACK_COMMAND_DESCRIPTION,
            }]
        },
    )

    if not api_result_is_success(result):
        logger.warning(
            "Could not register bot commands; /feedback remains usable by text"
        )
        return False

    logger.info("Bot commands registered")
    return True


def split_message(text, limit=MAX_MESSAGE_LENGTH):
    """Split long text near line boundaries without splitting Unicode characters."""
    if not isinstance(text, str) or not text:
        return []

    chunks = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < int(limit * 0.55):
            split_at = limit
        else:
            split_at += 1
        chunk = remaining[:split_at]
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:]

    if remaining:
        chunks.append(remaining)
    return chunks


def send_one_message(chat_id, text, reply_markup=None, reply_to_message_id=None):
    if not isinstance(text, str) or not text:
        return False

    payload = {"chat_id": str(chat_id), "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id

    started = time.perf_counter()
    try:
        result = soroush_request("sendMessage", payload)
        success = api_result_is_success(result)
        if not success:
            logger.error("Soroush sendMessage failed or was rejected")
        return success
    finally:
        logger.info(
            "TIMING send_message_ms=%.0f",
            (time.perf_counter() - started) * 1000,
        )


# =========================================================
# Update parsing and admin identification
# =========================================================
def extract_updates(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []

    for key in ("result", "updates", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested_key in ("updates", "messages", "result"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return nested
    return []


def has_update_container(data):
    if isinstance(data, list):
        return True
    if not isinstance(data, dict) or data.get("ok") is False:
        return False

    for key in ("result", "updates", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return True
        if isinstance(value, dict) and any(
            isinstance(value.get(k), list)
            for k in ("updates", "messages", "result")
        ):
            return True
    return False


def _as_identifier(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def extract_message(update):
    if not isinstance(update, dict):
        return None

    message = (
        update.get("message")
        or update.get("edited_message")
        or update.get("new_message")
        or update.get("channel_post")
        or update
    )
    if not isinstance(message, dict):
        return None

    message_text = (
        message.get("text")
        or message.get("body")
        or message.get("message_text")
    )
    chat = message.get("chat")
    sender = (
        message.get("from") or message.get("from_user")
        or message.get("sender") or message.get("user")
    )

    chat_id = chat.get("id") if isinstance(chat, dict) else message.get("chat_id")
    sender_id = sender.get("id") if isinstance(sender, dict) else None
    if sender_id is None:
        sender_id = message.get("sender_id") or message.get("from_id")
    if chat_id is None and sender_id is not None:
        chat_id = sender_id

    if chat_id is None or message_text is None:
        return None
    if not isinstance(message_text, str):
        message_text = str(message_text)

    username = sender.get("username") if isinstance(sender, dict) else None
    normalized_username = (
        username.strip().lstrip("@").lower() if isinstance(username, str) else None
    )

    chat_type = chat.get("type") if isinstance(chat, dict) else None
    if chat_type is None:
        chat_type = message.get("chat_type")
    chat_type = str(chat_type).strip().lower() if chat_type is not None else None

    if chat_type is not None:
        is_private = chat_type in {"private", "personal", "user"}
    else:
        is_private = sender_id is not None and str(sender_id) == str(chat_id)
        chat_type = "private" if is_private else "group"

    # In private dialogs the peer ID is the best available fallback if the
    # update format omits the sender object. Group messages never use this fallback.
    if is_private and sender_id is None:
        sender_id = chat_id

    chat_title = None
    if isinstance(chat, dict):
        chat_title = chat.get("title") or chat.get("name")
    if not chat_title and not is_private and isinstance(message, dict):
        chat_title = message.get("chat_title") or message.get("group_title")

    reply_to_bot = False
    reply_obj = message.get("reply_to_message") or message.get("reply_to")
    if isinstance(reply_obj, dict):
        reply_sender = (
            reply_obj.get("from") or reply_obj.get("from_user")
            or reply_obj.get("sender") or reply_obj.get("user")
        )
        if isinstance(reply_sender, dict):
            reply_to_bot = bool(
                reply_sender.get("is_bot")
                or reply_sender.get("isBot")
                or str(reply_sender.get("type", "")).lower() == "bot"
            )

    register_admin_identity(sender_id, normalized_username, chat_id, is_private)

    return (
        str(chat_id), message_text,
        str(sender_id) if sender_id is not None else None,
        normalized_username, chat_type, chat_title, bool(reply_to_bot), bool(is_private),
    )


def get_update_id(update):
    if not isinstance(update, dict):
        return None
    for key in ("update_id", "updateId", "id"):
        value = _as_identifier(update.get(key))
        if value is not None:
            return value
    return None


# =========================================================
# Text normalization and deterministic replies
# =========================================================
def normalize_text(text):
    normalized = str(text).strip().lower()
    normalized = normalized.replace("ي", "ی").replace("ك", "ک")
    normalized = normalized.replace("ۀ", "ه").replace("ة", "ه")
    normalized = normalized.replace("ؤ", "و").replace("إ", "ا").replace("أ", "ا")
    normalized = normalized.replace("\u200c", " ")
    normalized = re.sub(r"[؟?!.,،؛:]+", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def is_creator_question(user_text):
    normalized = normalize_text(user_text)
    exact_phrases = {
        "سازندت کیه",
        "سا

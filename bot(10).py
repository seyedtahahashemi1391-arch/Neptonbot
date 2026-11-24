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
VERSION = "2.7.0"
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

# Group inline control panel: visible to all, executable only by owner ID.
GROUP_PANEL_TRIGGER = "پنل نپتون"
GROUP_PANEL_PREFIX = "__NEPTON_GROUP_PANEL__:"
GROUP_PANEL_CALLBACKS = {
    "np:status", "np:toggle_ai", "np:mode_all", "np:mode_reply",
    "np:reset_confirm", "np:reset_execute", "np:reset_cancel",
    "np:help", "np:close",
}

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


def is_owner_user(sender_id):
    """Check the immutable owner ID in any chat; never authorize by username."""
    if sender_id is None:
        return False
    expected_id = get_expected_admin_user_id()
    return expected_id is not None and str(sender_id) == str(expected_id)


def is_admin_user(sender_id, is_private):
    """The private admin panel is restricted to the owner in a private chat."""
    return bool(is_private and is_owner_user(sender_id))


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
        "سازنده ات کیه",
        "سازنده تو کیه",
        "سازنده تو چیه",
        "چه کسی ساختت",
        "کی ساختت",
        "تو رو کی ساخته",
        "تورو کی ساخته",
        "توسعه دهنده ات کیه",
        "توسعه دهنده تو کیه",
        "چه کسی تو را ساخته",
        "چه کسی تو رو ساخته",
        "تو توسط چه کسی ساخته شدی",
        "سازنده تو چه کسیه",
    }
    if normalized in exact_phrases:
        return True

    patterns = (
        r"^(سازنده|سازند) .{0,18} کیه$",
        r"^(چه کسی|کی) .{0,10} (ساختت|ساخته|ساخته است)$",
        r"^(توسعه دهنده) .{0,18} کیه$",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def get_fixed_conversation_answer(user_text):
    normalized = normalize_text(user_text)
    answers = {
        "سلام": "سلام! چطور می‌تونم کمکت کنم؟",
        "درود": "درود! چطور می‌تونم کمکت کنم؟",
        "سلام خوبی": "سلام! خوبم، ممنون. تو چطوری؟",
        "سلام چطوری": "سلام! خوبم، ممنون. تو چطوری؟",
        "چطوری": "خوبم، ممنون! تو چطوری؟",
        "خوبی": "خوبم، ممنون! تو چطوری؟",
        "چه خبر": "سلام! خبر خاصی نیست؛ چه کمکی از دستم برمیاد؟",
        "چخبر": "سلام! خبر خاصی نیست؛ چه کمکی از دستم برمیاد؟",
        "صبح بخیر": "صبح بخیر! روز خوبی داشته باشی. چطور می‌تونم کمکت کنم؟",
        "شب بخیر": "شب بخیر! شبت خوش. کاری از دستم برمیاد؟",
    }
    return answers.get(normalized)


# =========================================================
# Durable feedback workflow
# =========================================================
def process_feedback_message(chat_id, text):
    stripped = text.strip()
    normalized = normalize_text(stripped)
    first_token = stripped.split(maxsplit=1)[0] if stripped else ""
    command = first_token.split("@", 1)[0].lower() if first_token else ""
    is_pending = db_feedback_is_pending(chat_id)

    if command == FEEDBACK_CANCEL_COMMAND:
        if is_pending:
            return {
                "responses": [(chat_id, "ارسال نظر لغو شد.")],
                "feedback_action": "clear",
            }
        return {
            "responses": [(chat_id, "در حال حاضر در مرحلهٔ ارسال نظر نیستی.")],
            "feedback_action": None,
        }

    if command == FEEDBACK_COMMAND or normalized == normalize_text(FEEDBACK_TRIGGER_TEXT):
        return {
            "responses": [(
                chat_id,
                "متن نظر یا پیشنهادت را در پیام بعدی بفرست.\n"
                "برای لغو، /cancel را ارسال کن.",
            )],
            "feedback_action": "set",
        }

    if not is_pending:
        return None

    if not stripped:
        return {
            "responses": [(chat_id, "متن نظر خالی است؛ لطفاً دوباره بفرست.")],
            "feedback_action": None,
        }

    if len(stripped) > MAX_MESSAGE_LENGTH:
        return {
            "responses": [(
                chat_id,
                f"متن نظر طولانی است. لطفاً آن را به حداکثر {MAX_MESSAGE_LENGTH} نویسه کاهش بده.",
            )],
            "feedback_action": None,
        }

    admin_id = get_admin_chat_id()
    if admin_id is None:
        return {
            "responses": [(
                chat_id,
                "صندوق پیشنهادها هنوز برای مدیر فعال نشده است. "
                "مدیر باید ابتدا در گفت‌وگوی خصوصی به ربات پیام بدهد؛ سپس دوباره تلاش کن.",
            )],
            "feedback_action": None,
        }

    admin_message = (
        "نظر یا پیشنهاد جدید\n"
        f"شناسهٔ فرستنده: {chat_id}\n\n"
        f"{stripped}"
    )

    # Admin delivery is planned before user confirmation. The durable response
    # cursor prevents resending the admin message after it is acknowledged.
    return {
        "responses": [
            (admin_id, admin_message),
            (chat_id, "نظر تو برای مدیر ارسال شد. ممنون از بازخوردت."),
        ],
        "feedback_action": "clear",
    }


# =========================================================
# Build a durable response plan (no network sends in this stage)
# =========================================================
def _reply_keyboard(rows, placeholder=None):
    markup = {
        "keyboard": [[{"text": str(label)} for label in row] for row in rows if row],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }
    if placeholder:
        markup["input_field_placeholder"] = placeholder[:64]
    return markup


def _admin_main_keyboard(selected_group_id=None):
    rows = [
        [BTN_PANEL, BTN_SELECT_GROUP],
        [BTN_STATUS],
    ]
    if selected_group_id:
        settings = db_get_group_settings(selected_group_id)
        rows.append([BTN_PAUSE_AI if settings["ai_enabled"] else BTN_ENABLE_AI])
        rows.append([BTN_RESET_GROUP])
        rows.append([BTN_MUTE_MEMBER, BTN_KICK_MEMBER])
        rows.append([BTN_MODE_REPLY if settings["response_mode"] == "all" else BTN_MODE_ALL])
    rows.append([BTN_CLOSE_PANEL])
    return _reply_keyboard(rows, "فرمان مدیریتی را انتخاب کن")


def _admin_group_picker_keyboard(groups):
    rows = []
    for group in groups:
        title = (group.get("title") or "گروه بدون نام").strip()
        if len(title) > 34:
            title = title[:31] + "…"
        label = f"👥 {title} | {group['chat_id']}"
        rows.append([label])
    rows.append(["↩️ بازگشت به پنل"])
    return _reply_keyboard(rows, "گروه موردنظر را انتخاب کن")


def _admin_selected_group_id():
    value = db_get_state("admin_selected_group_id")
    return str(value) if value else None


def _admin_panel_message(chat_id, extra_text=None):
    selected_id = _admin_selected_group_id()
    if selected_id:
        group = db_get_known_group(selected_id)
        if group:
            title = group.get("title") or f"گروه {selected_id}"
            settings = db_get_group_settings(selected_id)
            ai_state = "روشن" if settings["ai_enabled"] else "خاموش"
            mode = "همه پیام‌ها" if settings["response_mode"] == "all" else "فقط ریپلای به ربات"
            summary = f"گروه انتخاب‌شده: {title}\nشناسه: {selected_id}\nهوش مصنوعی: {ai_state}\nحالت پاسخ‌گویی: {mode}"
        else:
            db_set_state("admin_selected_group_id", "")
            selected_id = None
            summary = "هنوز گروهی انتخاب نشده است."
    else:
        summary = "هنوز گروهی انتخاب نشده است. از دکمهٔ «انتخاب گروه» استفاده کن."

    text = "🛠 پنل مدیریت نپتون\n\n" + summary
    if extra_text:
        text = extra_text + "\n\n" + text
    return {
        "responses": [(chat_id, text)],
        "history_user": None,
        "history_assistant": None,
        "clear_history": False,
        "feedback_action": None,
        "reply_markup": _admin_main_keyboard(selected_id),
    }


def _admin_status_text(selected_group_id=None):
    stats = db_queue_stats()
    polling_running = _polling_thread is not None and _polling_thread.is_alive()
    live_workers = sum(worker.is_alive() for worker in _worker_threads)
    overall = "فعال" if _runtime_active and polling_running and live_workers > 0 else "دارای اختلال/نقص"
    lines = [
        "📊 وضعیت نپتون",
        f"وضعیت سرویس: {overall}",
        f"Polling: {'فعال' if polling_running else 'متوقف'}",
        f"Worker فعال: {live_workers}/{MAX_PARALLEL_CHATS}",
        f"صف پیام: {stats['queued_messages']} (در انتظار: {stats['pending_messages']})",
        f"پیام‌های در حال پردازش: {stats['processing_messages']}",
        f"خطاهای نهایی (Dead Letter): {stats['dead_letters']}",
        f"نسخه: {VERSION}",
    ]
    if selected_group_id:
        group = db_get_known_group(selected_group_id)
        settings = db_get_group_settings(selected_group_id)
        title = (group or {}).get("title") or f"گروه {selected_group_id}"
        lines.extend([
            "",
            f"گروه: {title}",
            f"شناسه گروه: {selected_group_id}",
            f"پاسخ‌گویی هوش مصنوعی: {'روشن' if settings['ai_enabled'] else 'خاموش'}",
            f"حالت: {'همه پیام‌ها' if settings['response_mode'] == 'all' else 'فقط ریپلای به ربات'}",
        ])
    else:
        lines.extend(["", "برای دیدن تنظیمات یک گروه، ابتدا آن را انتخاب کن."])
    return "\n".join(lines)


def _recent_group_members_keyboard(group_id, action):
    """Offer recent senders only; the Bot API does not expose a verified full member list here."""
    with db_connect() as db:
        rows = db.execute("""
            SELECT user_id AS sender_id, username AS sender_username, last_seen AS seen
            FROM known_members WHERE chat_id = ?
            ORDER BY last_seen DESC LIMIT 20
        """, (str(group_id),)).fetchall()
    buttons = []
    for row in rows:
        uid = str(row["sender_id"])
        username = (row["sender_username"] or "").strip()
        label = ("@" + username) if username else ("عضو " + uid)
        label = label[:38]
        buttons.append([f"👤 {label} | {uid}"])
    buttons.append([BTN_CANCEL])
    db_set_state("admin_moderation_action", action)
    db_set_state("admin_moderation_group_id", str(group_id))
    return _reply_keyboard(buttons, "عضو را انتخاب کن (فهرست اخیرهاست)")


def _moderation_result(chat_id, message):
    return {
        "responses": [(chat_id, message)],
        "history_user": None,
        "history_assistant": None,
        "clear_history": False,
        "feedback_action": None,
        "reply_markup": _admin_main_keyboard(_admin_selected_group_id()),
    }


def process_admin_panel_action(admin_chat_id_value, text):
    """Execute only private, already-authenticated administrator panel actions."""
    normalized = normalize_text(text)
    selected_id = _admin_selected_group_id()
    selected_group = db_get_known_group(selected_id) if selected_id else None

    if normalized in {"/start", "/panel", normalize_text(BTN_PANEL)}:
        return _admin_panel_message(admin_chat_id_value, "پنل خصوصی آماده است.")

    if text == BTN_CLOSE_PANEL:
        return {
            "responses": [(admin_chat_id_value, "پنل مدیریت بسته شد. برای بازکردن دوباره، /panel را بفرست.")],
            "history_user": None,
            "history_assistant": None,
            "clear_history": False,
            "feedback_action": None,
            "reply_markup": {"remove_keyboard": True},
        }

    if text == BTN_SELECT_GROUP:
        groups = db_list_known_groups()
        if not groups:
            return {
                "responses": [(admin_chat_id_value, "هنوز گروهی شناسایی نشده است. ابتدا ربات را به گروه اضافه کن و یک پیام در آن گروه دریافت شود؛ سپس دوباره انتخاب گروه را بزن.")],
                "history_user": None,
                "history_assistant": None,
                "clear_history": False,
                "feedback_action": None,
                "reply_markup": _admin_main_keyboard(selected_id),
            }
        return {
            "responses": [(admin_chat_id_value, "گروه موردنظر را انتخاب کن:")],
            "history_user": None,
            "history_assistant": None,
            "clear_history": False,
            "feedback_action": None,
            "reply_markup": _admin_group_picker_keyboard(groups),
        }

    if text == "↩️ بازگشت به پنل":
        return _admin_panel_message(admin_chat_id_value)

    if text.startswith("👥 ") and "|" in text:
        candidate_id = text.rsplit("|", 1)[-1].strip()
        group = db_get_known_group(candidate_id)
        if group:
            db_set_state("admin_selected_group_id", candidate_id)
            db_set_state("admin_reset_confirm_group_id", "")
            title = group.get("title") or f"گروه {candidate_id}"
            return _admin_panel_message(admin_chat_id_value, f"گروه «{title}» انتخاب شد.")
        return _admin_panel_message(admin_chat_id_value, "این گروه در فهرست گروه‌های شناسایی‌شده نیست.")

    if text in {BTN_MUTE_MEMBER, BTN_KICK_MEMBER}:
        if not selected_group:
            return _admin_panel_message(admin_chat_id_value, "ابتدا یک گروه را انتخاب کن.")
        action = "mute" if text == BTN_MUTE_MEMBER else "kick"
        markup = _recent_group_members_keyboard(selected_id, action)
        return {
            "responses": [(admin_chat_id_value, "از میان افرادی که اخیراً در گروه پیام داده‌اند انتخاب کن. این فهرست کامل اعضای گروه نیست.")],
            "history_user": None, "history_assistant": None, "clear_history": False,
            "feedback_action": None, "reply_markup": markup,
        }

    if text.startswith("👤 ") and "|" in text:
        pending_group = str(db_get_state("admin_moderation_group_id") or "")
        pending_action = str(db_get_state("admin_moderation_action") or "")
        target_id = text.rsplit("|", 1)[-1].strip()
        if not selected_group or pending_group != selected_id or pending_action not in {"mute", "kick"}:
            db_set_state("admin_moderation_action", "")
            db_set_state("admin_moderation_group_id", "")
            return _admin_panel_message(admin_chat_id_value, "انتخاب عضو منقضی شده؛ دوباره عملیات را شروع کن.")
        # Confirm that this ID was actually observed in the selected group.
        with db_connect() as db:
            observed = db.execute("""
                SELECT 1 FROM known_members
                WHERE chat_id = ? AND user_id = ? LIMIT 1
            """, (selected_id, target_id)).fetchone()
        if not observed:
            return _admin_panel_message(admin_chat_id_value, "شناسهٔ عضو در پیام‌های ثبت‌شدهٔ این گروه پیدا نشد.")
        expected_admin_id = get_expected_admin_user_id()
        if expected_admin_id is not None and str(expected_admin_id) == target_id:
            return _admin_panel_message(admin_chat_id_value, "برای جلوگیری از خطا، عملیات روی حساب مدیر اصلی از این پنل مجاز نیست.")
        db_set_state("admin_moderation_target_id", target_id)
        action_label = "سکوت" if pending_action == "mute" else "اخراج"
        confirm_text = "برای چه مدت؟" if pending_action == "mute" else "اخراج این عضو را تأیید می‌کنی؟"
        rows = [["🔇 ۱۰ دقیقه", "🔇 ۱ ساعت"], ["🔇 ۱ روز", "🔇 دائمی"], [BTN_CANCEL]] if pending_action == "mute" else [[BTN_CONFIRM_KICK], [BTN_CANCEL]]
        return {
            "responses": [(admin_chat_id_value, f"عملیات {action_label} برای شناسهٔ {target_id}. {confirm_text}")],
            "history_user": None, "history_assistant": None, "clear_history": False,
            "feedback_action": None, "reply_markup": _reply_keyboard(rows, "مدت/تأیید را انتخاب کن"),
        }

    if text in {"🔇 ۱۰ دقیقه", "🔇 ۱ ساعت", "🔇 ۱ روز", "🔇 دائمی", BTN_CONFIRM_KICK}:
        pending_group = str(db_get_state("admin_moderation_group_id") or "")
        pending_action = str(db_get_state("admin_moderation_action") or "")
        target_id = str(db_get_state("admin_moderation_target_id") or "")
        if not selected_group or pending_group != selected_id or not target_id or pending_action not in {"mute", "kick"}:
            return _admin_panel_message(admin_chat_id_value, "درخواست منقضی شده؛ عملیات را دوباره شروع کن.")
        if pending_action == "mute":
            duration = {"🔇 ۱۰ دقیقه": 600, "🔇 ۱ ساعت": 3600, "🔇 ۱ روز": 86400, "🔇 دائمی": 0}.get(text)
            if duration is None:
                return _admin_panel_message(admin_chat_id_value, "مدت سکوت معتبر نیست.")
            payload = {"chat_id": selected_id, "user_id": target_id,
                       "permissions": {"can_send_messages": False, "can_send_media_messages": False,
                                       "can_send_polls": False, "can_send_other_messages": False,
                                       "can_add_web_page_previews": False}}
            if duration > 0:
                payload["until_date"] = int(time.time()) + duration
            method = "restrictChatMember"
        else:
            payload = {"chat_id": selected_id, "user_id": target_id}
            method = "kickChatMember"
        result = soroush_request(method, payload)
        ok = api_result_is_success(result)
        db_set_state("admin_moderation_action", "")
        db_set_state("admin_moderation_group_id", "")
        db_set_state("admin_moderation_target_id", "")
        if ok:
            action_name = "سکوت" if pending_action == "mute" else "اخراج"
            return _moderation_result(admin_chat_id_value, f"درخواست {action_name} برای عضو {target_id} به API ارسال شد و پاسخ موفق دریافت شد.")
        return _moderation_result(admin_chat_id_value, f"API سروش‌پلاس عملیات را تأیید نکرد. ممکن است این متد در Bot API فعلی پشتیبانی نشود یا ربات دسترسی مدیر نداشته باشد. عضو {target_id} تغییر داده نشد (نتیجهٔ موفق دریافت نشد).")

    if text == BTN_STATUS:
        return {
            "responses": [(admin_chat_id_value, _admin_status_text(selected_id))],
            "history_user": None,
            "history_assistant": None,
            "clear_history": False,
            "feedback_action": None,
            "reply_markup": _admin_main_keyboard(selected_id),
        }

    if text == BTN_CANCEL:
        db_set_state("admin_reset_confirm_group_id", "")
        db_set_state("admin_moderation_action", "")
        db_set_state("admin_moderation_group_id", "")
        db_set_state("admin_moderation_target_id", "")
        return _admin_panel_message(admin_chat_id_value, "عملیات لغو شد.")

    if text == BTN_CONFIRM_RESET:
        pending_id = db_get_state("admin_reset_confirm_group_id")
        if not selected_group or not selected_id or str(pending_id or "") != selected_id:
            db_set_state("admin_reset_confirm_group_id", "")
            return _admin_panel_message(admin_chat_id_value, "درخواست تأیید منقضی شده است؛ گروه را دوباره انتخاب کن.")
        reset_history(selected_id)
        db_set_state("admin_reset_confirm_group_id", "")
        title = selected_group.get("title") or f"گروه {selected_id}"
        return _admin_panel_message(admin_chat_id_value, f"حافظهٔ گفت‌وگوی گروه «{title}» پاک شد.")

    if text == BTN_RESET_GROUP:
        if not selected_group:
            return {
                "responses": [(admin_chat_id_value, "ابتدا یک گروه را انتخاب کن.")],
                "history_user": None,
                "history_assistant": None,
                "clear_history": False,
                "feedback_action": None,
                "reply_markup": _admin_main_keyboard(selected_id),
            }
        db_set_state("admin_reset_confirm_group_id", selected_id)
        return {
            "responses": [(admin_chat_id_value, "این کار حافظهٔ گفت‌وگوی گروه انتخاب‌شده را حذف می‌کند. برای ادامه تأیید کن.")],
            "history_user": None,
            "history_assistant": None,
            "clear_history": False,
            "feedback_action": None,
            "reply_markup": _reply_keyboard([[BTN_CONFIRM_RESET], [BTN_CANCEL]], "تأیید حذف حافظه؟"),
        }

    if text in {BTN_PAUSE_AI, BTN_ENABLE_AI, BTN_MODE_REPLY, BTN_MODE_ALL}:
        if not selected_group:
            return {
                "responses": [(admin_chat_id_value, "ابتدا یک گروه را انتخاب کن.")],
                "history_user": None,
                "history_assistant": None,
                "clear_history": False,
                "feedback_action": None,
                "reply_markup": _admin_main_keyboard(selected_id),
            }
        if text == BTN_PAUSE_AI:
            settings = db_set_group_settings(selected_id, ai_enabled=False)
            message = "پاسخ‌گویی هوش مصنوعی در این گروه متوقف شد."
        elif text == BTN_ENABLE_AI:
            settings = db_set_group_settings(selected_id, ai_enabled=True)
            message = "پاسخ‌گویی هوش مصنوعی در این گروه فعال شد."
        elif text == BTN_MODE_REPLY:
            settings = db_set_group_settings(selected_id, response_mode="reply")
            message = "از این پس ربات فقط به پیام‌هایی پاسخ می‌دهد که Reply آن‌ها به پیام ربات باشد."
        else:
            settings = db_set_group_settings(selected_id, response_mode="all")
            message = "حالت پاسخ‌گویی روی همهٔ پیام‌ها تنظیم شد."
        return _admin_panel_message(admin_chat_id_value, message)

    return None



def _group_panel_keyboard(chat_id):
    settings = db_get_group_settings(chat_id)
    ai_label = "⏸ خاموش‌کردن هوش مصنوعی" if settings["ai_enabled"] else "▶️ روشن‌کردن هوش مصنوعی"
    return {
        "inline_keyboard": [
            [
                {"text": "📊 وضعیت گروه", "callback_data": "np:status"},
                {"text": ai_label, "callback_data": "np:toggle_ai"},
            ],
            [
                {"text": "💬 پاسخ به همه پیام‌ها", "callback_data": "np:mode_all"},
                {"text": "🎯 فقط پاسخ به ریپلای", "callback_data": "np:mode_reply"},
            ],
            [{"text": "🧹 پاک‌کردن حافظه گروه", "callback_data": "np:reset_confirm"}],
            [
                {"text": "ℹ️ راهنما", "callback_data": "np:help"},
                {"text": "✖️ بستن پنل", "callback_data": "np:close"},
            ],
        ]
    }


def _group_panel_result(chat_id, message, with_keyboard=True):
    result = {
        "responses": [(str(chat_id), message)],
        "history_user": None,
        "history_assistant": None,
        "clear_history": False,
        "feedback_action": None,
    }
    if with_keyboard:
        result["reply_markup"] = _group_panel_keyboard(chat_id)
    return result


def _group_panel_status_text(chat_id):
    group = db_get_known_group(chat_id)
    settings = db_get_group_settings(chat_id)
    title = (group or {}).get("title") or f"گروه {chat_id}"
    ai_state = "روشن" if settings["ai_enabled"] else "خاموش"
    mode = "همه پیام‌ها" if settings["response_mode"] == "all" else "فقط ریپلای به ربات"
    return (
        f"📊 وضعیت پنل گروه\n"
        f"گروه: {title}\n"
        f"شناسه: {chat_id}\n"
        f"هوش مصنوعی: {ai_state}\n"
        f"حالت پاسخ‌گویی: {mode}\n"
        f"نسخه ربات: {VERSION}"
    )


def process_group_panel_action(chat_id, action, sender_id, panel_message_id=None):
    """Run only fixed, allow-listed group actions from the owner's inline panel."""
    if not is_owner_user(sender_id):
        return {
            "responses": [],
            "history_user": None,
            "history_assistant": None,
            "clear_history": False,
            "feedback_action": None,
        }

    chat_id = str(chat_id)
    if action == "np:status":
        return _group_panel_result(chat_id, _group_panel_status_text(chat_id))

    if action == "np:toggle_ai":
        current = db_get_group_settings(chat_id)
        updated = db_set_group_settings(chat_id, ai_enabled=not current["ai_enabled"])
        state = "فعال شد" if updated["ai_enabled"] else "متوقف شد"
        return _group_panel_result(chat_id, f"پاسخ‌گویی هوش مصنوعی در این گروه {state}.")

    if action == "np:mode_all":
        db_set_group_settings(chat_id, response_mode="all")
        return _group_panel_result(chat_id, "حالت پاسخ‌گویی روی «همه پیام‌ها» تنظیم شد.")

    if action == "np:mode_reply":
        db_set_group_settings(chat_id, response_mode="reply")
        return _group_panel_result(chat_id, "حالت پاسخ‌گویی روی «فقط ریپلای به پیام ربات» تنظیم شد.")

    if action == "np:reset_confirm":
        db_set_state(f"group_reset_confirm:{chat_id}", f"{sender_id}:{int(time.time())}")
        return {
            "responses": [(chat_id, "⚠️ حافظه گفت‌وگوی این گروه پاک شود؟ برای ادامه تأیید کن.")],
            "history_user": None,
            "history_assistant": None,
            "clear_history": False,
            "feedback_action": None,
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "✅ تأیید پاک‌کردن", "callback_data": "np:reset_execute"},
                    {"text": "↩️ انصراف", "callback_data": "np:reset_cancel"},
                ]]
            },
        }

    if action == "np:reset_execute":
        pending = db_get_state(f"group_reset_confirm:{chat_id}")
        try:
            pending_owner, pending_at = str(pending or "").split(":", 1)
            valid = pending_owner == str(sender_id) and (time.time() - int(pending_at)) <= 60
        except (TypeError, ValueError):
            valid = False
        db_set_state(f"group_reset_confirm:{chat_id}", "")
        if not valid:
            return _group_panel_result(chat_id, "درخواست تأیید منقضی شده است. دوباره از پنل اقدام کن.")
        reset_history(chat_id)
        return _group_panel_result(chat_id, "حافظه گفت‌وگوی این گروه پاک شد.")

    if action == "np:reset_cancel":
        db_set_state(f"group_reset_confirm:{chat_id}", "")
        return _group_panel_result(chat_id, "پاک‌کردن حافظه لغو شد.")

    if action == "np:help":
        return _group_panel_result(
            chat_id,
            "راهنمای پنل نپتون:\n"
            "• دکمه‌ها تنظیمات همین گروه را تغییر می‌دهند.\n"
            "• دکمه‌ها برای همه قابل‌مشاهده‌اند، اما فقط حساب مالک اجازه اجرای عملیات دارد.\n"
            "• پاک‌کردن حافظه پس از تأیید انجام می‌شود.\n"
            "• برای بازکردن دوباره پنل، مالک عبارت «پنل نپتون» یا /panel را بفرستد.",
        )

    if action == "np:close":
        db_set_state(f"group_panel_closed:{chat_id}:{panel_message_id or 'unknown'}", "1")
        if panel_message_id:
            soroush_request(
                "editMessageReplyMarkup",
                {
                    "chat_id": chat_id,
                    "message_id": panel_message_id,
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        return _group_panel_result(chat_id, "پنل کنترل گروه بسته شد.", with_keyboard=False)

    return _group_panel_result(chat_id, "این عملیات در پنل تعریف نشده است.")

def build_response_plan(
    chat_id,
    text,
    sender_id=None,
    sender_username=None,
    chat_type=None,
    reply_to_bot=False,
    is_private=False,
):
    started = time.perf_counter()
    text = text.strip()
    if not text:
        return {
            "responses": [],
            "history_user": None,
            "history_assistant": None,
            "clear_history": False,
            "feedback_action": None,
        }

    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()

    if text.startswith(GROUP_PANEL_PREFIX):
        if is_private or not is_owner_user(sender_id):
            return {
                "responses": [],
                "history_user": None,
                "history_assistant": None,
                "clear_history": False,
                "feedback_action": None,
            }
        action_payload = text[len(GROUP_PANEL_PREFIX):]
        action, separator, panel_message_id = action_payload.rpartition(":")
        if not separator:
            action, panel_message_id = action_payload, ""
        return process_group_panel_action(
            chat_id, action, sender_id,
            panel_message_id=panel_message_id or None,
        )

    admin_message = is_admin_user(sender_id, is_private)

    # The management panel is intentionally handled only in the administrator's
    # private dialog. Group updates never receive admin keyboards or command replies.
    if admin_message:
        panel_result = process_admin_panel_action(chat_id, text)
        if panel_result is not None:
            return panel_result
    elif command == "/panel":
        if not is_private and is_owner_user(sender_id):
            return _group_panel_result(
                chat_id,
                "🛠 پنل کنترل گروه نپتون\n"
                "دکمه‌ها برای همه دیده می‌شوند؛ اجرای آن‌ها فقط برای مالک مجاز است.",
            )
        return {
            "responses": [],
            "history_user": None,
            "history_assistant": None,
            "clear_history": False,
            "feedback_action": None,
        }

    if not is_private and normalize_text(text) == normalize_text(GROUP_PANEL_TRIGGER):
        if is_owner_user(sender_id):
            return _group_panel_result(
                chat_id,
                "🛠 پنل کنترل گروه نپتون\\n"
                "دکمه‌ها برای همه دیده می‌شوند؛ اجرای آن‌ها فقط برای مالک مجاز است.",
            )
        return {
            "responses": [],
            "history_user": None,
            "history_assistant": None,
            "clear_history": False,
            "feedback_action": None,
        }

    if command == "/start":
        return {
            "responses": [(chat_id, START_MESSAGE)],
            "history_user": None,
            "history_assistant": None,
            "clear_history": False,
            "feedback_action": None,
            "reply_markup": (
                _reply_keyboard([[FEEDBACK_TRIGGER_TEXT]], "پیام خود را بنویس")
                if is_private else None
            ),
        }

    if command == "/reset":
        return {
            "responses": [(chat_id, "حافظه گفت‌وگوی شما پاک شد.")],
            "history_user": None,
            "history_assistant": None,
            "clear_history": True,
            "feedback_action": None,
        }

    feedback_result = process_feedback_message(chat_id, text)
    if feedback_result is not None:
        return {
            "responses": feedback_result["responses"],
            "history_user": None,
            "history_assistant": None,
            "clear_history": False,
            "feedback_action": feedback_result.get("feedback_action"),
        }

    # Group controls are applied before normal AI responses. Commands above remain
    # usable, while paused groups and reply-only groups do not generate AI answers.
    if not is_private:
        group_settings = db_get_group_settings(chat_id)
        if not group_settings["ai_enabled"]:
            return {
                "responses": [],
                "history_user": None,
                "history_assistant": None,
                "clear_history": False,
                "feedback_action": None,
            }
        if group_settings["response_mode"] == "reply" and not reply_to_bot:
            return {
                "responses": [],
                "history_user": None,
                "history_assistant": None,
                "clear_history": False,
                "feedback_action": None,
            }

    if is_creator_question(text):
        return {
            "responses": [(chat_id, CREATOR_ANSWER)],
            "history_user": text,
            "history_assistant": CREATOR_ANSWER,
            "clear_history": False,
            "feedback_action": None,
        }

    fixed_answer = get_fixed_conversation_answer(text)
    if fixed_answer is not None:
        return {
            "responses": [(chat_id, fixed_answer)],
            "history_user": text,
            "history_assistant": fixed_answer,
            "clear_history": False,
            "feedback_action": None,
        }

    bounded_text = text[:MAX_MESSAGE_LENGTH]
    history_user = None
    history_assistant = None
    try:
        answer = call_ai(chat_id, bounded_text)
        history_user = bounded_text
        history_assistant = answer
    except UserVisibleError as exc:
        answer = str(exc)
    except Exception:
        logger.exception("Unexpected message processing failure")
        answer = "در پردازش پیام مشکلی پیش آمد. لطفاً کمی بعد دوباره تلاش کن."

    logger.info(
        "TIMING build_response_plan_ms=%.0f",
        (time.perf_counter() - started) * 1000,
    )
    return {
        "responses": [(chat_id, answer)],
        "history_user": history_user,
        "history_assistant": history_assistant,
        "clear_history": False,
        "feedback_action": None,
    }


def flatten_response_plan(responses, reply_markup=None):
    """Convert responses into durable chunks; attach keyboard to the final chunk."""
    plan = []
    for chat_id, text in responses:
        for chunk in split_message(text):
            plan.append({"chat_id": str(chat_id), "text": chunk})
    if plan and reply_markup is not None:
        plan[-1]["reply_markup"] = reply_markup
    return plan


# =========================================================
# Queue workers
# =========================================================
def _process_claimed_item(item, worker_id):
    message_id = item["id"]

    response_plan_raw = item.get("response_plan")
    if not response_plan_raw:
        result = build_response_plan(
            item["chat_id"],
            item["message_text"],
            sender_id=item.get("sender_id"),
            sender_username=item.get("sender_username"),
            chat_type=item.get("chat_type"),
            reply_to_bot=bool(item.get("reply_to_bot", 0)),
            is_private=bool(item.get("is_private", 0)),
        )
        plan = flatten_response_plan(
            result["responses"], reply_markup=result.get("reply_markup")
        )

        saved = db_prepare_response_plan(
            message_id,
            worker_id,
            plan,
            history_user=result.get("history_user"),
            history_assistant=result.get("history_assistant"),
            clear_history=result.get("clear_history", False),
            feedback_action=result.get("feedback_action"),
        )
        if not saved:
            raise RuntimeError("Could not persist response plan or worker lease was lost")
        response_index = 0
    else:
        try:
            plan = json.loads(response_plan_raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Stored response plan is invalid JSON") from exc
        response_index = int(item.get("response_index") or 0)

    if not isinstance(plan, list):
        raise RuntimeError("Stored response plan is not a list")

    while response_index < len(plan):
        if not db_renew_lease(message_id, worker_id):
            raise RuntimeError("Worker lease was lost before message delivery")

        part = plan[response_index]
        if not isinstance(part, dict) or not part.get("chat_id") or not isinstance(part.get("text"), str):
            raise RuntimeError("Stored response plan contains an invalid item")

        success = send_one_message(
            part["chat_id"], part["text"],
            reply_markup=part.get("reply_markup"),
            reply_to_message_id=part.get("reply_to_message_id"),
        )
        if not success:
            raise RetryableDeliveryError("Soroush sendMessage failed or had an ambiguous result")

        if not db_advance_response(message_id, worker_id, response_index):
            raise RuntimeError("Could not advance durable response cursor")
        response_index += 1

    if not db_finish_message(message_id, worker_id):
        raise RuntimeError("Could not finalize queue item")


def chat_worker():
    worker_id = _worker_id()
    logger.info("Worker started id=%s", worker_id)

    while not _polling_stop.is_set():
        item = None
        try:
            item = db_claim_next_message(worker_id)
            if item is None:
                _polling_stop.wait(WORKER_IDLE_WAIT)
                continue

            _process_claimed_item(item, worker_id)

        except Exception as exc:
            logger.error(
                "Worker processing failed type=%s",
                type(exc).__name__,
                exc_info=True,
            )
            if item is not None:
                try:
                    attempts = int(item.get("attempts", 0))
                    if attempts >= MAX_WORKER_RETRIES:
                        db_dead_letter(item["id"], worker_id, str(exc))
                    else:
                        db_retry_message(
                            item["id"],
                            worker_id,
                            attempts,
                            str(exc),
                        )
                except Exception:
                    logger.exception("Could not update failed queue item")
            _polling_stop.wait(0.5)

    logger.info("Worker stopped id=%s", worker_id)


def _start_or_replace_workers():
    global _worker_threads
    with _service_lock:
        alive = [worker for worker in _worker_threads if worker.is_alive()]
        _worker_threads = alive
        missing = max(0, MAX_PARALLEL_CHATS - len(_worker_threads))
        for index in range(missing):
            worker = threading.Thread(
                target=chat_worker,
                name=f"NeptonWorker-{len(_worker_threads) + 1}",
                daemon=True,
            )
            worker.start()
            _worker_threads.append(worker)
        if missing:
            logger.info("Started %s queue worker(s)", missing)


# =========================================================
# Polling and update batch handling
# =========================================================

def extract_callback_query(update):
    """Return normalized callback metadata, or None for a non-callback update."""
    if not isinstance(update, dict):
        return None
    callback = update.get("callback_query") or update.get("callbackQuery")
    if not isinstance(callback, dict):
        return None

    callback_id = callback.get("id") or callback.get("callback_query_id")
    data = callback.get("data") or callback.get("callback_data")
    sender = callback.get("from") or callback.get("from_user") or callback.get("sender")
    message = callback.get("message")
    if not isinstance(message, dict):
        return None

    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None

    sender_id = sender.get("id") if isinstance(sender, dict) else callback.get("user_id")
    username = sender.get("username") if isinstance(sender, dict) else None
    chat_id = chat.get("id")
    chat_type = str(chat.get("type") or "group").strip().lower()
    is_private = chat_type in {"private", "personal", "user"}

    if callback_id is None or data is None or sender_id is None or chat_id is None:
        return None

    message_id = message.get("message_id") or message.get("id")
    title = chat.get("title") or chat.get("name")
    return {
        "callback_id": str(callback_id),
        "data": str(data),
        "sender_id": str(sender_id),
        "sender_username": username.strip().lstrip("@").lower() if isinstance(username, str) else None,
        "chat_id": str(chat_id),
        "chat_type": chat_type,
        "chat_title": title,
        "is_private": is_private,
        "message_id": str(message_id) if message_id is not None else None,
    }


def answer_callback_query(callback_id, text=None, show_alert=False):
    if not callback_id:
        return False
    payload = {"callback_query_id": str(callback_id)}
    if text:
        payload["text"] = str(text)[:200]
    if show_alert:
        payload["show_alert"] = True
    result = soroush_request("answerCallbackQuery", payload)
    return api_result_is_success(result)

def prepare_update_batch(updates):
    prepared = []
    last_update_id = None

    for update in updates:
        update_id = get_update_id(update)
        if update_id is not None:
            last_update_id = (
                update_id if last_update_id is None
                else max(last_update_id, update_id)
            )

        callback = extract_callback_query(update)
        if callback is not None:
            callback_id = callback["callback_id"]
            sender_id = callback["sender_id"]
            chat_id = callback["chat_id"]
            action = callback["data"]

            if callback["is_private"] or callback["chat_type"] not in {"group", "supergroup"}:
                answer_callback_query(callback_id, "این پنل فقط در گروه قابل استفاده است.", True)
                continue

            if not is_owner_user(sender_id):
                answer_callback_query(callback_id, "فقط مالک ربات اجازه اجرای این دکمه‌ها را دارد.", True)
                continue

            if action not in GROUP_PANEL_CALLBACKS:
                answer_callback_query(callback_id, "این دکمه معتبر نیست.", True)
                continue

            if action != "np:close":
                closed_key = f"group_panel_closed:{chat_id}:{callback.get('message_id') or 'unknown'}"
                if db_get_state(closed_key) == "1":
                    answer_callback_query(callback_id, "این پنل قبلاً بسته شده است.", True)
                    continue

            answer_callback_query(callback_id, "درخواست دریافت شد؛ در حال اجرا…")
            panel_message_id = callback.get("message_id") or ""
            synthetic_text = GROUP_PANEL_PREFIX + action + ":" + panel_message_id
            prepared.append((
                update_id, chat_id, synthetic_text, sender_id,
                callback["sender_username"], callback["chat_type"],
                callback["chat_title"], False, False,
            ))
            continue

        extracted = extract_message(update)
        if extracted is not None:
            (chat_id, message_text, sender_id, sender_username, chat_type,
             chat_title, reply_to_bot, is_private) = extracted
            prepared.append((
                update_id, chat_id, message_text, sender_id, sender_username,
                chat_type, chat_title, reply_to_bot, is_private,
            ))

    return prepared, last_update_id


def polling_loop():
    offset = db_get_offset()
    backoff = POLL_ERROR_BACKOFF_START
    idle_wait = POLL_EMPTY_WAIT
    last_cleanup = time.monotonic()

    logger.info("Soroush polling started saved_offset=%s", offset)

    while not _polling_stop.is_set():
        try:
            if time.monotonic() - last_cleanup >= DB_CLEANUP_INTERVAL:
                try:
                    db_cleanup_old_records()
                except Exception:
                    logger.exception("Database cleanup failed")
                last_cleanup = time.monotonic()

            payload = {"offset": offset} if offset is not None else {}
            started = time.perf_counter()
            data = soroush_request("getUpdates", payload)
            logger.info(
                "TIMING get_updates_ms=%.0f",
                (time.perf_counter() - started) * 1000,
            )

            if data is None or not has_update_container(data):
                logger.error("Unexpected or failed Soroush getUpdates response")
                wait = min(backoff + random.uniform(0, backoff * 0.2), POLL_ERROR_BACKOFF_MAX)
                _polling_stop.wait(wait)
                backoff = min(backoff * 2, POLL_ERROR_BACKOFF_MAX)
                continue

            updates = extract_updates(data)
            if not updates:
                backoff = POLL_ERROR_BACKOFF_START
                _polling_stop.wait(idle_wait)
                idle_wait = min(idle_wait * 1.5, POLL_EMPTY_WAIT_MAX)
                continue

            idle_wait = POLL_EMPTY_WAIT
            prepared, last_id = prepare_update_batch(updates)

            if last_id is None:
                if not db_enqueue_updates(prepared, None):
                    _polling_stop.wait(POLL_ERROR_BACKOFF_START)
                elif not prepared:
                    _polling_stop.wait(idle_wait)
                    idle_wait = min(idle_wait * 1.5, POLL_EMPTY_WAIT_MAX)
                else:
                    backoff = POLL_ERROR_BACKOFF_START
                continue

            next_offset = last_id + 1
            if db_enqueue_updates(prepared, next_offset):
                offset = next_offset
                backoff = POLL_ERROR_BACKOFF_START
            else:
                # Do not move offset while the durable queue is full.
                _polling_stop.wait(POLL_ERROR_BACKOFF_START)

        except Exception:
            logger.exception("Polling iteration failed")
            _polling_stop.wait(backoff)
            backoff = min(backoff * 2, POLL_ERROR_BACKOFF_MAX)

    logger.info("Soroush polling stopped")


def _polling_main():
    try:
        polling_loop()
    except Exception:
        logger.exception("Polling thread stopped unexpectedly")


def start_polling():
    global _polling_thread
    with _service_lock:
        if _polling_thread is not None and _polling_thread.is_alive():
            return
        _polling_thread = threading.Thread(
            target=_polling_main,
            name="SoroushPolling",
            daemon=True,
        )
        _polling_thread.start()
        logger.info("Polling thread started")


def runtime_supervisor():
    logger.info("Runtime supervisor started")
    while not _polling_stop.wait(SUPERVISOR_INTERVAL):
        try:
            _start_or_replace_workers()
            start_polling()
        except Exception:
            logger.exception("Runtime supervisor iteration failed")
    logger.info("Runtime supervisor stopped")


def start_runtime_services():
    global _runtime_active, _supervisor_thread
    if _runtime_active:
        return

    if not acquire_runtime_lock():
        return

    # Database initialization and recovery happen only in the lock-owning process.
    # This prevents secondary WSGI workers from resetting live queue claims.
    init_database()
    db_recover_interrupted_jobs()
    load_persistent_runtime_state()
    register_bot_commands()
    _start_or_replace_workers()
    start_polling()

    _supervisor_thread = threading.Thread(
        target=runtime_supervisor,
        name="NeptonSupervisor",
        daemon=True,
    )
    _supervisor_thread.start()
    _runtime_active = True
    logger.info("NeptonBot %s runtime services started", VERSION)


def shutdown_runtime():
    _polling_stop.set()
    logger.info("NeptonBot shutdown requested")


atexit.register(shutdown_runtime)


# =========================================================
# Startup
# =========================================================
if os.getenv("NEPTON_DISABLE_AUTOSTART", "0").strip().lower() not in {"1", "true", "yes"}:
    start_runtime_services()
else:
    init_database()
    logger.warning("NEPTON_DISABLE_AUTOSTART is enabled; background services were not started")

logger.info("NeptonBot %s module initialized", VERSION)

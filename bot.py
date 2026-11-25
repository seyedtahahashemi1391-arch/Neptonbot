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
    "جهت ارسال نظرات پیشنهادات و انتقادات بر روی دکمه\n"
    "آبی کلیک و سپس بر روی ارسال نظر کلیک کنید."
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

            db.execute("PRAGMA user_version = 5")

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
    """Persist updates and polling offset atomically, with deduplication."""
    with db_connect() as db:
        db.execute("BEGIN IMMEDIATE")

        current_count = db.execute(
            "SELECT COUNT(*) AS total FROM message_queue"
        ).fetchone()["total"]

        unique_updates = []
        seen_in_batch = set()

        for update_id, chat_id, text in prepared_updates:
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
            unique_updates.append((update_id, str(chat_id), text))

        if current_count + len(unique_updates) > MAX_QUEUED_MESSAGES:
            db.rollback()
            logger.warning("Persistent message queue is full")
            return False

        now = time.time()
        for update_id, chat_id, message_text in unique_updates:
            db.execute("""
                INSERT INTO message_queue
                    (update_id, chat_id, message_text, queued_at,
                     attempts, status, retry_at)
                VALUES (?, ?, ?, ?, 0, 'pending', 0)
            """, (update_id, chat_id, message_text, now))

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
``````python
            if response.status_code in (408, 425, 429, 500, 502, 503, 504):
                if attempt == 0:
                    time.sleep(1.0)
                    continue
            response.raise_for_status()
            return response

        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exception = exc
            if attempt == 0:
                time.sleep(1.0)
                continue
            raise

    if last_exception is not None:
        raise last_exception
    raise RuntimeError("Cloudflare request failed after retry")


def ask_cloudflare(chat_id, message_text):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(get_history(chat_id))
    messages.append({"role": "user", "content": message_text})

    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 2048,
    }

    session = get_http_session("cloudflare_session")
    response = cloudflare_post_with_retry(session, payload)

    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("Cloudflare returned invalid JSON") from exc

    if not isinstance(result, dict):
        raise RuntimeError("Cloudflare response is not a JSON object")

    if result.get("success") is False or result.get("errors"):
        raise RuntimeError(
            "Cloudflare API error: " +
            json.dumps(result.get("errors"), ensure_ascii=False)[:500]
        )

    choices = result.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("Cloudflare response has no choices")

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise RuntimeError("Cloudflare choice is malformed")

    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("Cloudflare response has no message object")

    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Cloudflare returned empty message content")

    return content.strip()


# =========================================================
# Soroush Plus API
# =========================================================
def soroush_request(method, endpoint, **kwargs):
    url = f"{SOROUSH_API}/{endpoint.lstrip('/')}"
    session = get_http_session("soroush_session")

    try:
        response = session.request(
            method,
            url,
            timeout=SOROUSH_TIMEOUT,
            allow_redirects=False,
            **kwargs,
        )
    except requests.Timeout as exc:
        raise RetryableDeliveryError("Soroush API timed out") from exc
    except requests.ConnectionError as exc:
        raise RetryableDeliveryError("Soroush API connection failed") from exc

    if response.status_code in (408, 425, 429, 500, 502, 503, 504):
        raise RetryableDeliveryError(
            f"Soroush API temporary HTTP error: {response.status_code}"
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise UserVisibleError(
            f"Soroush API returned HTTP {response.status_code}"
        ) from exc

    try:
        result = response.json()
    except ValueError as exc:
        raise RetryableDeliveryError("Soroush API returned invalid JSON") from exc

    if not api_result_is_success(result):
        description = ""
        if isinstance(result, dict):
            description = str(
                result.get("description")
                or result.get("message")
                or result.get("error")
                or ""
            )
        raise RetryableDeliveryError(
            "Soroush API rejected request: " + description[:300]
        )

    return result


def api_result_is_success(result):
    """Interpret Soroush/Telegram-style responses without accepting explicit errors."""
    if result is None:
        return False
    if isinstance(result, dict):
        if "ok" in result:
            return result.get("ok") is True
        if result.get("success") is False or result.get("error"):
            return False
        # Some Soroush-compatible endpoints return a result object without `ok`.
        return True
    # A bare list is accepted only for endpoints whose response is list-shaped.
    return isinstance(result, list)


def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    return soroush_request("POST", "sendMessage", json=payload)


def send_typing(chat_id):
    try:
        soroush_request(
            "POST",
            "sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
        )
    except Exception:
        logger.debug("Could not send typing action", exc_info=True)


def split_message(text, limit=MAX_MESSAGE_LENGTH):
    """Split long text near line boundaries without splitting Unicode characters."""
    if not isinstance(text, str) or not text:
        return []
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("message split limit must be a positive integer")

    chunks = []
    remaining = text

    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit

        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


def send_long_message(chat_id, text, reply_markup=None):
    chunks = split_message(text)
    if not chunks:
        return []

    results = []
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        results.append(send_message(chat_id, chunk, reply_markup=markup))
    return results


# =========================================================
# Bot command registration
# =========================================================
def register_bot_commands():
    commands = [
        {"command": "start", "description": "شروع"},
        {"command": "help", "description": "راهنما"},
        {"command": "feedback", "description": FEEDBACK_COMMAND_DESCRIPTION},
        {"command": "cancel", "description": "لغو"},
    ]
    try:
        soroush_request(
            "POST",
            "setMyCommands",
            json={"commands": commands},
        )
    except Exception:
        logger.exception("Could not register bot commands")


# =========================================================
# Update parsing and polling
# =========================================================
def extract_message(update):
    if not isinstance(update, dict):
        return None

    message = (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("edited_channel_post")
    )
    if not isinstance(message, dict):
        return None

    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None

    chat_id = chat.get("id")
    if chat_id is None:
        return None

    text = message.get("text")
    if not isinstance(text, str):
        text = message.get("caption")
    if not isinstance(text, str):
        return None

    update_id = update.get("update_id")
    if update_id is not None:
        try:
            update_id = int(update_id)
        except (TypeError, ValueError):
            logger.warning("Ignoring update with invalid update_id")
            return None

    return update_id, str(chat_id), text


def get_updates(offset=None, timeout=20):
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset

    result = soroush_request(
        "GET",
        "getUpdates",
        params=params,
    )

    if isinstance(result, dict):
        updates = result.get("result", [])
    else:
        updates = result

    if not isinstance(updates, list):
        raise RetryableDeliveryError("Soroush getUpdates result is not a list")

    return updates


def polling_loop():
    offset = db_get_offset()
    empty_wait = POLL_EMPTY_WAIT
    error_backoff = POLL_ERROR_BACKOFF_START

    logger.info("Polling thread started")

    while not _polling_stop.is_set():
        try:
            updates = get_updates(offset=offset)
            prepared = []
            next_offset = offset

            for update in updates:
                if not isinstance(update, dict):
                    continue

                update_id = update.get("update_id")
                if update_id is not None:
                    try:
                        update_id = int(update_id)
                    except (TypeError, ValueError):
                        logger.warning("Skipping update with invalid update_id")
                        continue
                    next_offset = max(next_offset or 0, update_id + 1)

                parsed = extract_message(update)
                if parsed is not None:
                    prepared.append(parsed)

            if not db_enqueue_updates(prepared, next_offset):
                logger.warning("Could not persist updates; polling offset unchanged")
                _polling_stop.wait(1.0)
                continue

            offset = next_offset

            if updates:
                empty_wait = POLL_EMPTY_WAIT
            else:
                _polling_stop.wait(empty_wait)
                empty_wait = min(empty_wait * 1.5, POLL_EMPTY_WAIT_MAX)

            error_backoff = POLL_ERROR_BACKOFF_START

        except Exception:
            logger.exception("Polling loop failed")
            _polling_stop.wait(error_backoff)
            error_backoff = min(error_backoff * 2, POLL_ERROR_BACKOFF_MAX)

    logger.info("Polling thread stopped")


# =========================================================
# Message handling
# =========================================================
def normalize_command(text):
    if not isinstance(text, str):
        return ""
    return text.strip().split()[0].split("@")[0].lower() if text.strip() else ""


def is_admin_user(message):
    if not isinstance(message, dict):
        return False

    from_user = message.get("from")
    if not isinstance(from_user, dict):
        return False

    username = str(from_user.get("username") or "").lstrip("@").lower()
    return bool(username) and username == ADMIN_USERNAME


def make_main_keyboard():
    return {
        "keyboard": [
            [{"text": FEEDBACK_TRIGGER_TEXT}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def make_remove_keyboard():
    return {"remove_keyboard": True}


def handle_start(chat_id):
    send_message(chat_id, START_MESSAGE, reply_markup=make_main_keyboard())


def handle_help(chat_id):
    help_text = (
        "سلام! من نپتون بات هستم.\n\n"
        "پیامت را بفرست تا پاسخ بدهم.\n"
        "برای ارسال نظر از دکمهٔ «ارسال نظر» استفاده کن.\n"
        "برای لغو ارسال نظر دستور /cancel را بفرست."
    )
    send_message(chat_id, help_text, reply_markup=make_main_keyboard())


def handle_feedback_start(chat_id):
    db_set_state(f"feedback_prompt:{chat_id}", "1")
    send_message(
        chat_id,
        "نظرت را در یک پیام بفرست. برای لغو، /cancel را ارسال کن.",
        reply_markup=make_remove_keyboard(),
    )


def handle_feedback_cancel(chat_id):
    db_set_state(f"feedback_prompt:{chat_id}", "0")
    db_feedback_clear(chat_id)
    send_message(
        chat_id,
        "ارسال نظر لغو شد.",
        reply_markup=make_main_keyboard(),
    )


def handle_feedback_message(chat_id, text):
    if not db_feedback_is_pending(chat_id):
        return False

    if text.strip().lower() == FEEDBACK_CANCEL_COMMAND:
        handle_feedback_cancel(chat_id)
        return True

    admin_id = get_admin_chat_id()
    if not admin_id:
        send_message(
            chat_id,
            "هنوز مدیر ربات در دسترس نیست. لطفاً بعداً دوباره تلاش کن.",
            reply_markup=make_main_keyboard(),
        )
        return True

    feedback_text = (
        "📩 نظر جدید کاربر\n"
        f"Chat ID: {chat_id}\n\n"
        f"{text[:MAX_MESSAGE_LENGTH]}"
    )
    try:
        send_long_message(admin_id, feedback_text)
    except Exception:
        logger.exception("Could not forward feedback")
        send_message(
            chat_id,
            "ارسال نظر با خطا روبه‌رو شد. لطفاً دوباره تلاش کن.",
            reply_markup=make_main_keyboard(),
        )
        return True

    db_feedback_clear(chat_id)
    send_message(
        chat_id,
        "نظرت با موفقیت ارسال شد. ممنون از بازخوردت 🌟",
        reply_markup=make_main_keyboard(),
    )
    return True


def db_feedback_clear(chat_id):
    with db_connect() as db:
        db.execute(
            "DELETE FROM feedback_pending WHERE chat_id = ?",
            (str(chat_id),),
        )
    db_set_state(f"feedback_prompt:{chat_id}", "0")


def handle_admin_message(chat_id, text, message):
    command = normalize_command(text)

    if command == "/start":
        set_admin_chat_id(chat_id)
        handle_start(chat_id)
        return True

    if is_admin_user(message) and command == "/status":
        stats = db_queue_stats()
        report = (
            f"NeptonBot {VERSION}\n"
            f"Queue: {stats['queued_messages']}\n"
            f"Pending: {stats['pending_messages']}\n"
            f"Processing: {stats['processing_messages']}\n"
            f"Dead letters: {stats['dead_letters']}\n"
        )
        send_message(chat_id, report)
        return True

    return False


def process_user_message(chat_id, text, message):
    command = normalize_command(text)

    if command == "/start":
        handle_start(chat_id)
        return

    if command == "/help":
        handle_help(chat_id)
        return

    if command == FEEDBACK_COMMAND or text.strip() == FEEDBACK_TRIGGER_TEXT:
        handle_feedback_start(chat_id)
        return

    if command == FEEDBACK_CANCEL_COMMAND:
        handle_feedback_cancel(chat_id)
        return

    if handle_feedback_message(chat_id, text):
        return

    if handle_admin_message(chat_id, text, message):
        return

    bounded_text = text[:MAX_MESSAGE_LENGTH]
    if len(text) > MAX_MESSAGE_LENGTH:
        logger.info(
            "Incoming message truncated chat_id=%s original_chars=%s limit=%s",
            chat_id,
            len(text),
            MAX_MESSAGE_LENGTH,
        )

    history_user = None
    history_assistant = None
    try:
        send_typing(chat_id)
        answer = ask_cloudflare(chat_id, bounded_text)
        history_user = bounded_text
        history_assistant = answer
        send_long_message(chat_id, answer)
    except UserVisibleError as exc:
        logger.warning("User-visible processing error chat_id=%s: %s", chat_id, exc)
        send_message(chat_id, "ارسال پیام با خطا روبه‌رو شد. لطفاً دوباره تلاش کن.")
        raise
    except Exception:
        logger.exception("Message processing failed chat_id=%s", chat_id)
        raise

``````python
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
def build_response_plan(chat_id, text):
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

    if command == "/start":
        return {
            "responses": [(chat_id, START_MESSAGE)],
            "history_user": None,
            "history_assistant": None,
            "clear_history": False,
            "feedback_action": None,
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
    if len(text) > MAX_MESSAGE_LENGTH:
        logger.info(
            "Incoming message truncated chat_id=%s original_chars=%s limit=%s",
            chat_id,
            len(text),
            MAX_MESSAGE_LENGTH,
        )
    history_user = None
    history_assistant = None
    try:
        answer = call_ai(chat_id, bounded_text)
        history_user = bounded_text
        history_assistant = answer
    except UserVisibleError as exc:
        # Do not pollute future context with transient service-error messages.
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


def flatten_response_plan(responses):
    """Convert multi-recipient responses into durable, individually acknowledged chunks."""
    plan = []
    for chat_id, text in responses:
        for chunk in split_message(text):
            plan.append({"chat_id": str(chat_id), "text": chunk})
    return plan


# =========================================================
# Queue workers
# =========================================================
def _process_claimed_item(item, worker_id):
    message_id = item["id"]

    response_plan_raw = item.get("response_plan")
    if not response_plan_raw:
        result = build_response_plan(item["chat_id"], item["message_text"])
        plan = flatten_response_plan(result["responses"])

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
    if response_index < 0 or response_index > len(plan):
        raise RuntimeError("Stored response cursor is outside the response plan")

    while response_index < len(plan):
        if not db_renew_lease(message_id, worker_id):
            raise RuntimeError("Worker lease was lost before message delivery")

        part = plan[response_index]
        if not isinstance(part, dict) or not part.get("chat_id") or not isinstance(part.get("text"), str):
            raise RuntimeError("Stored response plan contains an invalid item")

        success = send_one_message(part["chat_id"], part["text"])
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

        extracted = extract_message(update)
        if extracted is not None:
            chat_id, message_text = extracted
            prepared.append((update_id, chat_id, message_text))

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
    """Ask background threads to stop and give them a short graceful-exit window."""
    _polling_stop.set()
    current = threading.current_thread()
    threads = [
        thread for thread in [_polling_thread, _supervisor_thread, *_worker_threads]
        if thread is not None and thread is not current and thread.is_alive()
    ]
    deadline = time.monotonic() + 8.0
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    still_alive = [thread.name for thread in threads if thread.is_alive()]
    if still_alive:
        logger.warning("Background threads still stopping: %s", ", ".join(still_alive))
    else:
        logger.info("All background threads stopped cleanly")
    logger.info("NeptonBot shutdown completed")


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

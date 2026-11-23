import os
import re
import time
import sqlite3
import logging
import threading
from contextlib import contextmanager
from collections import OrderedDict, deque

import requests
from requests.adapters import HTTPAdapter
from flask import Flask, jsonify


# =========================================================
# Configuration
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()

MODEL = "@cf/zai-org/glm-4.7-flash"
VERSION = "2.3.1"
ADMIN_USERNAME = "t91m62"

SOROUSH_API = f"https://api.splus.ir/bot{BOT_TOKEN}"
CLOUDFLARE_URL = (
    "https://api.cloudflare.com/client/v4/accounts/"
    f"{CLOUDFLARE_ACCOUNT_ID}/ai/v1/chat/completions"
)

# برای ماندگاری دیتابیس در Render، این متغیر را روی مسیر Persistent Disk تنظیم کن.
DB_PATH = os.getenv("NEPTON_DB_PATH", "nepton_state.sqlite3")

AI_TIMEOUT = (15, 150)
SOROUSH_TIMEOUT = (10, 35)

MAX_HISTORY_MESSAGES = 12
MAX_CONVERSATIONS = 500
MAX_MESSAGE_LENGTH = 3500
MAX_PARALLEL_CHATS = 3
MAX_QUEUED_MESSAGES = 1000

POLL_EMPTY_WAIT = 0.8
POLL_ERROR_BACKOFF_START = 2
POLL_ERROR_BACKOFF_MAX = 30
MAX_WORKER_RETRIES = 2

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
    "برای سؤال‌های کوتاه، پاسخ کوتاه و روشن بده؛ فقط در صورت نیاز جزئیات اضافه کن. "
    "درستی اطلاعات را بر روانی متن مقدم بدان و هیچ واقعیت، منبع، آزمون یا تجربه‌ای را جعل نکن. "
    "اطلاعات متغیر مانند قیمت، رتبه‌بندی و وضعیت روز را بدون منبع به‌روز قطعی اعلام نکن. "
    "اگر به اطلاعات زنده دسترسی نداری، این محدودیت را صریح بگو و وضعیت فعلی را حدس نزن. "
    "اگر دربارهٔ واقعیتی مطمئن نیستی، عدم‌قطعیت را صریح بیان کن. "
    "اگر کاربر پرسید چه کسی تو را ساخته یا سازنده‌ات کیست، "
    "پاسخ بده: «من توسط نپتون ساخته شدم. آیدی سازنده‌ام در بیوگرافی من قرار دارد.» "
    "در پاسخ به این سؤال هرگز از عبارت «تیم نپتون» استفاده نکن."
)

_missing = [
    name for name, value in (
        ("BOT_TOKEN", BOT_TOKEN),
        ("CLOUDFLARE_ACCOUNT_ID", CLOUDFLARE_ACCOUNT_ID),
        ("CLOUDFLARE_API_TOKEN", CLOUDFLARE_API_TOKEN),
    ) if not value
]
if _missing:
    raise RuntimeError("Missing environment variables: " + ", ".join(_missing))


# =========================================================
# Logging and HTTP sessions
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("NeptonBot")
logger.info("NeptonBot version: %s", VERSION)
logger.info("AI provider: Cloudflare Workers AI")
logger.info("Model: %s", MODEL)

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
# SQLite persistent state
# =========================================================
_db_init_lock = threading.Lock()


@contextmanager
def db_connect():
    """اتصال SQLite را در همهٔ حالت‌ها، حتی هنگام خطا، می‌بندد."""
    connection = sqlite3.connect(
        DB_PATH,
        timeout=30,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")

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


def init_database():
    with _db_init_lock:
        parent = os.path.dirname(os.path.abspath(DB_PATH))
        os.makedirs(parent, exist_ok=True)

        with db_connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = FULL")

            db.execute("""
                CREATE TABLE IF NOT EXISTS message_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    update_id INTEGER,
                    chat_id TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    queued_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending'
                )
            """)

            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_queue_status_id
                ON message_queue(status, id)
            """)

            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_queue_chat_status_id
                ON message_queue(chat_id, status, id)
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

            # پیام‌هایی که هنگام خاموش‌شدن در وضعیت پردازش بوده‌اند، بازیابی می‌شوند.
            db.execute("""
                UPDATE message_queue
                SET status = 'pending'
                WHERE status = 'processing'
            """)

    logger.info("SQLite database initialized: %s", DB_PATH)


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
        logger.warning("Invalid saved polling offset; starting without offset")
        return None


def db_get_admin_chat_id():
    return db_get_state("admin_chat_id")


def db_set_admin_chat_id(chat_id):
    if chat_id is None:
        return False
    db_set_state("admin_chat_id", str(chat_id))
    return True


def db_get_feedback_pending():
    with db_connect() as db:
        rows = db.execute(
            "SELECT chat_id FROM feedback_pending"
        ).fetchall()
    return {row["chat_id"] for row in rows}


def db_set_feedback_pending(chat_id, pending):
    with db_connect() as db:
        if pending:
            db.execute(
                "INSERT OR IGNORE INTO feedback_pending(chat_id) VALUES(?)",
                (str(chat_id),),
            )
        else:
            db.execute(
                "DELETE FROM feedback_pending WHERE chat_id = ?",
                (str(chat_id),),
            )


def db_queue_count():
    with db_connect() as db:
        row = db.execute(
            "SELECT COUNT(*) AS total FROM message_queue"
        ).fetchone()
    return int(row["total"])


def db_enqueue_updates(prepared_updates, next_offset):
    """پیام‌ها، شناسه‌های پردازش‌شده و offset را در یک تراکنش ذخیره می‌کند."""
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

            unique_updates.append((update_id, chat_id, text))

        if current_count + len(unique_updates) > MAX_QUEUED_MESSAGES:
            db.rollback()
            logger.warning("Persistent message queue is full")
            return False

        now = time.time()

        for update_id, chat_id, text in unique_updates:
            db.execute("""
                INSERT INTO message_queue
                    (update_id, chat_id, message_text, queued_at, attempts, status)
                VALUES (?, ?, ?, ?, 0, 'pending')
            """, (update_id, str(chat_id), text, now))

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


def db_claim_next_message():
    """ترتیب پیام‌های هر چت را حفظ می‌کند و چت‌های مختلف را موازی اجرا می‌کند."""
    with db_connect() as db:
        db.execute("BEGIN IMMEDIATE")

        row = db.execute("""
            SELECT q.*
            FROM message_queue AS q
            WHERE q.status = 'pending'
              AND NOT EXISTS (
                  SELECT 1
                  FROM message_queue AS earlier
                  WHERE earlier.chat_id = q.chat_id
                    AND earlier.id < q.id
              )
            ORDER BY q.id
            LIMIT 1
        """).fetchone()

        if row is None:
            db.commit()
            return None

        changed = db.execute("""
            UPDATE message_queue
            SET status = 'processing'
            WHERE id = ? AND status = 'pending'
        """, (row["id"],)).rowcount

        db.commit()
        return dict(row) if changed else None


def db_finish_message(message_id):
    with db_connect() as db:
        db.execute(
            "DELETE FROM message_queue WHERE id = ?",
            (message_id,),
        )


def db_retry_message(message_id):
    with db_connect() as db:
        db.execute("""
            UPDATE message_queue
            SET status = 'pending', attempts = attempts + 1, queued_at = ?
            WHERE id = ?
        """, (time.time(), message_id))


def db_discard_message(message_id):
    with db_connect() as db:
        db.execute(
            "DELETE FROM message_queue WHERE id = ?",
            (message_id,),
        )


# =========================================================
# Conversation history and runtime state
# =========================================================
conversation_history = OrderedDict()
history_lock = threading.Lock()

admin_lock = threading.Lock()
admin_chat_id = None

feedback_lock = threading.Lock()
feedback_pending = set()

app = Flask(__name__)
_polling_thread = None
_polling_stop = threading.Event()
_worker_threads = []


def load_persistent_runtime_state():
    global admin_chat_id, feedback_pending

    saved_admin = db_get_admin_chat_id()
    with admin_lock:
        admin_chat_id = saved_admin

    saved_pending = db_get_feedback_pending()
    with feedback_lock:
        feedback_pending = saved_pending


def get_history(chat_id):
    with history_lock:
        history = conversation_history.get(chat_id)
        if history is None:
            return []
        conversation_history.move_to_end(chat_id)
        return list(history)


def save_turn(chat_id, user_text, assistant_text):
    with history_lock:
        history = conversation_history.get(chat_id)
        if history is None:
            history = deque(maxlen=MAX_HISTORY_MESSAGES)
            conversation_history[chat_id] = history

        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": assistant_text})
        conversation_history.move_to_end(chat_id)

        while len(conversation_history) > MAX_CONVERSATIONS:
            conversation_history.popitem(last=False)


def reset_history(chat_id):
    with history_lock:
        conversation_history.pop(chat_id, None)


def set_admin_chat_id(chat_id):
    global admin_chat_id
    if chat_id is None:
        return False

    chat_id = str(chat_id)
    db_set_admin_chat_id(chat_id)

    with admin_lock:
        admin_chat_id = chat_id

    logger.info("Admin chat registered from verified sender username")
    return True


def get_admin_chat_id():
    with admin_lock:
        return admin_chat_id


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
    thread = _polling_thread
    polling_running = thread is not None and thread.is_alive()

    return jsonify({
        "status": "healthy" if polling_running else "degraded",
        "polling": "running" if polling_running else "stopped",
        "version": VERSION,
        "queued_messages": db_queue_count(),
        "workers": len(_worker_threads),
        "admin_registered": get_admin_chat_id() is not None,
    }), 200 if polling_running else 503


class UserVisibleError(Exception):
    pass


# =========================================================
# Cloudflare Workers AI
# =========================================================
_cloudflare_headers = {
    "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def cloudflare_post_with_retry(session, payload):
    for attempt in range(2):
        try:
            response = session.post(
                CLOUDFLARE_URL,
                headers=_cloudflare_headers,
                json=payload,
                timeout=AI_TIMEOUT,
                allow_redirects=False,
            )
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ):
            if attempt == 0:
                logger.warning("Temporary Cloudflare network failure; retrying once")
                continue
            raise

        if response.status_code in (500, 502, 503, 504) and attempt == 0:
            status = response.status_code
            response.close()
            logger.warning("Cloudflare HTTP %s; retrying once", status)
            continue

        return response

    raise requests.exceptions.ConnectionError(
        "Cloudflare request failed after retry"
    )


def call_ai(chat_id, user_text):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(get_history(chat_id))
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.5,
        "max_completion_tokens": 2048,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": False,
    }

    started = time.perf_counter()

    try:
        with cloudflare_post_with_retry(
            get_http_session("cloudflare_session"), payload
        ) as response:
            if not 200 <= response.status_code < 300:
                logger.error(
                    "Cloudflare HTTP error: status=%s",
                    response.status_code,
                )
                if response.status_code == 429:
                    raise UserVisibleError(
                        "تعداد درخواست‌ها به سرویس هوش مصنوعی زیاد شده است. "
                        "کمی بعد دوباره تلاش کن."
                    )
                raise UserVisibleError(
                    "سرویس هوش مصنوعی در حال حاضر پاسخ‌گوی درخواست نیست."
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
            "زمان پاسخ‌گویی هوش مصنوعی بیش از حد طول کشید."
        ) from exc
    except requests.exceptions.RequestException as exc:
        logger.error(
            "Cloudflare network error: type=%s",
            type(exc).__name__,
        )
        raise UserVisibleError(
            "ارتباط با سرویس هوش مصنوعی برقرار نشد."
        ) from exc
    finally:
        logger.info(
            "TIMING cloudflare_request_ms=%.0f",
            (time.perf_counter() - started) * 1000,
        )

    if not isinstance(data, dict):
        raise UserVisibleError("ساختار پاسخ هوش مصنوعی نامعتبر است.")

    choices = data.get("choices")
    if (
        not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0], dict)
    ):
        raise UserVisibleError("مدل نتوانست پاسخ معتبری تولید کند.")

    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise UserVisibleError("ساختار پیام خروجی مدل نامعتبر است.")

    content = message.get("content")
    if isinstance(content, str):
        answer = content.strip()
    elif isinstance(content, list):
        answer = "\n".join(
            item.get("text", "").strip()
            for item in content
            if isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and item.get("text").strip()
        ).strip()
    else:
        answer = ""

    if not answer:
        raise UserVisibleError(
            "مدل این بار پاسخ متنی تولید نکرد. دوباره تلاش کن."
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
                    "Soroush HTTP error: method=%s status=%s",
                    method,
                    response.status_code,
                )
                return None

            try:
                return response.json()
            except ValueError:
                logger.error("Soroush invalid JSON: method=%s", method)
                return None

    except requests.exceptions.RequestException as exc:
        logger.error(
            "Soroush network error: method=%s type=%s",
            method,
            type(exc).__name__,
        )
        return None


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

    if result is None or (
        isinstance(result, dict) and result.get("ok") is False
    ):
        logger.warning(
            "Could not register bot commands; /feedback remains usable by text"
        )
        return False

    logger.info("Bot commands registered")
    return True


def split_message(text):
    return [
        text[i:i + MAX_MESSAGE_LENGTH]
        for i in range(0, len(text), MAX_MESSAGE_LENGTH)
    ]


def send_message(chat_id, text):
    if not isinstance(text, str) or not text:
        return False

    started = time.perf_counter()
    success = True

    try:
        for chunk in split_message(text):
            result = soroush_request(
                "sendMessage",
                {"chat_id": chat_id, "text": chunk},
            )

            if result is None or (
                isinstance(result, dict) and result.get("ok") is False
            ):
                logger.error("Soroush sendMessage failed or was rejected")
                success = False
                break

        return success

    finally:
        logger.info(
            "TIMING send_message_ms=%.0f success=%s",
            (time.perf_counter() - started) * 1000,
            success,
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


def extract_message(update):
    if not isinstance(update, dict):
        return None

    message = (
        update.get("message")
        or update.get("edited_message")
        or update.get("new_message")
        or update
    )

    if not isinstance(message, dict):
        return None

    text = (
        message.get("text")
        or message.get("body")
        or message.get("message_text")
    )
    chat = message.get("chat")
    sender = message.get("from")

    chat_id = chat.get("id") if isinstance(chat, dict) else message.get("chat_id")
    if chat_id is None and isinstance(sender, dict):
        chat_id = sender.get("id")

    username = sender.get("username") if isinstance(sender, dict) else None
    normalized_username = (
        username.strip().lstrip("@").lower()
        if isinstance(username, str)
        else None
    )

    if normalized_username == ADMIN_USERNAME.lower() and chat_id is not None:
        set_admin_chat_id(chat_id)

    if text is None or chat_id is None:
        return None

    if not isinstance(text, str):
        text = str(text)

    return str(chat_id), text


def get_update_id(update):
    if not isinstance(update, dict):
        return None

    for key in ("update_id", "updateId", "id"):
        value = update.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value

    return None


# =========================================================
# Creator question detection
# =========================================================
def is_creator_question(user_text):
    normalized = user_text.strip().lower()
    normalized = normalized.replace("ي", "ی").replace("ك", "ک")
    normalized = normalized.replace("\u200c", " ")
    normalized = re.sub(r"[؟?!.,،؛:]+", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    exact_phrases = {
        "سازندت کیه",
        "سازنده ات کیه",
        "سازنده تو کیه",
        "چه کسی ساختت",
        "کی ساختت",
        "تو رو کی ساخته",
        "تورو کی ساخته",
        "توسعه دهنده ات کیه",
        "توسعه دهنده تو کیه",
        "چه کسی تو را ساخته",
        "چه کسی تو رو ساخته",
        "تو توسط چه کسی ساخته شدی",
    }

    if normalized in exact_phrases:
        return True

    patterns = (
        r"^(سازند|سازنده) .{0,12} کیه$",
        r"^(چه کسی|کی) .{0,8} (ساختت|ساخته|ساخته است)$",
        r"^(توسعه دهنده) .{0,12} کیه$",
    )

    return any(re.search(pattern, normalized) for pattern in patterns)


# =========================================================
# Feedback flow
# =========================================================
def process_feedback_message(chat_id, text):
    stripped = text.strip()
    command = (
        stripped.split(maxsplit=1)[0].split("@", 1)[0].lower()
        if stripped
        else ""
    )

    if command == FEEDBACK_CANCEL_COMMAND:
        with feedback_lock:
            was_pending = chat_id in feedback_pending
            feedback_pending.discard(chat_id)

        if was_pending:
            db_set_feedback_pending(chat_id, False)
            return send_message(chat_id, "ارسال نظر لغو شد.")

        return send_message(
            chat_id,
            "در حال حاضر در مرحلهٔ ارسال نظر نیستی.",
        )

    if command == FEEDBACK_COMMAND or stripped == FEEDBACK_TRIGGER_TEXT:
        with feedback_lock:
            feedback_pending.add(chat_id)

        db_set_feedback_pending(chat_id, True)

        return send_message(
            chat_id,
            "متن نظر یا پیشنهادت را در پیام بعدی بفرست.\n"
            "برای لغو، /cancel را ارسال کن.",
        )

    with feedback_lock:
        is_pending = chat_id in feedback_pending

    if not is_pending:
        return None

    if not stripped:
        return send_message(
            chat_id,
            "متن نظر خالی است؛ لطفاً دوباره بفرست.",
        )

    if len(stripped) > MAX_MESSAGE_LENGTH:
        return send_message(
            chat_id,
            f"متن نظر طولانی است. لطفاً آن را به حداکثر "
            f"{MAX_MESSAGE_LENGTH} نویسه کاهش بده.",
        )

    admin_id = get_admin_chat_id()
    if admin_id is None:
        return send_message(
            chat_id,
            "صندوق پیشنهادها هنوز برای مدیر فعال نشده است. "
            "مدیر باید ابتدا یک پیام به ربات بفرستد؛ سپس دوباره تلاش کن.",
        )

    admin_message = (
        "نظر یا پیشنهاد جدید\n"
        f"شناسهٔ فرستنده: {chat_id}\n\n"
        f"{stripped}"
    )

    if not send_message(admin_id, admin_message):
        return send_message(
            chat_id,
            "ارسال نظر به مدیر انجام نشد. لطفاً کمی بعد دوباره تلاش کن.",
        )

    with feedback_lock:
        feedback_pending.discard(chat_id)

    db_set_feedback_pending(chat_id, False)
    return send_message(
        chat_id,
        "نظر تو برای مدیر ارسال شد. ممنون از بازخوردت.",
    )


# =========================================================
# Message processing
# =========================================================
def process_message(chat_id, text, queued_at):
    started = time.perf_counter()
    queue_wait_ms = max(0, (time.time() - queued_at) * 1000)
    text = text.strip()

    if not text:
        return True

    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()

    if command == "/start":
        send_ok = send_message(chat_id, START_MESSAGE)
        logger.info(
            "TIMING message queue_wait_ms=%.0f total_ms=%.0f type=start",
            queue_wait_ms,
            (time.perf_counter() - started) * 1000,
        )
        return send_ok

    if command == "/reset":
        reset_history(chat_id)
        send_ok = send_message(chat_id, "حافظه گفت‌وگوی شما پاک شد.")
        logger.info(
            "TIMING message queue_wait_ms=%.0f total_ms=%.0f type=reset",
            queue_wait_ms,
            (time.perf_counter() - started) * 1000,
        )
        return send_ok

    feedback_result = process_feedback_message(chat_id, text)
    if feedback_result is not None:
        return feedback_result

    if is_creator_question(text):
        send_ok = send_message(chat_id, CREATOR_ANSWER)
        if send_ok:
            save_turn(chat_id, text, CREATOR_ANSWER)

        logger.info(
            "TIMING message queue_wait_ms=%.0f total_ms=%.0f type=creator result=%s",
            queue_wait_ms,
            (time.perf_counter() - started) * 1000,
            "sent" if send_ok else "send_failed",
        )
        return send_ok

    text = text[:MAX_MESSAGE_LENGTH]

    try:
        answer = call_ai(chat_id, text)
        send_ok = send_message(chat_id, answer)

        if send_ok:
            save_turn(chat_id, text, answer)

        logger.info(
            "TIMING message queue_wait_ms=%.0f total_ms=%.0f result=%s",
            queue_wait_ms,
            (time.perf_counter() - started) * 1000,
            "sent" if send_ok else "send_failed",
        )
        return send_ok

    except UserVisibleError as exc:
        send_ok = send_message(chat_id, str(exc))
        logger.warning(
            "TIMING message queue_wait_ms=%.0f total_ms=%.0f result=ai_error",
            queue_wait_ms,
            (time.perf_counter() - started) * 1000,
        )
        return bool(send_ok)

    except Exception as exc:
        logger.error(
            "Message processing failed: type=%s",
            type(exc).__name__,
        )
        send_ok = send_message(
            chat_id,
            "در پردازش پیام مشکلی پیش آمد. کمی بعد دوباره تلاش کن.",
        )
        logger.error(
            "TIMING message queue_wait_ms=%.0f total_ms=%.0f result=internal_error",
            queue_wait_ms,
            (time.perf_counter() - started) * 1000,
        )
        return send_ok


# =========================================================
# Persistent queue workers
# =========================================================
def chat_worker():
    while True:
        item = None

        try:
            item = db_claim_next_message()

            if item is None:
                time.sleep(0.25)
                continue

            completed = process_message(
                item["chat_id"],
                item["message_text"],
                item["queued_at"],
            )

            if completed:
                db_finish_message(item["id"])
            elif item["attempts"] >= MAX_WORKER_RETRIES:
                logger.error("Message discarded after bounded worker retries")
                db_discard_message(item["id"])
            else:
                db_retry_message(item["id"])

        except Exception as exc:
            logger.error("Worker failed: type=%s", type(exc).__name__)

            if item is not None:
                try:
                    if item["attempts"] >= MAX_WORKER_RETRIES:
                        db_discard_message(item["id"])
                    else:
                        db_retry_message(item["id"])
                except Exception:
                    logger.exception("Could not update failed queue item")

            time.sleep(1)


def start_chat_workers():
    if _worker_threads:
        return

    for index in range(MAX_PARALLEL_CHATS):
        worker = threading.Thread(
            target=chat_worker,
            name=f"NeptonWorker-{index + 1}",
            daemon=True,
        )
        worker.start()
        _worker_threads.append(worker)

    logger.info("Started %s persistent queue workers", MAX_PARALLEL_CHATS)


# =========================================================
# Update batch handling and polling
# =========================================================
def prepare_update_batch(updates):
    prepared = []
    last_update_id = None

    # شناسایی مدیر پیش از آغاز تراکنش ذخیرهٔ صف انجام می‌شود.
    for update in updates:
        update_id = get_update_id(update)

        if update_id is not None:
            last_update_id = (
                update_id if last_update_id is None
                else max(last_update_id, update_id)
            )

        extracted = extract_message(update)
        if extracted is not None:
            chat_id, text = extracted
            prepared.append((update_id, chat_id, text))

    return prepared, last_update_id


def polling_loop():
    offset = db_get_offset()
    backoff = POLL_ERROR_BACKOFF_START

    logger.info("Soroush polling started; saved offset=%s", offset)

    while not _polling_stop.is_set():
        try:
            payload = {"offset": offset} if offset is not None else {}
            started = time.perf_counter()
            data = soroush_request("getUpdates", payload)

            logger.info(
                "TIMING get_updates_ms=%.0f",
                (time.perf_counter() - started) * 1000,
            )

            if data is None or not has_update_container(data):
                logger.error("Unexpected or failed Soroush getUpdates response")
                _polling_stop.wait(backoff)
                backoff = min(backoff * 2, POLL_ERROR_BACKOFF_MAX)
                continue

            updates = extract_updates(data)
            if not updates:
                backoff = POLL_ERROR_BACKOFF_START
                _polling_stop.wait(POLL_EMPTY_WAIT)
                continue

            prepared, last_id = prepare_update_batch(updates)

            if last_id is None:
                # بدون شناسهٔ update امکان deduplication و ذخیرهٔ offset وجود ندارد.
                if not db_enqueue_updates(prepared, None):
                    _polling_stop.wait(POLL_ERROR_BACKOFF_START)
                elif not prepared:
                    _polling_stop.wait(POLL_EMPTY_WAIT)
                else:
                    backoff = POLL_ERROR_BACKOFF_START
                continue

            next_offset = last_id + 1

            if db_enqueue_updates(prepared, next_offset):
                offset = next_offset
                backoff = POLL_ERROR_BACKOFF_START
            else:
                # offset جلو نمی‌رود تا batch در درخواست بعدی دوباره دریافت شود.
                _polling_stop.wait(POLL_ERROR_BACKOFF_START)

        except Exception as exc:
            logger.error(
                "Polling iteration failed: type=%s",
                type(exc).__name__,
            )
            _polling_stop.wait(backoff)
            backoff = min(backoff * 2, POLL_ERROR_BACKOFF_MAX)


def _polling_main():
    try:
        polling_loop()
    except Exception as exc:
        logger.error(
            "Polling thread stopped unexpectedly: type=%s",
            type(exc).__name__,
        )


_polling_lock = threading.Lock()
_polling_started = False


def start_polling():
    global _polling_started, _polling_thread

    with _polling_lock:
        if (
            _polling_started
            and _polling_thread is not None
            and _polling_thread.is_alive()
        ):
            return

        _polling_stop.clear()
        _polling_thread = threading.Thread(
            target=_polling_main,
            name="SoroushPolling",
            daemon=True,
        )
        _polling_thread.start()
        _polling_started = True


# =========================================================
# Startup
# =========================================================
init_database()
load_persistent_runtime_state()
register_bot_commands()
start_chat_workers()
start_polling()

logger.info("NeptonBot %s started successfully", VERSION)

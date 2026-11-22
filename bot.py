
import os
import time
import logging
import threading
import queue

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

VERSION = "2.2.1"

SOROUSH_API = f"https://api.splus.ir/bot{BOT_TOKEN}"

CLOUDFLARE_URL = (
    "https://api.cloudflare.com/client/v4/accounts/"
    f"{CLOUDFLARE_ACCOUNT_ID}/ai/v1/chat/completions"
)

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

SYSTEM_PROMPT = (
    "تو دستیار هوش مصنوعی Nepton هستی. "
    "به زبان پیام کاربر پاسخ بده و پاسخ را دقیق، مستقیم، روشن و متناسب با سؤال بنویس. "
    "از مقدمه‌های کلیشه‌ای، تکرار سؤال و توضیحات غیرضروری پرهیز کن. "
    "برای سؤال‌های کوتاه، پاسخ کوتاه و روشن بده؛ فقط در صورت نیاز جزئیات اضافه کن. "
    "درستی اطلاعات را بر روانی متن مقدم بدان و هیچ واقعیت، منبع، آزمون یا تجربه‌ای را جعل نکن. "
    "اطلاعات متغیر مانند قیمت، رتبه‌بندی و وضعیت روز را بدون منبع به‌روز قطعی اعلام نکن. "
    "اگر به اطلاعات زنده دسترسی نداری، این محدودیت را صریح بگو و وضعیت فعلی را حدس نزن. "
    "اگر سؤال به معیارها یا شرایط خاصی وابسته است، معیارهای مهم را کوتاه روشن کن "
    "و نتیجه را بر همان اساس ارائه بده. "
    "اگر دربارهٔ واقعیتی مطمئن نیستی، عدم‌قطعیت را صریح بیان کن. "
    "اطلاعات شخصی کاربر را فقط وقتی مرتبط است در پاسخ به کار ببر."
)


# =========================================================
# Logging
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger("NeptonBot")


# =========================================================
# Environment validation
# =========================================================

_missing = []

if not BOT_TOKEN:
    _missing.append("BOT_TOKEN")

if not CLOUDFLARE_ACCOUNT_ID:
    _missing.append("CLOUDFLARE_ACCOUNT_ID")

if not CLOUDFLARE_API_TOKEN:
    _missing.append("CLOUDFLARE_API_TOKEN")

if _missing:
    raise RuntimeError(
        "Missing environment variables: " + ", ".join(_missing)
    )

logger.info("Environment variables verified")
logger.info("NeptonBot version: %s", VERSION)
logger.info("AI provider: Cloudflare Workers AI")
logger.info("Model: %s", MODEL)


# =========================================================
# Per-thread HTTP sessions
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
# Conversation memory
# =========================================================

conversation_history = OrderedDict()
history_lock = threading.Lock()


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

        history.append({
            "role": "user",
            "content": user_text,
        })

        history.append({
            "role": "assistant",
            "content": assistant_text,
        })

        conversation_history.move_to_end(chat_id)

        while len(conversation_history) > MAX_CONVERSATIONS:
            conversation_history.popitem(last=False)


def reset_history(chat_id):
    with history_lock:
        conversation_history.pop(chat_id, None)


# =========================================================
# Flask application
# =========================================================

app = Flask(__name__)

_polling_thread = None
_polling_stop = threading.Event()


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

    if thread is None or not thread.is_alive():
        return jsonify({
            "status": "degraded",
            "polling": "stopped",
            "version": VERSION,
        }), 503

    with _chat_queue_lock:
        pending_messages = _pending_message_count
        active_chats = len(_chat_messages)
        waiting_chats = _chat_queue.qsize()

    return jsonify({
        "status": "healthy",
        "polling": "running",
        "version": VERSION,
        "queued_messages": pending_messages,
        "active_or_waiting_chats": active_chats,
        "waiting_chats": waiting_chats,
        "workers": MAX_PARALLEL_CHATS,
    }), 200


# =========================================================
# Safe application errors
# =========================================================

class UserVisibleError(Exception):
    pass


# =========================================================
# Cloudflare AI
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
                logger.warning(
                    "Temporary Cloudflare network failure; retrying once"
                )
                continue

            raise

        if response.status_code in (500, 502, 503, 504):
            if attempt == 0:
                status = response.status_code
                response.close()

                logger.warning(
                    "Cloudflare HTTP %s; retrying once",
                    status,
                )
                continue

        return response

    raise requests.exceptions.ConnectionError(
        "Cloudflare request failed after retry"
    )


def call_ai(chat_id, user_text):
    messages = [{
        "role": "system",
        "content": SYSTEM_PROMPT,
    }]

    messages.extend(get_history(chat_id))

    messages.append({
        "role": "user",
        "content": user_text,
    })

    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.5,
        "max_completion_tokens": 2048,

        # Experimental optimization:
        # Disable GLM thinking mode for this test.
        "chat_template_kwargs": {
            "enable_thinking": False
        },

        "stream": False,
    }

    session = get_http_session("cloudflare_session")
    ai_started = time.perf_counter()

    try:
        with cloudflare_post_with_retry(
            session,
            payload,
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
                logger.error("Cloudflare returned invalid JSON")

                raise UserVisibleError(
                    "پاسخ سرویس هوش مصنوعی قابل پردازش نبود."
                ) from exc

    except UserVisibleError:
        raise

    except requests.exceptions.Timeout as exc:
        logger.warning("Cloudflare request timed out")

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
            (time.perf_counter() - ai_started) * 1000,
        )

    if not isinstance(data, dict):
        raise UserVisibleError(
            "ساختار پاسخ هوش مصنوعی نامعتبر است."
        )

    choices = data.get("choices")

    if not isinstance(choices, list) or not choices:
        logger.error(
            "Cloudflare response has no choices; keys=%s",
            list(data.keys()),
        )

        raise UserVisibleError(
            "مدل نتوانست پاسخ معتبری تولید کند."
        )

    first = choices[0]

    if not isinstance(first, dict):
        raise UserVisibleError(
            "ساختار خروجی مدل نامعتبر است."
        )

    finish_reason = first.get("finish_reason")
    message = first.get("message")

    if not isinstance(message, dict):
        raise UserVisibleError(
            "ساختار پیام خروجی مدل نامعتبر است."
        )

    content = message.get("content")

    if isinstance(content, str):
        answer = content.strip()

    elif isinstance(content, list):
        parts = []

        for item in content:
            if isinstance(item, dict):
                part = item.get("text")

                if isinstance(part, str) and part.strip():
                    parts.append(part.strip())

        answer = "\n".join(parts).strip()

    else:
        answer = ""

    if not answer:
        logger.error(
            "Empty AI content; finish_reason=%r; message_keys=%s",
            finish_reason,
            list(message.keys()),
        )

        raise UserVisibleError(
            "مدل این بار پاسخ متنی تولید نکرد. دوباره تلاش کن."
        )

    return answer


# =========================================================
# Soroush Plus API
# =========================================================

def soroush_request(method, payload=None):
    url = f"{SOROUSH_API}/{method}"
    session = get_http_session("soroush_session")

    try:
        with session.post(
            url,
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
                logger.error(
                    "Soroush invalid JSON: method=%s",
                    method,
                )
                return None

    except requests.exceptions.RequestException as exc:
        # Never log the URL because it contains the bot token.
        logger.error(
            "Soroush network error: method=%s type=%s",
            method,
            type(exc).__name__,
        )
        return None


def split_message(text):
    return [
        text[index:index + MAX_MESSAGE_LENGTH]
        for index in range(0, len(text), MAX_MESSAGE_LENGTH)
    ]


def send_message(chat_id, text):
    if not isinstance(text, str) or not text:
        return False

    send_started = time.perf_counter()
    success = True

    try:
        for chunk in split_message(text):
            result = soroush_request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                },
            )

            if result is None:
                logger.error("Soroush sendMessage failed")
                success = False
                break

            if isinstance(result, dict) and result.get("ok") is False:
                logger.error("Soroush rejected sendMessage")
                success = False
                break

        return success

    finally:
        logger.info(
            "TIMING send_message_ms=%.0f success=%s",
            (time.perf_counter() - send_started) * 1000,
            success,
        )


# =========================================================
# Update parsing
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

        if isinstance(value, dict):
            if any(
                isinstance(value.get(nested), list)
                for nested in ("updates", "messages", "result")
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

    chat_id = message.get("chat_id)
    chat = message.get("chat")

    if chat_id is None and isinstance(chat, dict):
        chat_id = chat.get("id")

    if chat_id is None:
        sender = message.get("from")

        if isinstance(sender, dict):
            chat_id = sender.get("id")

        elif sender is not None:
            chat_id = sender

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
# Message processing and timing
# =========================================================

def process_message(chat_id, text, queued_at):
    total_started = time.perf_counter()

    queue_wait_ms = max(
        0,
        (total_started - queued_at) * 1000,
    )

    text = text.strip()

    if not text:
        return

    command = (
        text.split(maxsplit=1)[0]
        .split("@", 1)[0]
        .lower()
    )

    if command == "/start":
        send_message(
            chat_id,
            "سلام! من نپتون هستم.\n"
            "به هوش مصنوعی متصل شده‌ام و آماده پاسخ‌گویی هستم."
        )

        logger.info(
            "TIMING message queue_wait_ms=%.0f total_ms=%.0f type=start",
            queue_wait_ms,
            (time.perf_counter() - total_started) * 1000,
        )
        return

    if command == "/reset":
        reset_history(chat_id)

        send_message(
            chat_id,
            "حافظه گفت‌وگوی شما پاک شد."
        )

        logger.info(
            "TIMING message queue_wait_ms=%.0f total_ms=%.0f type=reset",
            queue_wait_ms,
            (time.perf_counter() - total_started) * 1000,
        )
        return

    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH]

    try:
        answer = call_ai(chat_id, text)

        send_success = send_message(chat_id, answer)

        if send_success:
            save_turn(chat_id, text, answer)

        logger.info(
            "TIMING message queue_wait_ms=%.0f total_ms=%.0f result=%s",
            queue_wait_ms,
            (time.perf_counter() - total_started) * 1000,
            "sent" if send_success else "send_failed",
        )

    except UserVisibleError as exc:
        send_message(chat_id, str(exc))

        logger.warning(
            "TIMING message queue_wait_ms=%.0f total_ms=%.0f result=ai_error",
            queue_wait_ms,
            (time.perf_counter() - total_started) * 1000,
        )

    except Exception as exc:
        logger.error(
            "Message processing failed: type=%s",
            type(exc).__name__,
        )

        send_message(
            chat_id,
            "در پردازش پیام مشکلی پیش آمد. کمی بعد دوباره تلاش کن."
        )

        logger.error(
            "TIMING message queue_wait_ms=%.0f total_ms=%.0f result=internal_error",
            queue_wait_ms,
            (time.perf_counter() - total_started) * 1000,
        )


# =========================================================
# Asynchronous chat queue
# =========================================================

_chat_queue = queue.Queue(
    maxsize=MAX_CONVERSATIONS + MAX_PARALLEL_CHATS
)

_chat_queue_lock = threading.Lock()

_chat_messages = {}
_pending_message_count = 0

_worker_threads = []


def enqueue_chat_messages(grouped_messages):
    global _pending_message_count

    if not grouped_messages:
        return True

    with _chat_queue_lock:
        incoming_count = sum(
            len(messages)
            for messages in grouped_messages.values()
        )

        new_chats = [
            chat_id
            for chat_id in grouped_messages
            if chat_id not in _chat_messages
        ]

        if (
            _pending_message_count + incoming_count
            > MAX_QUEUED_MESSAGES
        ):
            logger.warning("Message queue is full")
            return False

        if (
            len(_chat_messages) + len(new_chats)
            > MAX_CONVERSATIONS
        ):
            logger.warning("Conversation queue capacity reached")
            return False

        for chat_id, messages in grouped_messages.items():
            if chat_id not in _chat_messages:
                _chat_messages[chat_id] = deque()
                _chat_queue.put_nowait(chat_id)

            received_at = time.perf_counter()

            for message_text in messages:
                _chat_messages[chat_id].append(
                    (message_text, received_at)
                )

        _pending_message_count += incoming_count

    return True


def chat_worker():
    global _pending_message_count

    while True:
        chat_id = _chat_queue.get()

        try:
            with _chat_queue_lock:
                messages = _chat_messages.get(chat_id)

                if not messages:
                    _chat_messages.pop(chat_id, None)
                    item = None

                else:
                    item = messages.popleft()
                    _pending_message_count -= 1

            if item is not None:
                message_text, queued_at = item

                try:
                    process_message(
                        chat_id,
                        message_text,
                        queued_at,
                    )

                except Exception as exc:
                    logger.error(
                        "Worker processing failed: type=%s",
                        type(exc).__name__,
                    )

            with _chat_queue_lock:
                messages = _chat_messages.get(chat_id)

                if messages:
                    _chat_queue.put_nowait(chat_id)

                else:
                    _chat_messages.pop(chat_id, None)

        except Exception as exc:
            logger.error(
                "Chat worker failed: type=%s",
                type(exc).__name__,
            )

            with _chat_queue_lock:
                messages = _chat_messages.get(chat_id)

                if messages:
                    _chat_queue.put_nowait(chat_id)

                else:
                    _chat_messages.pop(chat_id, None)

        finally:
            _chat_queue.task_done()


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

    logger.info(
        "Started %s chat workers",
        MAX_PARALLEL_CHATS,
    )


# =========================================================
# Update batch handling
# =========================================================

def process_update_batch(updates):
    grouped_messages = OrderedDict()
    last_update_id = None

    for update in updates:
        update_id = get_update_id(update)

        if update_id is not None:
            last_update_id = update_id

        extracted = extract_message(update)

        if extracted is None:
            continue

        chat_id, text = extracted

        grouped_messages.setdefault(
            chat_id,
            [],
        ).append(text)

    if not enqueue_chat_messages(grouped_messages):
        return None

    return last_update_id


# =========================================================
# Polling
# =========================================================

_polling_lock = threading.Lock()
_polling_started = False


def polling_loop():
    offset = None
    error_backoff = POLL_ERROR_BACKOFF_START

    logger.info("Soroush polling started")

    while not _polling_stop.is_set():
        try:
            payload = {}

            if offset is not None:
                payload["offset"] = offset

            poll_started = time.perf_counter()

            data = soroush_request(
                "getUpdates",
                payload,
            )

            poll_duration_ms = (
                time.perf_counter() - poll_started
            ) * 1000

            logger.info(
                "TIMING get_updates_ms=%.0f",
                poll_duration_ms,
            )

            if data is None:
                _polling_stop.wait(error_backoff)

                error_backoff = min(
                    error_backoff * 2,
                    POLL_ERROR_BACKOFF_MAX,
                )
                continue

            if not has_update_container(data):
                logger.error(
                    "Unexpected Soroush getUpdates response"
                )

                _polling_stop.wait(error_backoff)

                error_backoff = min(
                    error_backoff * 2,
                    POLL_ERROR_BACKOFF_MAX,
                )
                continue

            updates = extract_updates(data)

            if not updates:
                error_backoff = POLL_ERROR_BACKOFF_START
                _polling_stop.wait(POLL_EMPTY_WAIT)
                continue

            last_update_id = process_update_batch(updates)

            # Advance only after successful queue acceptance.
            if last_update_id is not None:
                if last_update_id < (offset or -1):
                    logger.warning("Unexpected update ID")

                else:
                    offset = last_update_id + 1

            error_backoff = POLL_ERROR_BACKOFF_START

        except Exception as exc:
            logger.error(
                "Polling iteration failed: type=%s",
                type(exc).__name__,
            )

            _polling_stop.wait(error_backoff)

            error_backoff = min(
                error_backoff * 2,
                POLL_ERROR_BACKOFF_MAX,
            )


def _polling_main():
    try:
        polling_loop()

    except Exception as exc:
        logger.error(
            "Polling thread stopped unexpectedly: type=%s",
            type(exc).__name__,
        )


def start_polling():
    global _polling_started, _polling_thread

    with _polling_lock:
        if (
            _polling_started
            and _polling_thread
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

start_chat_workers()
start_polling()

logger.info(
    "NeptonBot %s started successfully",
    VERSION,
)

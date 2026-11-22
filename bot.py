import os
import logging
import threading
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from flask import Flask, jsonify


# =========================
# Configuration
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()

MODEL = "@cf/zai-org/glm-4.7-flash"

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


# =========================
# Logging
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("NeptonBot")


# =========================
# Environment validation
# =========================

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
logger.info("AI provider: Cloudflare Workers AI")
logger.info("Model: %s", MODEL)


# =========================
# Per-thread HTTP sessions
# =========================

_thread_local = threading.local()


def _new_session():
    http_session = requests.Session()

    adapter = HTTPAdapter(
        pool_connections=4,
        pool_maxsize=4,
        max_retries=0,
        pool_block=True,
    )

    http_session.mount("https://", adapter)
    http_session.mount("http://", adapter)

    http_session.headers.update({
        "User-Agent": "NeptonBot/2.0",
        "Accept": "application/json",
    })

    return http_session


def get_http_session(name):
    """Return one reusable requests session per thread and service."""
    current = getattr(_thread_local, name, None)

    if current is None:
        current = _new_session()
        setattr(_thread_local, name, current)

    return current


# =========================
# Conversation memory
# =========================

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


# =========================
# Flask application
# =========================

app = Flask(__name__)


@app.get("/")
def home():
    return jsonify({
        "status": "online",
        "bot": "Nepton",
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
        }), 503

    return jsonify({
        "status": "healthy",
        "polling": "running",
    }), 200


# =========================
# Safe application errors
# =========================

class UserVisibleError(Exception):
    """An error message that is safe to show to the user."""


# =========================
# Cloudflare AI
# =========================

_cloudflare_headers = {
    "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def call_ai(chat_id, user_text):
    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        }
    ]

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
        "stream": False,
    }

    http_session = get_http_session("cloudflare_session")

    try:
        with http_session.post(
            CLOUDFLARE_URL,
            headers=_cloudflare_headers,
            json=payload,
            timeout=AI_TIMEOUT,
            allow_redirects=False,
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
                    "سرویس هوش مصنوعی در حال حاضر پاسخ‌گوی درخواست نیست. "
                    "کمی بعد دوباره تلاش کن."
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
            "زمان پاسخ‌گویی هوش مصنوعی بیش از حد طول کشید. دوباره تلاش کن."
        ) from exc

    except requests.exceptions.RequestException as exc:
        logger.error(
            "Cloudflare network error: type=%s",
            type(exc).__name__,
        )

        raise UserVisibleError(
            "ارتباط با سرویس هوش مصنوعی برقرار نشد. کمی بعد دوباره تلاش کن."
        ) from exc

    if not isinstance(data, dict):
        logger.error("Cloudflare returned a non-object JSON response")
        raise UserVisibleError("ساختار پاسخ هوش مصنوعی نامعتبر است.")

    choices = data.get("choices")

    if not isinstance(choices, list) or not choices:
        logger.error(
            "Cloudflare response has no choices; keys=%s",
            list(data.keys()),
        )

        raise UserVisibleError("مدل نتوانست پاسخ معتبری تولید کند.")

    first_choice = choices[0]

    if not isinstance(first_choice, dict):
        raise UserVisibleError("ساختار خروجی مدل نامعتبر است.")

    finish_reason = first_choice.get("finish_reason")
    message = first_choice.get("message")

    if not isinstance(message, dict):
        logger.error(
            "Cloudflare response has no valid message; finish_reason=%r",
            finish_reason,
        )
        raise UserVisibleError("ساختار پیام خروجی مدل نامعتبر است.")

    content = message.get("content")

    if isinstance(content, str):
        answer = content.strip()

    elif isinstance(content, list):
        parts = []

        for item in content:
            if not isinstance(item, dict):
                continue

            part = item.get("text")

            if isinstance(part, str) and part.strip():
                parts.append(part.strip())

        answer = "\n".join(parts).strip()

    else:
        answer = ""

    if not answer:
        logger.error(
            "Cloudflare returned empty final content; "
            "finish_reason=%r, message_keys=%s",
            finish_reason,
            list(message.keys()),
        )

        raise UserVisibleError(
            "مدل این بار پاسخ متنی تولید نکرد. لطفاً دوباره تلاش کن."
        )

    return answer


# =========================
# Soroush Plus API
# =========================

def soroush_request(method, payload=None):
    url = f"{SOROUSH_API}/{method}"
    http_session = get_http_session("soroush_session")

    try:
        with http_session.post(
            url,
            json=payload or {},
            timeout=SOROUSH_TIMEOUT,
            allow_redirects=False,
        ) as response:

            if not 200 <= response.status_code < 300:
                logger.error(
                    "Soroush API HTTP error: method=%s status=%s",
                    method,
                    response.status_code,
                )
                return None

            try:
                return response.json()
            except ValueError:
                logger.error(
                    "Soroush returned invalid JSON: method=%s",
                    method,
                )
                return None

    except requests.exceptions.RequestException as exc:
        # The Soroush URL contains the bot token; never log the URL or traceback.
        logger.error(
            "Soroush API network error: method=%s type=%s",
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

    for chunk in split_message(text):
        result = soroush_request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
            },
        )

        if result is None:
            logger.error("Could not send a message through Soroush")
            return False

        if isinstance(result, dict) and result.get("ok") is False:
            logger.error("Soroush rejected sendMessage")
            return False

    return True


# =========================
# Update parsing
# =========================

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
            isinstance(value.get(nested_key), list)
            for nested_key in ("updates", "messages", "result")
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

    chat_id = message.get("chat_id")
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


# =========================
# Message processing
# =========================

def process_message(chat_id, text):
    text = text.strip()

    if not text:
        return

    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()

    if command == "/start":
        send_message(
            chat_id,
            "سلام! من نپتون هستم.\n"
            "به هوش مصنوعی متصل شده‌ام و آماده پاسخ‌گویی هستم."
        )
        return

    if command == "/reset":
        reset_history(chat_id)
        send_message(chat_id, "حافظه گفت‌وگوی شما پاک شد.")
        return

    try:
        answer = call_ai(chat_id, text)

        if send_message(chat_id, answer):
            save_turn(chat_id, text, answer)

    except UserVisibleError as exc:
        send_message(chat_id, str(exc))

    except Exception as exc:
        logger.error(
            "Message processing failed: type=%s",
            type(exc).__name__,
        )

        send_message(
            chat_id,
            "در پردازش پیام مشکلی پیش آمد. کمی بعد دوباره تلاش کن."
        )


def process_chat_batch(chat_id, messages):
    # Each chat is assigned to one task, preserving its message order.
    for text in messages:
        process_message(chat_id, text)


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
        grouped_messages.setdefault(chat_id, []).append(text)

    futures = []

    for chat_id, messages in grouped_messages.items():
        futures.append(
            _message_executor.submit(
                process_chat_batch,
                chat_id,
                messages,
            )
        )

    for future in futures:
        try:
            future.result()

        except Exception as exc:
            logger.error(
                "Chat batch failed: type=%s",
                type(exc).__name__,
            )

    return last_update_id


# =========================
# Polling
# =========================

_message_executor = ThreadPoolExecutor(
    max_workers=MAX_PARALLEL_CHATS,
    thread_name_prefix="NeptonChat",
)

_polling_stop = threading.Event()
_polling_lock = threading.Lock()
_polling_started = False
_polling_thread = None


def polling_loop():
    offset = None
    error_backoff = POLL_ERROR_BACKOFF_START

    logger.info("Soroush polling started")

    while not _polling_stop.is_set():
        try:
            payload = {}

            if offset is not None:
                payload["offset"] = offset

            data = soroush_request("getUpdates", payload)

            if data is None:
                _polling_stop.wait(error_backoff)
                error_backoff = min(
                    error_backoff * 2,
                    POLL_ERROR_BACKOFF_MAX,
                )
                continue

            if not has_update_container(data):
                logger.error(
                    "Soroush getUpdates returned an unexpected response"
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

            # Advance only after all updates in this batch have been handled.
            if last_update_id is not None:
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
        # A final guard prevents an unhandled exception from being silent.
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


start_polling()

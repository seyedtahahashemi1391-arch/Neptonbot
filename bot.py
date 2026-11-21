
import os
import time
import logging
import threading
from collections import defaultdict, deque

import requests
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
    f"https://api.cloudflare.com/client/v4/accounts/"
    f"{CLOUDFLARE_ACCOUNT_ID}/ai/v1/chat/completions"
)

AI_TIMEOUT = (15, 150)
SOROUSH_TIMEOUT = (10, 35)
MAX_HISTORY_MESSAGES = 12
MAX_MESSAGE_LENGTH = 3500

SYSTEM_PROMPT = (
    "تو یک دستیار هوش مصنوعی دقیق، مفید و فارسی‌زبان هستی. "
    "پاسخ‌ها را روشن، صحیح و متناسب با سؤال کاربر بنویس. "
    "اگر از موضوعی مطمئن نیستی، عدم اطمینان را بیان کن."
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
# Validation
# =========================

missing = []

if not BOT_TOKEN:
    missing.append("BOT_TOKEN")

if not CLOUDFLARE_ACCOUNT_ID:
    missing.append("CLOUDFLARE_ACCOUNT_ID")

if not CLOUDFLARE_API_TOKEN:
    missing.append("CLOUDFLARE_API_TOKEN")

if missing:
    raise RuntimeError(
        "Missing environment variables: " + ", ".join(missing)
    )

logger.info("Environment variables verified")
logger.info("AI provider: Cloudflare Workers AI")
logger.info("Model: %s", MODEL)


# =========================
# HTTP Sessions
# =========================

session = requests.Session()

session.headers.update({
    "User-Agent": "NeptonBot/1.0",
    "Accept": "application/json"
})

cloudflare_headers = {
    "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json"
}


# =========================
# Conversation Memory
# =========================

conversation_history = defaultdict(
    lambda: deque(maxlen=MAX_HISTORY_MESSAGES)
)

history_lock = threading.Lock()


# =========================
# Flask Application
# =========================

app = Flask(__name__)


@app.get("/")
def home():
    return jsonify({
        "status": "online",
        "bot": "Nepton",
        "provider": "Cloudflare Workers AI",
        "model": MODEL
    })


@app.get("/health")
def health():
    return jsonify({"status": "healthy"}), 200


# =========================
# Cloudflare AI
# =========================

def call_ai(chat_id, user_text):

    with history_lock:
        history = list(conversation_history[chat_id])

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        }
    ]

    messages.extend(history)

    messages.append({
        "role": "user",
        "content": user_text
    })

    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 2048,
        "stream": False
    }

    try:
        response = requests.post(
            CLOUDFLARE_URL,
            headers=cloudflare_headers,
            json=payload,
            timeout=AI_TIMEOUT
        )

        if not response.ok:
            logger.error(
                "Cloudflare HTTP error: status=%s body=%s",
                response.status_code,
                response.text[:1000]
            )
            response.raise_for_status()

        try:
            data = response.json()
        except ValueError:
            logger.error("Cloudflare returned invalid JSON")
            raise RuntimeError("پاسخ کلودفلر قابل خواندن نیست.")

        choices = data.get("choices")

        if not isinstance(choices, list) or not choices:
            logger.error(
                "Unexpected Cloudflare response keys: %s",
                list(data.keys()) if isinstance(data, dict) else type(data).__name__
            )
            raise RuntimeError("ساختار پاسخ هوش مصنوعی نامعتبر است.")

        first_choice = choices[0]

        if not isinstance(first_choice, dict):
            raise RuntimeError("ساختار خروجی مدل نامعتبر است.")

        message = first_choice.get("message", {})

        if not isinstance(message, dict):
            raise RuntimeError("پیام خروجی مدل نامعتبر است.")

        answer = message.get("content")

        if isinstance(answer, list):
            answer = "".join(
                item.get("text", "")
                for item in answer
                if isinstance(item, dict)
            )

        if not isinstance(answer, str) or not answer.strip():
            logger.error(
                "Cloudflare returned empty content. Message keys: %s",
                list(message.keys())
            )
            raise RuntimeError("مدل پاسخ متنی تولید نکرد.")

        answer = answer.strip()

        with history_lock:
            conversation_history[chat_id].append({
                "role": "user",
                "content": user_text
            })

            conversation_history[chat_id].append({
                "role": "assistant",
                "content": answer
            })

        return answer

    except requests.exceptions.Timeout:
        logger.error("Cloudflare request timed out")
        raise RuntimeError(
            "زمان پاسخ‌گویی هوش مصنوعی بیش از حد طول کشید. دوباره تلاش کن."
        )

    except requests.exceptions.RequestException:
        logger.exception("Cloudflare network request failed")
        raise RuntimeError(
            "ارتباط با سرویس هوش مصنوعی برقرار نشد."
        )


# =========================
# Soroush Plus API
# =========================

def soroush_request(method, payload=None):

    url = f"{SOROUSH_API}/{method}"

    try:
        response = session.post(
            url,
            json=payload or {},
            timeout=SOROUSH_TIMEOUT
        )

        response.raise_for_status()

        try:
            return response.json()
        except ValueError:
            logger.error("Soroush returned invalid JSON")
            return None

    except requests.exceptions.RequestException:
        logger.exception("Soroush API request failed")
        return None


def send_message(chat_id, text):

    if not text:
        return

    chunks = [
        text[i:i + MAX_MESSAGE_LENGTH]
        for i in range(0, len(text), MAX_MESSAGE_LENGTH)
    ]

    for chunk in chunks:
        result = soroush_request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk
            }
        )

        if result is None:
            logger.error("Could not send message to chat")
            return

        if isinstance(result, dict) and result.get("ok") is False:
            logger.error(
                "Soroush rejected sendMessage: %s",
                str(result)[:500]
            )
            return


# =========================
# Update Parsing
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

    chat_id = message.get("chat_id")

    if chat_id is None and isinstance(chat, dict):
        chat_id = chat.get("id")

    if chat_id is None:
        sender = message.get("from")

        if isinstance(sender, dict):
            chat_id = sender.get("id")

    if chat_id is None:
        chat_id = message.get("from")

    if isinstance(chat_id, dict):
        chat_id = chat_id.get("id")

    if text is None or chat_id is None:
        return None

    return str(chat_id), str(text)


def get_update_id(update):

    if not isinstance(update, dict):
        return None

    for key in ("update_id", "updateId", "id"):
        value = update.get(key)

        if isinstance(value, int):
            return value

    return None


# =========================
# Message Processing
# =========================

def process_message(chat_id, text):

    text = text.strip()

    if not text:
        return

    if text.startswith("/start"):
        send_message(
            chat_id,
            "سلام! من نپتون هستم.\n"
            "به هوش مصنوعی متصل شده‌ام و آماده پاسخ‌گویی هستم."
        )
        return

    if text.startswith("/reset"):
        with history_lock:
            conversation_history.pop(chat_id, None)

        send_message(
            chat_id,
            "حافظه گفت‌وگوی شما پاک شد."
        )
        return

    try:
        answer = call_ai(chat_id, text)
        send_message(chat_id, answer)

    except Exception as error:
        logger.exception("Message processing failed")

        send_message(
            chat_id,
            "در پردازش پیام مشکلی پیش آمد:\n"
            + str(error)
        )


# =========================
# Polling
# =========================

def polling_loop():

    offset = None

    logger.info("Soroush polling started")

    while True:

        payload = {}

        if offset is not None:
            payload["offset"] = offset

        data = soroush_request("getUpdates", payload)

        if data is None:
            time.sleep(5)
            continue

        updates = extract_updates(data)

        if not updates:
            time.sleep(1)
            continue

        for update in updates:

            update_id = get_update_id(update)

            if update_id is not None:
                offset = update_id + 1

            extracted = extract_message(update)

            if extracted is None:
                continue

            chat_id, text = extracted

            logger.info("New message received")

            process_message(chat_id, text)


# =========================
# Start Polling Once
# =========================

_polling_lock = threading.Lock()
_polling_started = False


def start_polling():

    global _polling_started

    with _polling_lock:

        if _polling_started:
            return

        thread = threading.Thread(
            target=polling_loop,
            name="SoroushPolling",
            daemon=True
        )

        thread.start()

        _polling_started = True


start_polling()

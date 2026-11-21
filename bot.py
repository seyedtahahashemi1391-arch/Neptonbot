
import os
import time
import logging
import threading
import requests

from flask import Flask, jsonify

# ================ CONFIG ================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CF_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID")
CF_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")

MODEL = "@cf/zai-org/glm-4.7-flash"

SYSTEM_PROMPT = """
تو Nepton هستی، یک دستیار هوش مصنوعی فارسی زبان.
به زبان کاربر پاسخ بده.
پاسخ‌ها دقیق، حرفه‌ای و قابل فهم باشند.
از اطلاعات ساختگی خودداری کن.
"""

# ================ LOGGING ================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger("Nepton")

app = Flask(__name__)
session = requests.Session()

histories = {}
history_lock = threading.Lock()

# ================ ENVIRONMENT ================

def check_environment():

    variables = {
        "BOT_TOKEN": BOT_TOKEN,
        "CLOUDFLARE_ACCOUNT_ID": CF_ACCOUNT_ID,
        "CLOUDFLARE_API_TOKEN": CF_API_TOKEN
    }

    missing = [
        name for name, value in variables.items()
        if not value or not value.strip()
    ]

    if missing:
        raise RuntimeError(
            "Missing variables: " + ", ".join(missing)
        )


BOT_API = (
    f"https://api.splus.ir/bot{BOT_TOKEN}"
    if BOT_TOKEN else ""
)

AI_API = (
    f"https://api.cloudflare.com/client/v4/accounts/"
    f"{CF_ACCOUNT_ID}/ai/run/{MODEL}"
    if CF_ACCOUNT_ID else ""
)

# ================ CLOUDFLARE AI ================

def call_ai(chat_id, prompt):

    with history_lock:
        history = list(histories.get(chat_id, []))

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ] + history + [
        {"role": "user", "content": prompt}
    ]

    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messages": messages
    }

    for attempt in range(2):

        try:

            logger.info(
                "Cloudflare request attempt %s",
                attempt + 1
            )

            response = session.post(
                AI_API,
                headers=headers,
                json=payload,
                timeout=(15, 180)
            )

            if not response.ok:
                logger.error(
                    "Cloudflare HTTP %s: %s",
                    response.status_code,
                    response.text[:1000]
                )

            response.raise_for_status()

            data = response.json()

            if not data.get("success", True):
                raise RuntimeError(
                    "Cloudflare API error: "
                    + str(data.get("errors", []))
                )

            result = data.get("result", {})
            answer = result.get("response")

            if not answer:
                raise ValueError(
                    "Cloudflare returned an empty response"
                )

            with history_lock:

                history = histories.get(chat_id, [])

                history.extend([
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": answer}
                ])

                histories[chat_id] = history[-20:]

            return answer

        except requests.exceptions.Timeout:

            logger.warning(
                "Cloudflare timeout on attempt %s",
                attempt + 1
            )

            if attempt == 1:
                raise

            time.sleep(3)

    raise RuntimeError("Cloudflare request failed")


# ================ SOROUSH API ================

def splus_call(method, data=None):

    response = session.post(
        f"{BOT_API}/{method}",
        json=data or {},
        timeout=(15, 40)
    )

    response.raise_for_status()

    result = response.json()

    if result.get("ok") is False:
        raise RuntimeError(str(result))

    return result


def send_message(chat_id, message):

    for start in range(0, len(message), 3500):

        splus_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": message[start:start + 3500]
            }
        )


# ================ MESSAGE HANDLER ================

def process_update(update):

    message = update.get("message")

    if not message:
        return

    text = message.get("text")

    if not text:
        return

    chat = message.get("chat") or {}
    sender = message.get("from") or {}

    chat_id = chat.get("id") or sender.get("id")

    if not chat_id:
        return

    text = text.strip()

    if not text:
        return

    if text == "/start":

        send_message(
            chat_id,
            "سلام! من Nepton هستم.\n"
            "چطور می‌توانم کمکت کنم؟"
        )

        return

    if text == "/reset":

        with history_lock:
            histories.pop(chat_id, None)

        send_message(
            chat_id,
            "حافظه مکالمه پاک شد."
        )

        return

    try:

        logger.info("New message received")

        answer = call_ai(chat_id, text)

        send_message(chat_id, answer)

        logger.info("Response sent successfully")

    except requests.exceptions.Timeout:

        logger.exception("Request timeout")

        try:
            send_message(
                chat_id,
                "سرویس هوش مصنوعی دیر پاسخ داد. "
                "لطفاً چند لحظه دیگر دوباره تلاش کن."
            )
        except Exception:
            logger.exception("Failed to send timeout message")

    except Exception:

        logger.exception("Message processing failed")

        try:
            send_message(
                chat_id,
                "در پردازش پیام مشکلی ایجاد شد."
            )
        except Exception:
            logger.exception("Failed to send error message")


# ================ POLLING ================

def polling_worker():

    offset = None

    logger.info("Soroush polling started")

    while True:

        try:

            params = {}

            if offset is not None:
                params["offset"] = offset

            response = session.get(
                f"{BOT_API}/getUpdates",
                params=params,
                timeout=(15, 40)
            )

            response.raise_for_status()

            data = response.json()

            if data.get("ok") is False:
                raise RuntimeError(str(data))

            for update in data.get("result", []):

                update_id = update.get("update_id")

                if update_id is not None:
                    offset = update_id + 1

                process_update(update)

        except Exception:

            logger.exception("Polling error")
            time.sleep(5)


# ================ FLASK ================

@app.route("/")
def home():

    return "Nepton AI is running"


@app.route("/health")
def health():

    return jsonify({
        "status": "online",
        "provider": "Cloudflare Workers AI",
        "model": MODEL
    })


# ================ START ================

check_environment()

logger.info("Environment variables verified")
logger.info("AI provider: Cloudflare Workers AI")
logger.info("Model: %s", MODEL)

threading.Thread(
    target=polling_worker,
    daemon=True,
    name="soroush-polling"
).start()

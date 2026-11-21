
import os
import time
import logging
import threading
import requests

from flask import Flask, jsonify

# ---------------- CONFIG ----------------

BOT_TOKEN = os.getenv("70955089:Ia9F-M498sILiCXKaY3tV716x1MgUQw2HE0")

CF_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID")
CF_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")

MODEL = "@cf/zai-org/glm-4.7-flash"

BOT_API = f"https://api.splus.ir/bot{BOT_TOKEN}"

AI_API = (
    f"https://api.cloudflare.com/client/v4/accounts/"
    f"{CF_ACCOUNT_ID}/ai/v1/chat/completions"
)

SYSTEM_PROMPT = """
تو یک دستیار هوش مصنوعی فارسی‌زبان به نام Nepton هستی.
به زبان کاربر پاسخ بده.
پاسخ‌ها دقیق، مفید، حرفه‌ای و قابل فهم باشند.
در صورت نیاز توضیحات را مرحله‌بندی کن.
از اطلاعات ساختگی خودداری کن.
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

app = Flask(__name__)

session = requests.Session()

# Conversation memory
histories = {}
history_lock = threading.Lock()

# ---------------- CLOUDFLARE AI ----------------

def call_ai(chat_id, prompt):

    with history_lock:
        history = histories.get(chat_id, [])

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
        "model": MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1500
    }

    response = session.post(
        AI_API,
        headers=headers,
        json=payload,
        timeout=90
    )

    response.raise_for_status()

    data = response.json()

    answer = data["choices"][0]["message"]["content"]

    if not answer:
        raise ValueError("Empty AI response")

    with history_lock:
        history.append({
            "role": "user",
            "content": prompt
        })

        history.append({
            "role": "assistant",
            "content": answer
        })

        histories[chat_id] = history[-20:]

    return answer


# ---------------- SOROUSH API ----------------

def splus_call(method, data=None):

    url = f"{BOT_API}/{method}"

    response = session.post(
        url,
        json=data or {},
        timeout=40
    )

    response.raise_for_status()

    result = response.json()

    if result.get("ok") is False:
        raise RuntimeError(str(result))

    return result


def send_message(chat_id, text):

    # Soroush message length protection
    for i in range(0, len(text), 3500):

        chunk = text[i:i + 3500]

        splus_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk
            }
        )


# ---------------- MESSAGE PROCESSING ----------------

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
        logging.warning("Chat ID not found")
        return

    text = text.strip()

    if not text:
        return

    if text == "/start":
        send_message(
            chat_id,
            "سلام! من Nepton هستم.\n"
            "دستیار هوش مصنوعی شما با قدرت Cloudflare AI."
        )
        return

    if text == "/reset":
        with history_lock:
            histories.pop(chat_id, None)

        send_message(chat_id, "حافظه مکالمه پاک شد.")
        return

    try:
        logging.info("Processing message from %s", chat_id)

        answer = call_ai(chat_id, text)

        send_message(chat_id, answer)

        logging.info("AI response sent successfully")

    except requests.HTTPError as e:
        logging.exception("HTTP error: %s", e)
        send_message(
            chat_id,
            "خطا در ارتباط با سرویس هوش مصنوعی. لطفاً دوباره تلاش کن."
        )

    except Exception:
        logging.exception("Message processing failed")
        send_message(
            chat_id,
            "در پردازش پیام مشکلی پیش آمد."
        )


# ---------------- POLLING ----------------

def polling_worker():

    offset = None

    logging.info("Soroush polling started")

    while True:

        try:
            params = {}

            if offset is not None:
                params["offset"] = offset

            response = session.get(
                f"{BOT_API}/getUpdates",
                params=params,
                timeout=40
            )

            response.raise_for_status()

            data = response.json()

            if data.get("ok") is False:
                raise RuntimeError(str(data))

            updates = data.get("result", [])

            for update in updates:

                update_id = update.get("update_id")

                if update_id is not None:
                    offset = update_id + 1

                process_update(update)

        except Exception:
            logging.exception("Polling error")
            time.sleep(5)


# ---------------- FLASK ----------------

@app.route("/")
def home():
    return "Nepton AI Bot is running"


@app.route("/health")
def health():
    return jsonify({
        "status": "online",
        "ai_provider": "Cloudflare Workers AI",
        "model": MODEL
    })


# ---------------- START ----------------

if not BOT_TOKEN or not CF_ACCOUNT_ID or not CF_API_TOKEN:
    raise RuntimeError("Missing required environment variables")

threading.Thread(
    target=polling_worker,
    daemon=True,
    name="soroush-polling"
).start()

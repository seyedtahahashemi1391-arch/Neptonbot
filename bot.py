
# -*- coding: utf-8 -*-

import os
import re
import time
import logging
import requests

from flask import Flask, jsonify

# =========================================================
# 1. CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "70955089:Ia9F-M498sILiCXKaY3tV716x1MgUQw2HE0")

PORT = int(os.getenv("PORT", "8080"))

API_BASE = "https://api.splus.ir"
BOT_API = f"{API_BASE}/bot{BOT_TOKEN}"

# LLM7 + DeepSeek
LLM7_URL = "https://api.llm7.io/v1/chat/completions"

# قابل تغییر از تنظیمات Render
LLM7_MODEL = os.getenv(
    "LLM7_MODEL",
    "deepseek-v4-flash:0731"
)

CREATOR_NAME = "Nepton"
CREATOR_ID = "@t91m62"

# =========================================================
# 2. LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger("superbot")

# =========================================================
# 3. HTTP SESSION
# =========================================================

session = requests.Session()

session.headers.update({
    "Content-Type": "application/json",
    "User-Agent": "Nepton-Soroush-Bot/2.0"
})

# =========================================================
# 4. AI ENGINE - DEEPSEEK
# =========================================================

def call_ai(prompt):

    messages = [
        {
            "role": "system",
            "content": (
                "تو یک دستیار فارسی زبان هستی. "
                "پاسخ‌ها باید دقیق، صحیح، واضح و مفید باشند. "
                "به زبان کاربر پاسخ بده. "
                "از ارائه اطلاعات ساختگی خودداری کن."
            )
        },
        {
            "role": "user",
            "content": prompt
        }
    ]

    payload = {
        "model": LLM7_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 500
    }

    for attempt in range(1, 4):

        try:
            logger.info(
                f"DeepSeek request | Attempt {attempt}/3"
            )

            response = session.post(
                LLM7_URL,
                json=payload,
                timeout=(10, 45)
            )

            logger.info(
                f"LLM7 HTTP Status: {response.status_code}"
            )

            if response.status_code == 200:

                data = response.json()

                answer = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content")
                )

                if isinstance(answer, str) and answer.strip():

                    answer = re.sub(
                        r"<thought>.*?</thought>",
                        "",
                        answer,
                        flags=re.DOTALL
                    ).strip()

                    if answer:
                        logger.info("DeepSeek response received")
                        return answer

                logger.error("Empty or invalid AI response")

                return "پاسخ معتبری از هوش مصنوعی دریافت نشد."

            # خطاهای موقت
            if response.status_code in (408, 429, 500, 502, 503, 504):

                logger.warning(
                    f"Temporary API error: {response.text[:300]}"
                )

                if attempt < 3:
                    time.sleep(attempt * 2)
                    continue

            # خطاهای دائمی یا تنظیمات اشتباه
            logger.error(
                f"LLM7 API Error: {response.text[:500]}"
            )

            return "ارتباط با هوش مصنوعی با خطا مواجه شد."

        except requests.exceptions.Timeout:

            logger.warning(
                f"DeepSeek timeout | Attempt {attempt}/3"
            )

        except requests.exceptions.ConnectionError:

            logger.warning(
                f"Network error | Attempt {attempt}/3"
            )

        except (ValueError, KeyError, IndexError) as e:

            logger.exception(f"Invalid AI response: {e}")

            return "پاسخ دریافتی از هوش مصنوعی معتبر نیست."

        except Exception as e:

            logger.exception(f"Unexpected AI error: {e}")

            return "خطای غیرمنتظره در هوش مصنوعی رخ داد."

        if attempt < 3:
            time.sleep(attempt * 2)

    logger.error("All AI attempts failed")

    return "هوش مصنوعی فعلاً پاسخگو نیست. کمی بعد دوباره امتحان کن."

# =========================================================
# 5. SOROUSH API
# =========================================================

def splus_call(method, payload):

    try:

        response = session.post(
            f"{BOT_API}/{method}",
            json=payload,
            timeout=(10, 40)
        )

        if response.status_code != 200:

            logger.error(
                f"Soroush HTTP Error: {response.status_code}"
            )

            return {}

        data = response.json()

        if data.get("ok") is False:
            logger.error(f"Soroush API Error: {data}")

        return data

    except Exception as e:

        logger.exception(f"Soroush connection error: {e}")

        return {}


def send_message(chat_id, message):

    return splus_call(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": str(message)[:4000]
        }
    )

# =========================================================
# 6. MESSAGE HANDLER
# =========================================================

def process_update(update):

    try:

        if "message" not in update:
            return

        msg = update["message"]

        chat_id = msg["chat"]["id"]

        text = (msg.get("text") or "").strip()

        if not text:
            return

        logger.info(f"New message received: {text[:100]}")

        # Start command
        if text == "/start":

            send_message(
                chat_id,
                "سلام! ربات Nepton روشن است.\nهر سوالی داری بپرس."
            )

            return

        # Creator information
        if "سازندت" in text or "کی ساختت" in text:

            send_message(
                chat_id,
                f"سازنده من {CREATOR_NAME} است.\nآیدی: {CREATOR_ID}"
            )

            return

        # Processing message
        send_message(
            chat_id,
            "⏳ در حال پردازش..."
        )

        answer = call_ai(text)

        send_message(
            chat_id,
            answer
        )

        logger.info("Message processed successfully")

    except Exception as e:

        logger.exception(f"Message processing error: {e}")

# =========================================================
# 7. POLLING WORKER
# =========================================================

def polling_worker():

    offset = None

    logger.info("Soroush polling started")

    while True:

        try:

            payload = {
                "timeout": 30
            }

            if offset is not None:
                payload["offset"] = offset

            result = splus_call(
                "getUpdates",
                payload
            )

            updates = result.get("result", [])

            for update in updates:

                update_id = update.get("update_id")

                if update_id is None:
                    continue

                # ثبت شناسه قبل از پردازش
                offset = update_id + 1

                process_update(update)

        except Exception as e:

            logger.exception(
                f"Polling error: {e}"
            )

            time.sleep(5)

# =========================================================
# 8. FLASK SERVER
# =========================================================

app = Flask(__name__)


@app.route("/")
@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "bot": "Nepton",
        "ai": "DeepSeek via LLM7",
        "model": LLM7_MODEL
    }), 200


# =========================================================
# 9. STARTUP
# =========================================================

if __name__ == "__main__":

    logger.info("Starting Nepton Bot...")

    if not BOT_TOKEN:

        logger.critical("BOT_TOKEN is missing!")

        raise SystemExit(1)

    import threading

    worker = threading.Thread(
        target=polling_worker,
        daemon=True
    )

    worker.start()

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True
    )

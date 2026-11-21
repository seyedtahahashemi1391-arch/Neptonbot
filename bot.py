# -*- coding: utf-8 -*-
import os
import re
import time
import logging
import threading
import requests
from flask import Flask, jsonify

# =========================================================
# 1. CONFIG (تنظیمات)
# =========================================================
# ⚠️ فقط توکن ربات سروش رو بین دو تا "" زیر بذار (دیگه هیچ کلید دیگه‌ای لازم نیست)
BOT_TOKEN = "70955089:Ia9F-M498sILiCXKaY3tV716x1MgUQw2HE0"

PORT = int(os.getenv("PORT", "8080"))
API_BASE = "https://api.splus.ir"
BOT_API = f"{API_BASE}/bot{BOT_TOKEN}"

CREATOR_NAME = "Nepton"
CREATOR_ID = "@t91m62"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("superbot")

# =========================================================
# 2. AI ENGINE (استفاده از جدیدترین API رایگان)
# =========================================================
def call_ai(prompt):
    messages = [
        {"role": "system", "content": "تو یک دستیار فارسی‌زبان هستی. پاسخ‌ها باید کوتاه، دقیق و حداکثر ۲ جمله باشند. هرگز فرآیند فکر کردنت را ننویس."},
        {"role": "user", "content": prompt}
    ]
    
    # استفاده از درگاه جدید و پایدار Pollinations
    url = "https://gen.pollinations.ai/v1/chat/completions"
    payload = {
        "model": "openai",
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 150
    }
    
    try:
        logger.info("در حال ارسال به هوش مصنوعی...")
        resp = requests.post(url, json=payload, timeout=20)
        logger.info(f"وضعیت پاسخ: {resp.status_code}")
        
        if resp.status_code == 200:
            answer = resp.json()["choices"][0]["message"]["content"].strip()
            # پاکسازی فکرهای لورفته
            answer = re.sub(r'<thought>.*?</thought>', '', answer, flags=re.DOTALL).strip()
            return answer
        else:
            logger.error(f"خطای سرور: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"خطای شبکه: {e}")

    return "⚠️ هوش مصنوعی فعلاً در دسترس نیست. لطفاً بعداً امتحان کنید."

# =========================================================
# 3. SOROUSH HELPERS
# =========================================================
def splus_call(method, payload):
    try:
        r = requests.post(f"{BOT_API}/{method}", json=payload, timeout=20)
        return r.json()
    except Exception as e:
        logger.error(f"Soroush API Error: {e}")
        return {}

def send_message(chat_id, text):
    return splus_call("sendMessage", {"chat_id": chat_id, "text": str(text)[:4000]})

# =========================================================
# 4. UPDATE HANDLERS
# =========================================================
def process_update(update):
    if "message" not in update: return
    msg = update["message"]
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text: return

    if text == "/start":
        send_message(chat_id, "سلام! ربات روشن است. هر سوالی داری بپرس.")
        return
    if "سازندت" in text or "کی ساختت" in text:
        send_message(chat_id, f"👨‍💻 سازنده من {CREATOR_NAME} است.\nآیدی: {CREATOR_ID}")
        return

    send_message(chat_id, "⏳ در حال پردازش...")
    answer = call_ai(text)
    send_message(chat_id, answer)

def polling_worker():
    offset = None
    while True:
        try:
            payload = {"timeout": 30}
            if offset is not None: payload["offset"] = offset
            res = splus_call("getUpdates", payload)
            for update in res.get("result", []):
                offset = update["update_id"] + 1
                threading.Thread(target=process_update, args=(update,), daemon=True).start()
        except Exception as e:
            logger.error(f"Polling Error: {e}")
            time.sleep(3)

# =========================================================
# 5. FLASK & STARTUP
# =========================================================
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health(): return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    logger.info("Starting bot...")
    threading.Thread(target=polling_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)

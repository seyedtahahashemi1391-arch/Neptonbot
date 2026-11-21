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
# ⚠️ توکن سروش پلاس رو دقیقاً بین دو تا "" زیر بذار
BOT_TOKEN = "70955089:Ia9F-M498sILiCXKaY3tV716x1MgUQw2HE0"

# ⚠️ توکن گیت‌هاب (که با ghp_ شروع میشه) رو دقیقاً بین دو تا "" زیر بذار
GITHUB_TOKEN = "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"

PORT = int(os.getenv("PORT", "8080"))
API_BASE = "https://api.splus.ir"
BOT_API = f"{API_BASE}/bot{BOT_TOKEN}"

CREATOR_NAME = "Nepton"
CREATOR_ID = "@t91m62"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("superbot")

# =========================================================
# 2. AI ENGINE (GitHub Models -> Pollinations)
# =========================================================
def call_ai(prompt):
    messages = [
        {"role": "system", "content": "تو یک دستیار فارسی‌زبان هستی. پاسخ‌ها باید کوتاه، دقیق و حداکثر ۲ جمله باشند. هرگز فرآیند فکر کردنت را ننویس."},
        {"role": "user", "content": prompt}
    ]
    
    # اولویت اول: GitHub Models
    if GITHUB_TOKEN and GITHUB_TOKEN != "توکن_گیت_هاب_رو_اینجا_بذار":
        try:
            logger.info("Trying GitHub Models...")
            url = "https://models.inference.ai.azure.com/chat/completions"
            headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Content-Type": "application/json"}
            payload = {"model": "gpt-4o-mini", "messages": messages, "temperature": 0.1, "max_tokens": 150}
            
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            logger.info(f"GitHub Status: {resp.status_code}")
            if resp.status_code == 200:
                answer = resp.json()["choices"][0]["message"]["content"].strip()
                answer = re.sub(r'<thought>.*?</thought>', '', answer, flags=re.DOTALL).strip()
                return answer
        except Exception as e:
            logger.error(f"GitHub Error: {e}")

    # اولویت دوم: Pollinations (بدون کلید)
    try:
        logger.info("Trying Pollinations...")
        url = "https://text.pollinations.ai/openai"
        payload = {"model": "openai", "messages": messages}
        resp = requests.post(url, json=payload, timeout=15)
        logger.info(f"Pollinations Status: {resp.status_code}")
        if resp.status_code == 200:
            answer = resp.json()["choices"][0]["message"]["content"].strip()
            answer = re.sub(r'<thought>.*?</thought>', '', answer, flags=re.DOTALL).strip()
            return answer
    except Exception as e:
        logger.error(f"Pollinations Error: {e}")

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

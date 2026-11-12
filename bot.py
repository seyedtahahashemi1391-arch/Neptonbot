# -*- coding: utf-8 -*-
import os
import re
import json
import time
import logging
import threading
import traceback
from queue import Queue, Empty
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify

# =========================================================
# 1. CONFIG (تنظیمات)
# =========================================================
BOT_TOKEN = "70955089:I0xa1mOtAb90m_qlX0SNCDNfPFc-jZzL8ec"
PORT = int(os.getenv("PORT", "8080"))
API_BASE = "https://api.splus.ir"
BOT_API = f"{API_BASE}/bot{BOT_TOKEN}"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("superbot")

# =========================================================
# 2. AI PROVIDERS (بدون نیاز به کلید - جایگزین سرویس‌های خراب)
# =========================================================
def get_providers():
    return [
        # ۱. Pollinations (بدون کلید، پایدار)
        {
            "name": "Pollinations",
            "url": "https://text.pollinations.ai/openai",
            "model": "openai",
            "headers": {"Content-Type": "application/json"},
            "timeout": 5
        },
        # ۲. OVHcloud (بدون کلید، نود اروپا)
        {
            "name": "OVHcloud",
            "url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
            "model": "Qwen3Guard-Gen-0.6B",
            "headers": {"Content-Type": "application/json", "Authorization": "Bearer unused"},
            "timeout": 5
        },
        # ۳. KeylessAI (بدون کلید)
        {
            "name": "KeylessAI",
            "url": "https://keylessai.thryx.workers.dev/v1/chat/completions",
            "model": "gpt-4o",
            "headers": {"Content-Type": "application/json"},
            "timeout": 6
        }
    ]

SYSTEM_PROMPT = """تو یک دستیار هوشمند فارسی‌زبان هستی.
قوانین: حداکثر ۱ تا ۲ جمله کوتاه و مستقیم جواب بده. هرگز فرآیند فکر کردنت را چاپ نکن. اگر نمی‌دانی بگو نمی‌دانم."""

def provider_request(provider, messages):
    headers = provider["headers"]
    payload = {
        "model": provider["model"], 
        "messages": messages, 
        "temperature": 0.1, 
        "max_tokens": 80, 
        "stream": False
    }
    
    start = time.time()
    response = requests.post(provider["url"], json=payload, headers=headers, timeout=provider["timeout"])
    response.raise_for_status()
    
    data = response.json()
    answer = data["choices"][0]["message"]["content"].strip()
    
    # فیلتر پاکسازی توهمات مدل
    answer = re.sub(r'<thought>.*?</thought>', '', answer, flags=re.DOTALL).strip()
    answer = re.sub(r'Thinking Process:.*', '', answer, flags=re.DOTALL).strip()
    
    if not answer or len(answer) < 2:
        raise RuntimeError("Empty answer")
        
    logger.info(f"✅ پاسخ از {provider['name']} در {round(time.time() - start, 2)}s")
    return answer

def call_ai(user_id, prompt):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt}
    ]
    for p in get_providers():
        try:
            return provider_request(p, messages)
        except Exception as e:
            logger.warning(f"❌ {p['name']} شکست خورد: {str(e)}")
    return None

# =========================================================
# 3. SOROUSH HELPERS
# =========================================================
def splus_call(method, payload):
    try:
        r = requests.post(f"{BOT_API}/{method}", json=payload, timeout=15)
        return r.json()
    except Exception as e:
        logger.error(f"API Error {method}: {e}")
        return {}

def send_message(chat_id, text):
    if len(text) > 4000: text = text[:4000]
    return splus_call("sendMessage", {"chat_id": chat_id, "text": text})

# =========================================================
# 4. MESSAGE HANDLING & POLLING
# =========================================================
def process_update(update):
    if "message" not in update: return
    msg = update["message"]
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = (msg.get("text") or "").strip()
    if not text: return

    if text == "/start":
        send_message(chat_id, "سلام! ربات آماده است. هر سوالی داری بپرس.")
        return

    send_message(chat_id, "⏳ در حال پردازش...")
    answer = call_ai(user_id, text)
    if answer:
        send_message(chat_id, answer)
    else:
        send_message(chat_id, "⚠️ تمام سرویس‌های رایگان AI در دسترس نیستند. لطفاً چند لحظه بعد دوباره امتحان کنید.")

def polling_worker():
    offset = None
    while True:
        try:
            payload = {"timeout": 30, "limit": 20}
            if offset is not None: payload["offset"] = offset
            res = splus_call("getUpdates", payload)
            for update in res.get("result", []):
                offset = update["update_id"] + 1
                process_update(update)
        except Exception:
            time.sleep(3)

# =========================================================
# 5. FLASK HEALTHCHECK (برای Render)
# =========================================================
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200

def main():
    threading.Thread(target=polling_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)

if __name__ == "__main__":
    main()

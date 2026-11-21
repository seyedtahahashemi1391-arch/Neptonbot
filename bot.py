# -*- coding: utf-8 -*-
import os, re, json, time, logging, threading
import requests
from flask import Flask, jsonify

# ⚠️⚠️⚠️ توکن ربات رو دقیقاً بین دو تا "" زیر بذار ⚠️⚠️⚠️
BOT_TOKEN = "70955089:Ia9F-M498sILiCXKaY3tV716x1MgUQw2HE0" 

PORT = int(os.getenv("PORT", "8080"))
API_BASE = "https://api.splus.ir"
BOT_API = f"{API_BASE}/bot{BOT_TOKEN}"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("superbot")

def call_ai(prompt):
    url = "https://text.pollinations.ai/openai"
    payload = {
        "model": "openai", 
        "messages": [
            {"role": "system", "content": "پاسخ کوتاه و فارسی بده."},
            {"role": "user", "content": prompt}
        ]
    }
    try:
        logger.info("Sending request to Pollinations...")
        response = requests.post(url, json=payload, timeout=15)
        logger.info(f"Pollinations Status: {response.status_code}")
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Pollinations Error: {e}")
        return "خطا در اتصال به هوش مصنوعی."

def splus_call(method, payload):
    try:
        r = requests.post(f"{BOT_API}/{method}", json=payload, timeout=20)
        return r.json()
    except Exception: return {}

def send_message(chat_id, text):
    return splus_call("sendMessage", {"chat_id": chat_id, "text": text})

def polling_worker():
    offset = None
    while True:
        try:
            payload = {"timeout": 30}
            if offset is not None: payload["offset"] = offset
            res = splus_call("getUpdates", payload)
            for update in res.get("result", []):
                offset = update["update_id"] + 1
                if "message" in update:
                    msg = update["message"]
                    chat_id = msg["chat"]["id"]
                    text = msg.get("text", "")
                    if text == "/start":
                        send_message(chat_id, "ربات روشن است.")
                    else:
                        send_message(chat_id, "در حال فکر کردن...")
                        answer = call_ai(text)
                        send_message(chat_id, answer)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(3)

app = Flask(__name__)

@app.route("/")
def home(): return "Alive", 200

if __name__ == "__main__":
    threading.Thread(target=polling_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=Pتوکن

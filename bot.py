# -*- coding: utf-8 -*-
"""
SUPER BOT - Final Complete Edition (Fixed AI Providers)
"""

import os
import re
import json
import time
import logging
import threading
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify

# =========================================================
# 1. CONFIG (تنظیمات)
# =========================================================
# ⚠️⚠️⚠️ توکن رو دقیقاً بین دو تا "" زیر بذار ⚠️⚠️⚠️
BOT_TOKEN = "70955089:Ia9F-M498sILiCXKaY3tV716x1MgUQw2HE0"

PORT = int(os.getenv("PORT", "8080"))
API_BASE = "https://api.splus.ir"
BOT_API = f"{API_BASE}/bot{BOT_TOKEN}"

DATA_DIR = os.getenv("DATA_DIR", ".")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
REMINDERS_FILE = os.path.join(DATA_DIR, "reminders.json")

CREATOR_NAME = "Nepton"
CREATOR_ID = "@t91m62"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("superbot")

os.makedirs(DATA_DIR, exist_ok=True)

# =========================================================
# 2. STORAGE
# =========================================================
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False)
    except Exception as e: logger.error(f"Save error: {e}")

HISTORY = load_json(HISTORY_FILE, {})
REMINDERS = load_json(REMINDERS_FILE, [])

# =========================================================
# 3. AI ENGINE (سرویس‌های جدید و پایدار)
# =========================================================
def get_providers():
    return [
        # ۱. KeylessAI (بدون کلید، پایداری بالا)
        {
            "name": "KeylessAI",
            "url": "https://keylessai.thryx.workers.dev/v1/chat/completions",
            "model": "gpt-4o-mini",
            "headers": {"Content-Type": "application/json"},
            "timeout": 7
        },
        # ۲. LLMF (بدون کلید، دارای Fallback داخلی)
        {
            "name": "LLMF",
            "url": "https://llmf.ai/v1/chat/completions",
            "model": "auto",
            "headers": {"Content-Type": "application/json"},
            "timeout": 8
        }
    ]

SYSTEM_PROMPT = """تو یک دستیار هوشمند فارسی‌زبان هستی.
قوانین فوق‌العاده سخت‌گیرانه:
۱. پاسخ تو باید حداکثر ۱ تا ۲ جمله کوتاه، مستقیم و منطقی باشد.
۲. هرگز فرآیند فکر کردن (Reasoning/Thinking) خود را به هیچ زبانی ننویس.
۳. هرگز حرف‌های کاربر را تکرار نکن و مقدمه‌چینی نکن.
۴. اگر جواب را نمی‌دانی فقط بنویس: نمی‌دانم."""

def provider_request(provider, messages):
    payload = {
        "model": provider["model"], 
        "messages": messages, 
        "temperature": 0.1, 
        "max_tokens": 80, 
        "stream": False
    }
    
    response = requests.post(provider["url"], json=payload, headers=provider["headers"], timeout=provider["timeout"])
    response.raise_for_status()
    
    data = response.json()
    answer = data["choices"][0]["message"]["content"].strip()
    
    # فیلتر پاکسازی توهمات
    answer = re.sub(r'<thought>.*?</thought>', '', answer, flags=re.DOTALL).strip()
    answer = re.sub(r'Thinking Process:.*', '', answer, flags=re.DOTALL).strip()
    answer = re.sub(r'The user is asking.*?\n\n', '', answer, flags=re.DOTALL).strip()
    
    if not answer or len(answer) < 2:
        raise RuntimeError("Empty answer")
    return answer

def call_ai(user_id, prompt):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    hist = HISTORY.get(str(user_id), [])
    messages.extend(hist[-3:])
    messages.append({"role": "user", "content": prompt})
    
    for p in get_providers():
        try:
            answer = provider_request(p, messages)
            hist.append({"role": "user", "content": prompt})
            hist.append({"role": "assistant", "content": answer})
            HISTORY[str(user_id)] = hist[-10:]
            save_json(HISTORY_FILE, HISTORY)
            return answer
        except Exception as e:
            logger.warning(f"❌ {p['name']} failed: {str(e)}")
            continue
    return None

# =========================================================
# 4. SOROUSH HELPERS
# =========================================================
def splus_call(method, payload):
    try:
        r = requests.post(f"{BOT_API}/{method}", json=payload, timeout=15)
        return r.json()
    except Exception as e:
        logger.error(f"API Error {method}: {e}")
        return {}

def send_message(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": str(text)[:4000]}
    if keyboard: payload["reply_markup"] = keyboard
    return splus_call("sendMessage", payload)

def send_keyboard(chat_id, text, buttons_list):
    return send_message(chat_id, text, {"inline_keyboard": buttons_list})

# =========================================================
# 5. MENUS & TOOLS
# =========================================================
def main_menu():
    return [
        [{"text": "🤖 هوش مصنوعی", "callback_data": "menu:ai"}, {"text": "🌤 هواشناسی", "callback_data": "menu:weather"}],
        [{"text": "🔎 جستجوی وب", "callback_data": "menu:search"}, {"text": "🧹 پاک کردن حافظه", "callback_data": "settings:clear"}],
        [{"text": "👨‍💻 سازنده", "callback_data": "menu:creator"}]
    ]

def get_weather(city):
    try:
        geo = requests.get(f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=fa", timeout=5).json()
        if not geo.get("results"): return "شهر موردنظر پیدا نشد."
        loc = geo["results"][0]
        
        w = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={loc['latitude']}&longitude={loc['longitude']}&current=temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m&timezone=auto", timeout=5).json()
        cur = w["current"]
        return (f"🌤 وضعیت هوای {loc['name']}\n\n🌡 دما: {cur.get('temperature_2m', '?')}°C\n🤔 دمای احساسی: {cur.get('apparent_temperature', '?')}°C\n💧 رطوبت: {cur.get('relative_humidity_2m', '?')}%\n💨 سرعت باد: {cur.get('wind_speed_10m', '?')} km/h")
    except Exception:
        return "خطا در دریافت هواشناسی."

def search_web(query):
    try:
        res = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, headers={"User-Agent": "Mozilla/5.0"}, timeout=7)
        soup = BeautifulSoup(res.text, 'html.parser')
        results = []
        for item in soup.select('.result__body')[:3]:
            title = item.select_one('.result__a')
            snippet = item.select_one('.result__snippet')
            if title and snippet:
                results.append(f"{title.text.strip()}: {snippet.text.strip()}")
        return "\n\n".join(results)
    except Exception:
        return None

def create_reminder(user_id, chat_id, text):
    t = text.lower()
    now = datetime.now()
    if "پس‌فردا" in t or "پس فردا" in t: day = now.date() + timedelta(days=2)
    elif "فردا" in t: day = now.date() + timedelta(days=1)
    else: day = now.date()
    
    match = re.search(r"ساعت\s*(\d{1,2})(?:[:٫.](\d{1,2}))?", t)
    if not match: return "زمان مشخص نیست.\nمثال: یادم بنداز فردا ساعت ۵ زنگ بزن"
    
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    rem_text = re.sub(r"یادم بنداز|یادآور|یادآوری کن|فردا|پس.?فردا|امروز|ساعت\s*\d{1,2}(?:[:٫.]\d{1,2})?", "", text, flags=re.IGNORECASE).strip(" ،,؛;")
    
    target = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
    if target <= now: return "زمان باید در آینده باشد."
    
    REMINDERS.append({"user_id": str(user_id), "chat_id": chat_id, "text": rem_text, "timestamp": target.timestamp()})
    save_json(REMINDERS_FILE, REMINDERS)
    return f"⏰ یادآور ثبت شد!\nموضوع: {rem_text}\nزمان: {target.strftime('%Y-%m-%d %H:%M')}"

# =========================================================
# 6. UPDATE HANDLERS
# =========================================================
def process_update(update):
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text = (msg.get("text") or "").strip()
        if not text: return

        if text == "/start":
            send_keyboard(chat_id, "سلام! به Super Bot خوش آمدی.", main_menu())
            return

        if text == "/clear":
            HISTORY[str(user_id)] = []
            save_json(HISTORY_FILE, HISTORY)
            send_message(chat_id, "🧹 حافظه پاک شد.")
            return

        t_lower = text.lower()
        if "سازندت" in t_lower or "کی ساختت" in t_lower:
            send_message(chat_id, f"👨‍💻 سازنده من **{CREATOR_NAME}** است.\n🆔 آیدی: {CREATOR_ID}")
            return

        if "یادم بنداز" in t_lower or "یادآور" in t_lower:
            send_message(chat_id, create_reminder(user_id, chat_id, text))
            return

        if "هواشناسی" in t_lower:
            city = re.sub(r"هواشناسی|آب.?وهوا|دمای هوا", "", text, flags=re.IGNORECASE).strip(" ؟?،")
            if not city: send_message(chat_id, "نام شهر را هم بنویس.")
            else: send_message(chat_id, get_weather(city))
            return

        if "جستجو" in t_lower or "بگرد" in t_lower:
            send_message(chat_id, "🔍 در حال جستجو...")
            results = search_web(text)
            if results:
                answer = call_ai(user_id, f"بر اساس این نتایج پاسخ بده:\n{results}\n\nسوال: {text}")
                send_message(chat_id, answer)
            else:
                send_message(chat_id, "جستجو ناموفق بود.")
            return

        # چت عادی
        send_message(chat_id, "⏳ در حال پردازش...")
        answer = call_ai(user_id, text)
        if answer: send_message(chat_id, answer)
        else: send_message(chat_id, "⚠️ هوش مصنوعی فعلاً در دسترس نیست.")

    elif "callback_query" in update:
        cb = update["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        user_id = cb["from"]["id"]
        data = cb["data"]
        
        if data == "menu:ai": send_message(chat_id, "سؤالت را بنویس.")
        elif data == "menu:weather": send_message(chat_id, "نام شهر را بنویس (مثلاً: تهران)")
        elif data == "menu:search": send_message(chat_id, "عبارت موردنظر را با کلمه جستجو بفرست.")
        elif data == "menu:creator": send_message(chat_id, f"👨‍💻 سازنده من **{CREATOR_NAME}** است.\n🆔 آیدی: {CREATOR_ID}")
        elif data == "settings:clear":
            HISTORY[str(user_id)] = []
            save_json(HISTORY_FILE, HISTORY)
            send_message(chat_id, "🧹 حافظه پاک شد.")

# =========================================================
# 7. WORKERS & POLLING
# =========================================================
def polling_worker():
    offset = None
    while True:
        try:
            payload = {"timeout": 30, "limit": 20}
            if offset is not None: payload["offset"] = offset
            res = splus_call("getUpdates", payload)
            for update in res.get("result", []):
                offset = update["update_id"] + 1
                threading.Thread(target=process_update, args=(update,), daemon=True).start()
        except Exception:
            time.sleep(3)

def reminder_worker():
    while True:
        try:
            now = time.time()
            due = [r for r in REMINDERS if r["timestamp"] <= now]
            if due:
                REMINDERS[:] = [r for r in REMINDERS if r["timestamp"] > now]
                save_json(REMINDERS_FILE, REMINDERS)
                for r in due:
                    send_message(r["chat_id"], f"⏰ یادآوری:\n{r['text']}")
        except Exception:
            pass
        time.sleep(10)

# =========================================================
# 8. FLASK HEALTHCHECK (برای Render)
# =========================================================
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200

# =========================================================
# 9. STARTUP
# =========================================================
def main():
    print("Super Bot starting...", flush=True)
    threading.Thread(target=polling_worker, daemon=True).start()
    threading.Thread(target=reminder_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)

if __name__ == "__main__":
    main()

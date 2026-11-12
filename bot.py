# -*- coding: utf-8 -*-
"""
SUPER BOT - Final Async Edition (Render Ready + Creator Info)
Dependencies: aiohttp, requests, beautifulsoup4, Flask
"""

import os
import re
import time
import sqlite3
import logging
import asyncio
import requests
import threading
from datetime import datetime, timedelta
from collections import deque

import aiohttp
from bs4 import BeautifulSoup
from flask import Flask

# =========================================================
# 1. CONFIG (تنظیمات)
# =========================================================
# ⚠️ توکن ربات خود را اینجا بگذارید
BOT_TOKEN = os.getenv("BOT_TOKEN", "70955089:Eg66Kyf45u1tchK7O5wMzXiwy2-TIe3gTYc").strip()
DB_FILE = "superbot.db"
PORT = int(os.getenv("PORT", "8080"))

# اطلاعات سازنده
CREATOR_NAME = "Nepton"
CREATOR_ID = "@t91m62"

if not BOT_TOKEN or BOT_TOKEN == "توکن_خودت_رو_اینجا_بذار":
    raise RuntimeError("BOT_TOKEN تنظیم نشده است.")

BOT_API = f"https://api.splus.ir/bot{BOT_TOKEN}"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("superbot")

SEEN_UPDATES = deque(maxlen=5000)

# =========================================================
# 2. FLASK WEB SERVER (برای زنده نگه داشتن در Render)
# =========================================================
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Super Bot is alive!"

@flask_app.route('/health')
def health():
    return "OK", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=PORT, use_reloader=False)

# =========================================================
# 3. DATABASE (دیتابیس SQLite با حالت WAL)
# =========================================================
def get_db_conn():
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL;") # جلوگیری از قفل شدن در ترافیک بالا
    return conn

def init_db():
    conn = get_db_conn()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        first_name TEXT,
        message_count INTEGER DEFAULT 0,
        last_seen TEXT DEFAULT ''
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        role TEXT,
        content TEXT
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        text TEXT,
        remind_at INTEGER
    )''')
    conn.commit()
    conn.close()

init_db()

async def db_execute(query, params=()):
    def _run():
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        conn.close()
    await asyncio.to_thread(_run)

async def db_fetch_one(query, params=()):
    def _run():
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(query, params)
        row = cursor.fetchone()
        conn.close()
        return row
    return await asyncio.to_thread(_run)

async def db_fetch_all(query, params=()):
    def _run():
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        return rows
    return await asyncio.to_thread(_run)

# =========================================================
# 4. API CALLS & HELPERS
# =========================================================
async def api_call(method, payload):
    url = f"{BOT_API}/{method}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, timeout=35 if method == "getUpdates" else 15) as response:
                return await response.json()
        except Exception as e:
            logger.error(f"API Error {method}: {e}")
            return {}

async def send_message(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": str(text)[:4000]}
    if keyboard: payload["reply_markup"] = keyboard
    return await api_call("sendMessage", payload)

async def update_user_stats(user_id, first_name):
    today = datetime.now().strftime("%Y-%m-%d")
    await db_execute("""
        INSERT INTO users (user_id, first_name, message_count, last_seen) 
        VALUES (?, ?, 1, ?) 
        ON CONFLICT(user_id) DO UPDATE SET 
        message_count = message_count + 1, last_seen = ?
    """, (user_id, first_name, today, today))

# =========================================================
# 5. AI ENGINE (پاسخ‌های کوتاه و مستقیم)
# =========================================================
async def ask_ai(user_id, prompt):
    # دریافت ۱۰ پیام آخر برای حفظ حافظه
    rows = await db_fetch_all("SELECT role, content FROM history WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,))
    history = [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    
    # پرامپت سیستمی بسیار سخت‌گیرانه برای کوتاه بودن
    system_prompt = (
        "تو یک دستیار هوشمند فارسی‌زبان هستی. قوانین سخت‌گیرانه تو: \n"
        "۱. پاسخ تو باید فوق‌العاده کوتاه، تمیز و مستقیم باشد (حداکثر ۲ تا ۳ جمله).\n"
        "۲. هرگز مقدمه‌چینی نکن (مثلاً نگو 'سوال خوبی پرسیدی' یا 'بگذار توضیح دهم').\n"
        "۳. بلافاصله و مستقیماً به اصل جواب بده.\n"
        "۴. از تکرار حرف‌های کاربر و توضیحات اضافی پرهیز کن.\n"
        "۵. فقط در صورت نیاز از ایموجی استفاده کن."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    providers = [
        {"url": "https://api.kilo.ai/api/gateway/chat/completions", "model": "kilo-auto/free"},
        {"url": "https://api.llm7.io/v1/chat/completions", "model": "gpt-oss:20b"},
        {"url": "https://text.pollinations.ai/openai", "model": "openai"}
    ]

    async with aiohttp.ClientSession() as session:
        for p in providers:
            try:
                # محدودیت توکن و Temperature پایین برای جلوگیری از زیاده‌گویی
                payload = {
                    "model": p["model"], 
                    "messages": messages, 
                    "max_tokens": 200, 
                    "temperature": 0.2
                }
                async with session.post(p["url"], json=payload, timeout=12) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        answer = data["choices"][0]["message"]["content"].strip()
                        
                        # ذخیره در دیتابیس
                        await db_execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, "user", prompt))
                        await db_execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, "assistant", answer))
                        await db_execute("DELETE FROM history WHERE user_id = ? AND id NOT IN (SELECT id FROM history WHERE user_id = ? ORDER BY id DESC LIMIT 50)", (user_id, user_id))
                        
                        return answer
            except Exception:
                continue
    return "⚠️ سرورهای هوش مصنوعی فعلاً پاسخ نمی‌دهند."

async def search_web(query):
    async with aiohttp.ClientSession() as session:
        try:
            url = "https://html.duckduckgo.com/html/"
            async with session.post(url, data={'q': query}, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10) as resp:
                text = await resp.text()
                soup = BeautifulSoup(text, 'html.parser')
                results = []
                for item in soup.select('.result__body')[:3]:
                    title = item.select_one('.result__a')
                    snippet = item.select_one('.result__snippet')
                    if title and snippet:
                        results.append(f"{title.get_text(strip=True)}: {snippet.get_text(strip=True)}")
                return "\n\n".join(results) if results else ""
        except Exception:
            return ""

async def get_weather(city):
    async with aiohttp.ClientSession() as session:
        try:
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=fa"
            async with session.get(geo_url, timeout=8) as r:
                geo = await r.json()
                if not geo.get("results"): return "شهر پیدا نشد."
                loc = geo["results"][0]
            
            w_url = f"https://api.open-meteo.com/v1/forecast?latitude={loc['latitude']}&longitude={loc['longitude']}&current=temperature_2m,weather_code,wind_speed_10m"
            async with session.get(w_url, timeout=8) as r:
                w = await r.json()
                cur = w["current"]
                return f"🌤 هوای {loc['name']}:\n🌡 دما: {cur['temperature_2m']}°C\n💨 باد: {cur['wind_speed_10m']} km/h"
        except Exception:
            return "خطا در دریافت هواشناسی."

# =========================================================
# 6. REMINDERS (یادآور)
# =========================================================
async def create_reminder(user_id, chat_id, text):
    t = text.lower()
    now = datetime.now()
    if "پس‌فردا" in t or "پس فردا" in t: day = now.date() + timedelta(days=2)
    elif "فردا" in t: day = now.date() + timedelta(days=1)
    else: day = now.date()
    
    match = re.search(r"ساعت\s*(\d{1,2})(?:[:٫.](\d{1,2}))?", t)
    if not match: return "زمان مشخص نیست.\nمثال: یادم بنداز فردا ساعت ۵ زنگ بزن"
    
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if hour > 23 or minute > 59: return "زمان نامعتبر است."
    
    rem_text = re.sub(r"یادم بنداز|یادآور|یادآوری کن|فردا|پس.?فردا|امروز|ساعت\s*\d{1,2}(?:[:٫.]\d{1,2})?", "", text, flags=re.IGNORECASE).strip(" ،,؛;")
    if not rem_text: rem_text = "یادآوری شما"
    
    target = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
    if target <= now: return "زمان باید در آینده باشد."
    
    await db_execute("INSERT INTO reminders (user_id, chat_id, text, remind_at) VALUES (?, ?, ?, ?)", 
                     (user_id, chat_id, rem_text, int(target.timestamp())))
    return f"⏰ یادآور ثبت شد!\nموضوع: {rem_text}\nزمان: {target.strftime('%Y-%m-%d %H:%M')}"

async def reminder_worker():
    while True:
        now = int(time.time())
        due = await db_fetch_all("SELECT id, chat_id, text FROM reminders WHERE remind_at <= ?", (now,))
        for rem_id, chat_id, text in due:
            await send_message(chat_id, f"⏰ **یادآوری:**\n{text}")
            await db_execute("DELETE FROM reminders WHERE id = ?", (rem_id,))
        await asyncio.sleep(15)

# =========================================================
# 7. INTENT DETECTION (تشخیص هدف)
# =========================================================
def detect_intent(text):
    t = text.lower()
    if "سازندت" in t or "کی ساختت" in t or "سازنده" in t: return "creator"
    if re.search(r"(یادم بنداز|یادآور|یادآوری کن)", t): return "reminder"
    if re.search(r"(هواشناسی|آب.?وهوا|دمای هوا)", t): return "weather"
    if re.search(r"(جستجو کن|بگرد|اخبار|خبر|قیمت|امروز|آخرین وضعیت)", t): return "search"
    return "chat"

# =========================================================
# 8. UPDATE HANDLERS
# =========================================================
async def process_update(update):
    update_id = update.get("update_id")
    if update_id in SEEN_UPDATES: return
    if update_id: SEEN_UPDATES.append(update_id)

    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        first_name = msg["from"].get("first_name", "کاربر")
        text = msg.get("text", "").strip()

        if not text: return

        await update_user_stats(user_id, first_name)

        if text == "/start":
            # منوی اصلی با دکمه سازنده
            keyboard = {"inline_keyboard": [
                [{"text": "🌤 هواشناسی", "callback_data": "weather"}, {"text": "🧹 پاک کردن حافظه", "callback_data": "clear"}],
                [{"text": "👨‍💻 سازنده", "callback_data": "creator"}]
            ]}
            await send_message(chat_id, f"سلام {first_name} عزیز! 👋\nمن یک دستیار هوشمند هستم. هر سوالی داری بپرس، جستجو کن یا برام یادآور بساز.", keyboard)
            return

        if text == "/clear":
            await db_execute("DELETE FROM history WHERE user_id = ?", (user_id,))
            await send_message(chat_id, "🧹 حافظه مکالمه شما پاک شد.")
            return

        intent = detect_intent(text)

        # پاسخ به سوال "سازندت کیه"
        if intent == "creator":
            creator_text = f"👨‍💻 سازنده من **{CREATOR_NAME}** است.\n🆔 آیدی: {CREATOR_ID}"
            await send_message(chat_id, creator_text)
            return

        if intent == "reminder":
            res = await create_reminder(user_id, chat_id, text)
            await send_message(chat_id, res)
            return
            
        if intent == "weather":
            city = re.sub(r"هواشناسی|آب.?وهوا|دمای هوا", "", text, flags=re.IGNORECASE).strip(" ؟?،")
            if city: await send_message(chat_id, await get_weather(city))
            else: await send_message(chat_id, "نام شهر را هم بنویسید (مثال: هواشناسی تهران)")
            return

        if intent == "search":
            await send_message(chat_id, "🔍 در حال جستجو در اینترنت...")
            web_results = await search_web(text)
            if web_results:
                prompt = f"بر اساس نتایج جستجوی زیر به سوال کاربر پاسخ بده:\n\n{web_results}\n\nسوال کاربر: {text}"
                answer = await ask_ai(user_id, prompt)
                await send_message(chat_id, answer)
            else:
                await send_message(chat_id, "جستجوی زنده ناموفق بود. اما بر اساس دانش خودم پاسخ می‌دهم:")
                answer = await ask_ai(user_id, text)
                await send_message(chat_id, answer)
            return

        # چت عادی
        await send_message(chat_id, "⏳ در حال پردازش...")
        answer = await ask_ai(user_id, text)
        await send_message(chat_id, answer)

    elif "callback_query" in update:
        cb = update["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        user_id = cb["from"]["id"]
        data = cb["data"]

        if data == "weather":
            await send_message(chat_id, "نام شهر را بنویسید (مثلاً: تهران)")
        elif data == "clear":
            await db_execute("DELETE FROM history WHERE user_id = ?", (user_id,))
            await send_message(chat_id, "🧹 حافظه مکالمه شما پاک شد.")
        elif data == "creator":
            creator_text = f"👨‍💻 سازنده من **{CREATOR_NAME}** است.\n🆔 آیدی: {CREATOR_ID}"
            await send_message(chat_id, creator_text)

async def safe_process_update(update):
    try:
        await process_update(update)
    except Exception as e:
        logger.error(f"Error processing update: {e}")

# =========================================================
# 9. POLLING LOOP
# =========================================================
async def polling_worker():
    offset = None
    logger.info("🚀 ربات روشن شد و در حال دریافت پیام‌هاست...")
    while True:
        try:
            payload = {"timeout": 30, "limit": 50}
            if offset is not None:
                payload["offset"] = offset

            response = await api_call("getUpdates", payload)
            updates = response.get("result", [])
            
            if isinstance(updates, list):
                for update in updates:
                    update_id = update.get("update_id")
                    if update_id is not None:
                        offset = update_id + 1
                    asyncio.create_task(safe_process_update(update))
            else:
                await asyncio.sleep(3)
                
        except Exception as e:
            logger.error(f"Polling Error: {e}")
            await asyncio.sleep(3)

# =========================================================
# 10. STARTUP
# =========================================================
async def main():
    # اجرای وب‌سرور Flask در یک ترد جداگانه (برای Render)
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info(f"🌐 Flask server running on port {PORT}")
    
    # اجرای همزمان یادآورها و دریافت پیام‌ها
    asyncio.create_task(reminder_worker())
    await polling_worker()

if __name__ == "__main__":
    asyncio.run(main())

# -*- coding: utf-8 -*-
"""
SUPER BOT (Private Edition) - Fast & Strict Edition
"""

import os
import re
import json
import time
import socket
import ipaddress
import logging
import threading
import secrets
import hashlib
import random
import traceback
from queue import Queue, Full, Empty
from collections import OrderedDict, deque
from datetime import datetime, timedelta
from urllib.parse import urlparse, unquote

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# 1. CONFIG (تنظیمات)
# =========================================================
BOT_TOKEN = "70955089:I0xa1mOtAb90m_qlX0SNCDNfPFc-jZzL8ec"
PORT = int(os.getenv("PORT", "8080"))
API_BASE = os.getenv("SPLUS_API", "https://api.splus.ir").rstrip("/")
DATA_DIR = os.getenv("DATA_DIR", ".")

HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
REMINDERS_FILE = os.path.join(DATA_DIR, "reminders.json")
POLLS_FILE = os.path.join(DATA_DIR, "polls.json")

CREATOR_NAME = "Nepton"
CREATOR_ID = "@t91m62"

MAX_TEXT = 4000
MAX_QUEUE = 100
MAX_HISTORY_USERS = 1000
MAX_HISTORY_MESSAGES = 40
MAX_SEEN = 5000
HISTORY_TTL = 30 * 86400
SEARCH_CACHE_TTL = 300
REQUEST_TIMEOUT = 12
POLL_TIMEOUT = 30
WORKERS = 1
START_TIME = time.time()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN تنظیم نشده است.")

os.makedirs(DATA_DIR, exist_ok=True)

# =========================================================
# 2. STRUCTURED LOGGING
# =========================================================
class JSONFormatter(logging.Formatter):
    def format(self, record):
        data = {"time": datetime.utcnow().isoformat(), "level": record.levelname, "message": record.getMessage()}
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)

logger = logging.getLogger("superbot")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger.addHandler(handler)

# =========================================================
# 3. THREAD-SAFE JSON STORAGE
# =========================================================
storage_lock = threading.RLock()

def load_json(path, default_type):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, type(default_type)):
                return default_type
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default_type

def save_json(path, data):
    with storage_lock:
        temp = path + ".tmp"
        try:
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp, path)
        except Exception:
            pass

HISTORY = load_json(HISTORY_FILE, {})
STATS = load_json(STATS_FILE, {})
SETTINGS = load_json(SETTINGS_FILE, {})
REMINDERS = load_json(REMINDERS_FILE, [])
POLLS = load_json(POLLS_FILE, {})

# =========================================================
# 4. MEMORY MANAGEMENT
# =========================================================
memory_lock = threading.RLock()
SEEN_UPDATES = OrderedDict()
RATE_LIMITS = {}
SEARCH_CACHE = {}

def remember_update(update_id):
    now = time.time()
    with memory_lock:
        if update_id in SEEN_UPDATES:
            return False
        SEEN_UPDATES[update_id] = now
        while len(SEEN_UPDATES) > MAX_SEEN:
            SEEN_UPDATES.popitem(last=False)
    return True

def get_history(user_id):
    with memory_lock:
        item = HISTORY.get(str(user_id), {})
        if isinstance(item, list):
            item = {"messages": item, "summary": "", "turns": len(item)//2, "updated": time.time()}
            HISTORY[str(user_id)] = item
            
        if not isinstance(item, dict) or not item:
            return {"messages": [], "summary": "", "turns": 0, "updated": time.time()}
            
        if time.time() - item.get("updated", 0) > HISTORY_TTL:
            HISTORY.pop(str(user_id), None)
            save_json(HISTORY_FILE, HISTORY)
            return {"messages": [], "summary": "", "turns": 0, "updated": time.time()}
        return item

def update_history(user_id, role, content):
    uid = str(user_id)
    with memory_lock:
        item = get_history(uid)
        if "messages" not in item or not isinstance(item["messages"], list):
            item["messages"] = []
            
        item["messages"].append({"role": role, "content": str(content)[:MAX_TEXT]})
        item["messages"] = item["messages"][-MAX_HISTORY_MESSAGES:]
        if role == "user":
            item["turns"] = item.get("turns", 0) + 1
        item["updated"] = time.time()
        HISTORY[uid] = item
        if len(HISTORY) > MAX_HISTORY_USERS:
            oldest = min(HISTORY, key=lambda k: HISTORY[k].get("updated", 0))
            HISTORY.pop(oldest, None)
        save_json(HISTORY_FILE, HISTORY)

def clear_history(user_id):
    with memory_lock:
        HISTORY.pop(str(user_id), None)
        save_json(HISTORY_FILE, HISTORY)

# =========================================================
# 5. HTTP CLIENT
# =========================================================
def http_request(method, url, *, timeout=REQUEST_TIMEOUT, attempts=3, **kwargs):
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < attempts:
                    time.sleep(min(0.7 * (2 ** attempt), 6))
                    continue
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if isinstance(exc, requests.HTTPError):
                raise RuntimeError(f"HTTP Error {exc.response.status_code}")
            if attempt + 1 < attempts:
                time.sleep(min(0.7 * (2 ** attempt), 6))
    raise RuntimeError(f"HTTP request failed: {type(last_error).__name__}")

def http_json(method, url, **kwargs):
    response = http_request(method, url, **kwargs)
    try:
        return response.json()
    except ValueError:
        raise RuntimeError("Invalid JSON response")

# =========================================================
# 6. SOROUSH PLUS API
# =========================================================
BOT_API = f"{API_BASE}/bot{BOT_TOKEN}"

def splus_call(method, payload=None):
    url = f"{BOT_API}/{method}"
    return http_json("POST", url, json=payload or {}, timeout=35 if method == "getUpdates" else 15, attempts=2)

def send_message(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": str(text)[:MAX_TEXT]}
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        return splus_call("sendMessage", payload)
    except Exception:
        logger.exception("Failed to send message")
        return None

def answer_callback(callback_id, text=""):
    try:
        splus_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:150]})
    except Exception:
        pass

def send_keyboard(chat_id, text, buttons_list):
    return send_message(chat_id, text, {"inline_keyboard": buttons_list})

# =========================================================
# 7. MENUS
# =========================================================
def main_menu():
    return [
        [{"text": "🤖 هوش مصنوعی", "callback_data": "menu:ai"}, {"text": "🌤 هواشناسی", "callback_data": "menu:weather"}],
        [{"text": "🔎 جستجوی وب", "callback_data": "menu:search"}, {"text": "🌐 ترجمه", "callback_data": "menu:translate"}],
        [{"text": "⏰ یادآور", "callback_data": "menu:reminder"}, {"text": "📊 آمار", "callback_data": "menu:stats"}],
        [{"text": "⚙️ تنظیمات", "callback_data": "menu:settings"}, {"text": "🧠 حافظه", "callback_data": "menu:memory"}],
        [{"text": "🎲 تاس", "callback_data": "menu:dice"}, {"text": "👨‍💻 سازنده", "callback_data": "menu:creator"}]
    ]

def settings_menu(user_id):
    pref = SETTINGS.get(str(user_id), {})
    style = pref.get("style", "short")
    web_enabled = pref.get("web", True)
    return [
        [{"text": "سبک پاسخ: " + ("کوتاه" if style == "short" else "مفصل"), "callback_data": "settings:style"}],
        [{"text": "جستجو: " + ("روشن" if web_enabled else "خاموش"), "callback_data": "settings:web"}],
        [{"text": "🗑 پاک کردن حافظه", "callback_data": "settings:clear"}],
        [{"text": "↩️ بازگشت", "callback_data": "menu:home"}]
    ]

# =========================================================
# 8. AI PROVIDER ROUTER (بهینه‌شده برای سرعت و منطق)
# =========================================================
PROVIDER_HEALTH = {}

def get_providers():
    # تایم‌اوت‌ها به شدت کم شدند تا اگر سرویسی خراب بود، ربات معطل نشود
    providers = [
        {"name": "Kilo", "url": "https://api.kilo.ai/api/gateway/chat/completions", "model": "kilo-auto/free", "key": "", "timeout": 4},
        {"name": "LLM7", "url": "https://api.llm7.io/v1/chat/completions", "model": "gpt-oss:20b", "key": "", "timeout": 5}
    ]
    pollinations_key = os.getenv("POLLINATIONS_API_KEY", "")
    if pollinations_key:
        providers.append({"name": "Pollinations", "url": "https://gen.pollinations.ai/v1/chat/completions", "model": "openai/gpt-4o-mini", "key": pollinations_key, "timeout": 8})
    return providers

SYSTEM_PROMPT = """
تو یک دستیار هوشمند و بسیار منطقی هستی.
قوانین حیاتی:
۱. پاسخ تو باید فوق‌العاده کوتاه باشد (حداکثر ۱ تا ۲ جمله).
۲. هرگز مقدمه‌چینی نکن، توضیح اضافه نده و حرف کاربر را تکرار نکن.
۳. فقط و فقط جواب مستقیم و منطقی بده.
۴. اگر جواب را نمی‌دانی، فقط بنویس: "نمی‌دانم".
"""

def provider_request(provider, messages):
    headers = {"Content-Type": "application/json"}
    if provider["key"]:
        headers["Authorization"] = "Bearer " + provider["key"]
    
    # توکن را از 200 به 100 کاهش دادیم تا مدل زور نزند طولانی بنویسد
    # تمپریچر را به 0.1 کاهش دادیم تا مدل خیال‌پردازی و چرت و پرت نگوید
    payload = {
        "model": provider["model"], 
        "messages": messages, 
        "temperature": 0.1, 
        "max_tokens": 100, 
        "stream": False
    }
    
    start = time.time()
    response = http_json("POST", provider["url"], json=payload, headers=headers, timeout=provider["timeout"], attempts=1)
    choices = response.get("choices", [])
    if not choices:
        raise RuntimeError("Empty AI response")
        
    message = choices[0].get("message", {})
    answer = message.get("content") or message.get("reasoning") or ""
    if isinstance(answer, list):
        answer = " ".join(str(x.get("text", "")) for x in answer if isinstance(x, dict))
    answer = str(answer).strip()
    if not answer:
        raise RuntimeError("Empty AI content")
        
    PROVIDER_HEALTH[provider["name"]] = {"status": "online", "latency": round(time.time() - start, 2), "last_success": time.time()}
    return answer

def call_ai(user_id, prompt, extra_system=None):
    history = get_history(user_id)
    pref = SETTINGS.get(str(user_id), {})
    style = pref.get("style", "short")
    system = SYSTEM_PROMPT
    
    if style == "detailed":
        system = "تو یک دستیار هوشمند هستی. پاسخ‌ها را مفصل بده."
        
    if extra_system:
        system += "\n" + extra_system

    messages = [{"role": "system", "content": system}]
    
    if history.get("summary"):
        messages.append({"role": "system", "content": "خلاصه مکالمات قبلی:\n" + history["summary"]})
    
    # حافظه رو از ۸ پیام به ۳ پیام کاهش دادیم تا هم سریع‌تر شه هم گیج نشه
    messages.extend(history.get("messages", [])[-3:])
    messages.append({"role": "user", "content": str(prompt)[:MAX_TEXT]})
    
    for provider in get_providers():
        try:
            return provider_request(provider, messages)
        except Exception:
            PROVIDER_HEALTH[provider["name"]] = {"status": "offline", "last_failure": time.time()}
    return None

def maybe_summarize(user_id):
    item = get_history(user_id)
    turns = item.get("turns", 0)
    if turns < 20 or turns % 20 != 0: return
    if item.get("summarized_turn", 0) >= turns: return
    
    transcript = item.get("messages", [])[-30:]
    text_block = "\n".join(f"{m['role']}: {m['content']}" for m in transcript)
    prompt = "مکالمه زیر را در حداکثر 120 کلمه خلاصه کن.\n\n" + text_block
    
    summary = None
    for provider in get_providers():
        try:
            summary = provider_request(provider, [{"role": "system", "content": "خلاصه‌ساز مکالمه هستی."}, {"role": "user", "content": prompt}])
            break
        except Exception:
            continue
            
    with memory_lock:
        current = get_history(user_id)
        if summary:
            current["summary"] = summary[:1500]
            current["messages"] = current["messages"][-8:]
        current["summarized_turn"] = turns
        current["updated"] = time.time()
        HISTORY[str(user_id)] = current
        save_json(HISTORY_FILE, HISTORY)

# =========================================================
# 9. VALIDATION & INTENT
# =========================================================
def clean_text(text):
    if not isinstance(text, str): return ""
    text = text.replace("\x00", "")
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
    return text.strip()[:MAX_TEXT]

def allow_request(user_id):
    uid = str(user_id)
    now = time.time()
    with memory_lock:
        if uid not in RATE_LIMITS: RATE_LIMITS[uid] = deque()
        events = RATE_LIMITS[uid]
        while events and now - events[0] > 3600: events.popleft()
        if events and now - events[-1] < 2: return False, "لطفاً کمی بین درخواست‌ها فاصله بگذار."
        if sum(1 for t in events if now - t < 60) >= 20: return False, "سقف درخواست‌های دقیقه‌ای پر شده است."
        if len(events) >= 100: return False, "سقف درخواست‌های ساعتی پر شده است."
        events.append(now)
    return True, ""

def detect_intent(text):
    t = text.lower()
    if "سازندت" in t or "کی ساختت" in t or "سازنده" in t: return "creator"
    if re.search(r"(یادم بنداز|یادآور|یادآوری کن)", t): return "reminder"
    if re.search(r"(هواشناسی|آب.?وهوا|دمای هوا|پیش.?بینی هوا)", t): return "weather"
    if re.search(r"(قیمت دلار|نرخ دلار|قیمت ارز)", t): return "currency"
    if re.search(r"(ترجمه کن|معنی کن|translate)", t): return "translate"
    if re.search(r"(جستجو کن|بگرد|اخبار|خبر|قیمت|امروز|آخرین وضعیت)", t): return "search"
    if re.search(r"https?://\S+", t): return "link"
    return "chat"

# =========================================================
# 10. WEB SEARCH
# =========================================================
def search_web(query):
    key = hashlib.sha256(query.lower().encode("utf-8")).hexdigest()
    cached = SEARCH_CACHE.get(key)
    if cached and time.time() - cached[0] < SEARCH_CACHE_TTL: return cached[1]
    
    engines = [("https://html.duckduckgo.com/html/", "ddg"), ("https://www.bing.com/search", "bing")]
    for base, engine in engines:
        try:
            response = http_request(
                "POST" if engine == "ddg" else "GET", 
                base, 
                data={"q": query} if engine == "ddg" else None, 
                params={"q": query} if engine == "bing" else None, 
                headers={"User-Agent": "Mozilla/5.0"}, 
                timeout=9, 
                attempts=1
            )
            soup = BeautifulSoup(response.text, 'html.parser')
            results = []
            
            if engine == "ddg":
                items = soup.select('.result__body')[:3] # فقط ۳ نتیجه برای سرعت
                for item in items:
                    title_tag = item.select_one('.result__a')
                    snippet_tag = item.select_one('.result__snippet')
                    if title_tag and snippet_tag:
                        url = title_tag.get('href', '')
                        if "uddg=" in url:
                            parsed = urlparse(url)
                            url = unquote(dict(x.split("=", 1) for x in parsed.query.split("&") if "=" in x).get("uddg", url))
                        results.append({"title": title_tag.text.strip(), "snippet": snippet_tag.text.strip(), "url": url})
            else:
                items = soup.select('.b_algo')[:3]
                for item in items:
                    title_tag = item.select_one('h2 a')
                    snippet_tag = item.select_one('.b_caption p')
                    if title_tag and snippet_tag:
                        results.append({"title": title_tag.text.strip(), "snippet": snippet_tag.text.strip(), "url": title_tag.get('href', '')})
            
            if results:
                SEARCH_CACHE[key] = (time.time(), results)
                return results
        except Exception:
            pass
    return None

# =========================================================
# 11. WEATHER & CURRENCY
# =========================================================
def get_weather(city):
    geo = http_json("GET", "https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1, "language": "fa", "format": "json"}, timeout=9)
    locations = geo.get("results", [])
    if not locations: return "شهر موردنظر پیدا نشد."
    location = locations[0]
    data = http_json("GET", "https://api.open-meteo.com/v1/forecast", params={"latitude": location["latitude"], "longitude": location["longitude"], "current": "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m", "timezone": "auto"}, timeout=9)
    current = data.get("current", {})
    return (f"🌤 وضعیت هوای {location['name']}\n\n🌡 دما: {current.get('temperature_2m', '?')}°C\n🤔 دمای احساسی: {current.get('apparent_temperature', '?')}°C\n💧 رطوبت: {current.get('relative_humidity_2m', '?')}%\n💨 سرعت باد: {current.get('wind_speed_10m', '?')} km/h")

def get_currency():
    url = os.getenv("DOLLAR_API_URL", "").strip()
    if not url: return "API قیمت دلار تنظیم نشده است."
    try:
        data = http_json("GET", url, timeout=8)
        return "💵 اطلاعات دریافتی قیمت دلار:\n" + json.dumps(data, ensure_ascii=False)[:1500]
    except Exception:
        return "دریافت قیمت دلار در حال حاضر ناموفق بود."

# =========================================================
# 12. SAFE LINK FETCHING
# =========================================================
def validate_public_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname: return False
    host = parsed.hostname.lower()
    if host in ("localhost", "localhost.localdomain"): return False
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        for address in addresses:
            if not ipaddress.ip_address(address[4][0]).is_global: return False
    except Exception: return False
    return True

def fetch_link(url):
    if not validate_public_url(url): return None
    try:
        response = http_request("GET", url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, attempts=1, allow_redirects=False, stream=True)
        if "text/html" not in response.headers.get("Content-Type", ""): return None
        
        chunks, total = [], 0
        for chunk in response.iter_content(8192):
            if not chunk: continue
            total += len(chunk)
            if total > 1_500_000: break
            chunks.append(chunk)
            
        soup = BeautifulSoup(b"".join(chunks).decode(response.encoding or "utf-8", errors="replace"), 'html.parser')
        for tag in soup(["script", "style", "noscript", "svg", "iframe", "nav", "footer"]):
            tag.decompose()
        
        return " ".join(soup.stripped_strings)[:10000]
    except Exception:
        return None

# =========================================================
# 13. REMINDERS
# =========================================================
def create_reminder(user_id, chat_id, text):
    t = text.lower()
    now = datetime.now()
    if "پس‌فردا" in t or "پس فردا" in t: day = now.date() + timedelta(days=2)
    elif "فردا" in t: day = now.date() + timedelta(days=1)
    else: day = now.date()
    
    match = re.search(r"ساعت\s*(\d{1,2})(?:[:٫.](\d{1,2}))?", t)
    if not match: return "زمان یادآور مشخص نیست.\nمثال: یادم بنداز فردا ساعت ۵ زنگ بزن"
    
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if hour > 23 or minute > 59: return "زمان واردشده معتبر نیست."
    
    reminder_text = re.sub(r"یادم بنداز|یادآور|یادآوری کن|فردا|پس.?فردا|امروز|ساعت\s*\d{1,2}(?:[:٫.]\d{1,2})?", "", text, flags=re.IGNORECASE).strip(" ،,؛;")
    if not reminder_text: reminder_text = "یادآوری شما"
    
    target = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
    if target <= now: return "زمان یادآور باید در آینده باشد."
    
    with storage_lock:
        REMINDERS.append({"id": secrets.token_hex(5), "user_id": str(user_id), "chat_id": chat_id, "text": reminder_text[:300], "timestamp": target.timestamp()})
        save_json(REMINDERS_FILE, REMINDERS)
        
    return "⏰ یادآور ثبت شد!\n\n" + f"موضوع: {reminder_text}\nزمان: {target.strftime('%Y-%m-%d %H:%M')}\n\nزمان‌بندی بر اساس ساعت سرور است."

def reminder_worker():
    while True:
        try:
            now = time.time()
            due = []
            with storage_lock:
                remaining = []
                for reminder in REMINDERS:
                    if reminder["timestamp"] <= now: due.append(reminder)
                    else: remaining.append(reminder)
                if due:
                    REMINDERS[:] = remaining
                    save_json(REMINDERS_FILE, REMINDERS)
            
            for reminder in due:
                try:
                    send_message(reminder["chat_id"], "⏰ یادآوری!\n\n" + reminder["text"])
                    time.sleep(0.5)
                except Exception: pass
        except Exception: pass
        time.sleep(15)

# =========================================================
# 14. USER STATS
# =========================================================
def update_stats(user_id):
    uid = str(user_id)
    with storage_lock:
        item = STATS.setdefault(uid, {"messages": 0, "first_seen": time.time(), "last_seen": time.time()})
        item["messages"] += 1
        item["last_seen"] = time.time()
        if item["messages"] % 5 == 0: save_json(STATS_FILE, STATS)

def get_stats(user_id):
    item = STATS.get(str(user_id), {})
    return f"📊 آمار استفاده شما\n\nتعداد پیام‌ها: {item.get('messages', 0)}\nمدت فعالیت ربات: {int((time.time() - START_TIME) / 60)} دقیقه"

# =========================================================
# 15. COMMAND & CALLBACK HANDLERS
# =========================================================
def handle_command(chat_id, user_id, text):
    parts = text.split(maxsplit=1)
    command = parts[0].split("@")[0].lower()
    argument = parts[1] if len(parts) > 1 else ""

    if command in ("/start", "/menu"):
        send_keyboard(chat_id, "سلام! به Super Bot خوش آمدی.\nیک قابلیت را انتخاب کن یا مستقیماً پیام بده.", main_menu())
        return True
    if command in ("/help", "/راهنما"):
        send_message(chat_id, "🤖 راهنمای Super Bot\n\n/start - شروع\n/help - راهنما\n/settings - تنظیمات\n/stats - آمار\n/clear - پاک کردن حافظه\n/weather تهران - هواشناسی\n/tr متن - ترجمه")
        return True
    if command == "/settings":
        send_keyboard(chat_id, "⚙️ تنظیمات ربات", settings_menu(user_id))
        return True
    if command == "/clear":
        clear_history(user_id)
        send_message(chat_id, "حافظه مکالمه پاک شد.")
        return True
    if command == "/stats":
        send_message(chat_id, get_stats(user_id))
        return True
    if command == "/weather":
        city = argument.strip()
        if not city: send_message(chat_id, "مثال: /weather تهران")
        else:
            try: send_message(chat_id, get_weather(city))
            except Exception: send_message(chat_id, "دریافت هواشناسی فعلاً ممکن نیست.")
        return True
    if command in ("/tr", "/translate"):
        if not argument: send_message(chat_id, "متن موردنظر را بعد از دستور وارد کن.")
        else:
            result = call_ai(user_id, "متن زیر را ترجمه کن:\n" + argument)
            send_message(chat_id, result or "سرویس ترجمه موقتاً در دسترس نیست.")
        return True
    return False

def handle_callback(update):
    callback = update.get("callback_query", {})
    callback_id, data = callback.get("id"), callback.get("data", "")
    chat_id = callback.get("message", {}).get("chat", {}).get("id")
    user_id = callback.get("from", {}).get("id")
    if not chat_id or not user_id: return
    
    answer_callback(callback_id, "در حال پردازش...")

    if data == "menu:home": send_keyboard(chat_id, "منوی اصلی", main_menu())
    elif data == "menu:settings": send_keyboard(chat_id, "تنظیمات", settings_menu(user_id))
    elif data == "menu:ai": send_message(chat_id, "سؤالت را بنویس؛ هوش مصنوعی پاسخ می‌دهد.")
    elif data == "menu:weather": send_message(chat_id, "نام شهر را ارسال کن.\nمثال: تهران")
    elif data == "menu:search": send_message(chat_id, "عبارت موردنظر را با کلمه جستجو ارسال کن.")
    elif data == "menu:translate": send_message(chat_id, "متن را با دستور /tr ارسال کن.")
    elif data == "menu:reminder": send_message(chat_id, "مثال:\nیادم بنداز فردا ساعت ۵ تماس بگیر")
    elif data == "menu:stats": send_message(chat_id, get_stats(user_id))
    elif data == "menu:memory": send_message(chat_id, "برای پاک کردن حافظه از /clear استفاده کن.")
    elif data == "menu:dice": send_message(chat_id, f"🎲 نتیجه تاس: {random.randint(1, 6)}")
    elif data == "menu:creator": send_message(chat_id, f"👨‍💻 سازنده من **{CREATOR_NAME}** است.\n🆔 آیدی: {CREATOR_ID}")
    elif data == "settings:style":
        pref = SETTINGS.setdefault(str(user_id), {})
        pref["style"] = "detailed" if pref.get("style", "short") == "short" else "short"
        save_json(SETTINGS_FILE, SETTINGS)
        send_keyboard(chat_id, "سبک پاسخ تغییر کرد.", settings_menu(user_id))
    elif data == "settings:web":
        pref = SETTINGS.setdefault(str(user_id), {})
        pref["web"] = not pref.get("web", True)
        save_json(SETTINGS_FILE, SETTINGS)
        send_keyboard(chat_id, "تنظیم جستجو تغییر کرد.", settings_menu(user_id))
    elif data == "settings:clear":
        clear_history(user_id)
        send_message(chat_id, "حافظه پاک شد.")

# =========================================================
# 16. MESSAGE PROCESSING
# =========================================================
def process_message(message):
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    if chat_id is None or user_id is None: return

    text = clean_text(message.get("text") or message.get("caption") or "")
    if message.get("voice"):
        send_message(chat_id, "🎤 ویس دریافت شد؛ اما تبدیل خودکار صوت به متن فعال نیست.")
        return
    if not text: return

    allowed, reason = allow_request(user_id)
    if not allowed:
        send_message(chat_id, reason)
        return

    update_stats(user_id)

    if text.startswith("/"):
        if handle_command(chat_id, user_id, text): return

    intent = detect_intent(text)

    if intent == "creator":
        send_message(chat_id, f"👨‍💻 سازنده من **{CREATOR_NAME}** است.\n🆔 آیدی: {CREATOR_ID}")
        return
    if intent == "reminder":
        send_message(chat_id, create_reminder(user_id, chat_id, text))
        return
    if intent == "weather":
        city = re.sub(r"هواشناسی|آب.?وهوا|دمای هوا|پیش.?بینی هوا", "", text, flags=re.IGNORECASE).strip(" ؟?،")
        if not city: send_message(chat_id, "نام شهر را هم بنویس.")
        else:
            try: send_message(chat_id, get_weather(city))
            except Exception: send_message(chat_id, "سرویس هواشناسی فعلاً پاسخ نمی‌دهد.")
        return
    if intent == "currency":
        send_message(chat_id, get_currency())
        return
    if intent == "translate":
        result = call_ai(user_id, "متن زیر را به زبان مناسب ترجمه کن:\n" + text)
        send_message(chat_id, result or "سرویس ترجمه موقتاً در دسترس نیست.")
        return
    if intent == "link":
        match = re.search(r"https?://\S+", text)
        if match:
            content = fetch_link(match.group(0).rstrip(".,،)"))
            if not content: send_message(chat_id, "نتوانستم محتوای این لینک را دریافت کنم.")
            else:
                result = call_ai(user_id, "این محتوای صفحه را خلاصه کن:\n" + content)
                send_message(chat_id, result or "خلاصه‌سازی موقتاً ممکن نیست.")
            return

    pref = SETTINGS.get(str(user_id), {})
    if intent == "search" and pref.get("web", True):
        results = search_web(text)
        if not results:
            send_message(chat_id, "جستجوی زنده ناموفق بود؛ در حال حاضر اطلاعات زنده ندارم.")
            return
        result_text = "\n\n".join(f"{i+1}. {x['title']}\n{x['snippet']}" for i, x in enumerate(results))
        prompt = "بر اساس نتایج زیر به سؤال کاربر پاسخ بده. منابع را ذکر کن.\n\n" + result_text + "\n\nسؤال کاربر:\n" + text
        answer = call_ai(user_id, prompt)
        send_message(chat_id, answer or ("نتایج جستجو دریافت شدند، اما هوش مصنوعی پاسخ نداد:\n\n" + result_text[:2000]))
        return

    # چت عادی
    update_history(user_id, "user", text)
    answer = call_ai(user_id, text)
    if not answer:
        send_message(chat_id, "سرویس‌های هوش مصنوعی فعلاً مشغول هستند. کمی بعد امتحان کن.")
        return
    update_history(user_id, "assistant", answer)
    send_message(chat_id, answer)

    item = get_history(user_id)
    if item.get("turns", 0) >= 20 and item.get("turns", 0) % 20 == 0:
        try: maybe_summarize(user_id)
        except Exception: pass

# =========================================================
# 17. JOB QUEUE & POLLING
# =========================================================
JOB_QUEUE = Queue(maxsize=MAX_QUEUE)

def worker():
    while True:
        try:
            update = JOB_QUEUE.get(timeout=5)
            try:
                if "callback_query" in update: handle_callback(update)
                elif "message" in update: process_message(update["message"])
            except Exception:
                traceback.print_exc()
                message = update.get("message", {})
                chat_id = message.get("chat", {}).get("id")
                if chat_id:
                    try: send_message(chat_id, "یک خطای موقت رخ داد؛ دوباره امتحان کن.")
                    except Exception: pass
            finally:
                JOB_QUEUE.task_done()
        except Empty: continue
        except Exception: pass

def polling_worker():
    offset = None
    while True:
        try:
            payload = {"timeout": POLL_TIMEOUT, "limit": 50}
            if offset is not None: payload["offset"] = offset
            response = splus_call("getUpdates", payload)
            updates = response.get("result", response.get("data", []))
            if not isinstance(updates, list):
                time.sleep(3)
                continue
            for update in updates:
                update_id = update.get("update_id")
                if update_id is not None and update_id in SEEN_UPDATES: continue
                try: JOB_QUEUE.put_nowait(update)
                except Full: break
                if update_id is not None:
                    remember_update(update_id)
                    offset = update_id + 1
        except Exception:
            time.sleep(3)

# =========================================================
# 18. FLASK HEALTHCHECK
# =========================================================
app = Flask(__name__)

@app.route("/")
def home(): return jsonify({"service": "Super Bot", "status": "running"})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "uptime": int(time.time() - START_TIME), "queue": JOB_QUEUE.qsize()})

# =========================================================
# 19. STARTUP
# =========================================================
def main():
    print("Super Bot starting...", flush=True)
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=polling_worker, daemon=True).start()
    threading.Thread(target=reminder_worker, daemon=True).start()
    
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)

if __name__ == "__main__":
    main()

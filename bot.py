# -*- coding: utf-8 -*-
"""
SUPER BOT (Private Edition) - Final Stable
Dependencies: requests, flask
Run:
    BOT_TOKEN=70955089:Eg66Kyf45u1tchK7O5wMzXiwy2-TIe3gTYc python bot.py
"""

import os
import re
import json
import time
import html
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
from html.parser import HTMLParser

import requests
from flask import Flask, jsonify

# =========================================================
# 1. CONFIG (تنظیمات)
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "8080"))
API_BASE = os.getenv("SPLUS_API", "https://api.splus.ir").rstrip("/")
DATA_DIR = os.getenv("DATA_DIR", ".")

HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
REMINDERS_FILE = os.path.join(DATA_DIR, "reminders.json")
POLLS_FILE = os.path.join(DATA_DIR, "polls.json")

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
# 2. STRUCTURED LOGGING (سیستم لاگ)
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
# 3. THREAD-SAFE JSON STORAGE (ذخیره‌سازی امن و ضدضربه)
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
# 4. MEMORY MANAGEMENT (مدیریت حافظه)
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
# 5. HTTP CLIENT (درخواست‌های امن با تلاش مجدد)
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
            # فقط برای خطاهای شبکه یا سرور تلاش مجدد میکنیم
            if isinstance(exc, requests.HTTPError):
                raise RuntimeError(f"HTTP Error {exc.response.status_code}")
            if attempt + 1 < attempts:
                time.sleep(min(0.7 * (2 ** attempt), 6))
    raise RuntimeError(f"HTTP request failed: {type(last_error).name}")

def http_json(method, url, **kwargs):
    response = http_request(method, url, **kwargs)
    try:
        return response.json()
    except ValueError:
        raise RuntimeError("Invalid JSON response")

# =========================================================
# 6. SOROUSH PLUS API (ارتباط با سروش)
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
    except Exception as e:
        logger.exception("Failed to send message to Soroush")
        return None

def answer_callback(callback_id, text=""):
    try:
        splus_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:150]})
    except Exception:
        pass

def send_keyboard(chat_id, text, buttons_list):
    # دکمه‌ها باید به صورت لیست لیست‌ها پاس داده شوند
    return send_message(chat_id, text, {"inline_keyboard": buttons_list})

# =========================================================
# 7. MENUS (منوها) - خروجی این توابع باید "لیست" باشد
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
# 8. AI PROVIDER ROUTER (مدیریت هوش مصنوعی)
# =========================================================
PROVIDER_HEALTH = {}

def get_providers():
    providers = [
        {"name": "Kilo", "url": "https://api.kilo.ai/api/gateway/chat/completions", "model": "kilo-auto/free", "key": "", "timeout": 9},
        {"name": "LLM7", "url": "https://api.llm7.io/v1/chat/completions", "model": "gpt-oss:20b", "key": "", "timeout": 11}
    ]
    pollinations_key = os.getenv("POLLINATIONS_API_KEY", "")
    if pollinations_key:
        providers.append({"name": "Pollinations", "url": "https://gen.pollinations.ai/v1/chat/completions", "model": "openai/gpt-4o-mini", "key": pollinations_key, "timeout": 12})
    return providers

SYSTEM_PROMPT = """
تو یک دستیار هوشمند فارسی‌زبان هستی.
قوانین:
- دقیق، منطقی و صادق باش.
- پاسخ پیش‌فرض کوتاه و کاربردی باشد.
- در صورت نیاز مراحل را شماره‌گذاری کن.
- اطلاعات زنده و تأییدنشده را جعل نکن.
- اگر اطلاعات کافی نداری، شفاف بگو.
"""

def provider_request(provider, messages):
    headers = {"Content-Type": "application/json"}
    if provider["key"]:
        headers["Authorization"] = "Bearer " + provider["key"]
    payload = {"model": provider["model"], "messages": messages, "temperature": 0.6, "max_tokens": 450, "stream": False}
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
    if style == "short":
        system += "\nپاسخ را کوتاه و مستقیم نگه دار."
    else:
        system += "\nپاسخ را مفصل، دقیق و مرحله‌ای ارائه کن."
    if extra_system:
        system += "\n" + extra_system

messages = [{"role": "system", "content": system}]
    if history.get("summary"):
        messages.append({"role": "system", "content": "خلاصه مکالمات قبلی:\n" + history["summary"]})
    messages.extend(history.get("messages", [])[-8:])
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
    prompt = "مکالمه زیر را در حداکثر 120 کلمه خلاصه کن. فقط علایق و اطلاعات لازم برای ادامه گفتگو را نگه دار.\n\n" + text_block
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
# 9. VALIDATION & INTENT (اعتبارسنجی و تشخیص هدف)
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
    if re.search(r"(یادم بنداز|یادآور|یادآوری کن)", t): return "reminder"
    if re.search(r"(هواشناسی|آب.?وهوا|دمای هوا|پیش.?بینی هوا)", t): return "weather"
    if re.search(r"(قیمت دلار|نرخ دلار|قیمت ارز)", t): return "currency"
    if re.search(r"(ترجمه کن|معنی کن|translate)", t): return "translate"
    if re.search(r"(جستجو کن|بگرد|اخبار|خبر|قیمت|امروز|آخرین وضعیت)", t): return "search"
    if re.search(r"https?://\S+", t): return "link"
    return "chat"

# =========================================================
# 10. WEB SEARCH (جستجوی وب)
# =========================================================
class SearchParser(HTMLParser):
    def init(self):
        super().init()
        self.results = []
        self.current_anchor = None
        self.current_text = ""
        self.current_snippet = False
        self.in_bing = False
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "")
        if tag == "li" and "b_algo" in classes: self.in_bing = True
        if tag == "a":
            if "result__a" in classes or self.in_bing:
                self.current_anchor = {"url": attrs.get("href", ""), "title": ""}

self.current_text = ""
        if tag == "p":
            if self.in_bing or "result__snippet" in classes:
                self.current_snippet = True
                self.current_text = ""
    def handle_data(self, data):
        if self.current_anchor is not None or self.current_snippet: self.current_text += data
    def handle_endtag(self, tag):
        if tag == "a" and self.current_anchor is not None:
            self.current_anchor["title"] = self.current_text.strip()
            self.results.append(self.current_anchor)
            self.current_anchor = None
            self.current_text = ""
        if tag == "p" and self.current_snippet:
            if self.results: self.results[-1]["snippet"] = self.current_text.strip()
            self.current_snippet = False
            self.current_text = ""
        if tag == "li": self.in_bing = False

def clean_snippet(text):
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:400]

def search_web(query):
    key = hashlib.sha256(query.lower().encode("utf-8")).hexdigest()
    cached = SEARCH_CACHE.get(key)
    if cached and time.time() - cached[0] < SEARCH_CACHE_TTL: return cached[1]
    engines = [("https://html.duckduckgo.com/html/", "ddg"), ("https://www.bing.com/search", "bing")]
    for base, engine in engines:
        try:
            response = http_request("GET", base, params={"q": query}, headers={"User-Agent": "Mozilla/5.0"}, timeout=9, attempts=1)
            parser = SearchParser()
            parser.feed(response.text)
            results = []
            for item in parser.results:
                title = clean_snippet(item.get("title"))
                snippet = clean_snippet(item.get("snippet"))
                url = item.get("url", "")
                if engine == "ddg" and "uddg=" in url:
                    parsed = urlparse(url)
                    url = unquote(dict(x.split("=", 1) for x in parsed.query.split("&") if "=" in x).get("uddg", url))
                if not title or not url.startswith("http"): continue
                if any(r["url"] == url for r in results): continue
                results.append({"title": title, "snippet": snippet, "url": url})
                if len(results) >= 5: break
            if results:
                SEARCH_CACHE[key] = (time.time(), results)
                return results
        except Exception:
            pass
    return None

# =========================================================
# 11. WEATHER & CURRENCY (هواشناسی و ارز)
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
    if not url: return "API قیمت دلار تنظیم نشده است.\nمتغیر DOLLAR_API_URL را تنظیم کن."
    try:
        data = http_json("GET", url, timeout=8)
        return "💵 اطلاعات دریافتی قیمت دلار:\n" + json.dumps(data, ensure_ascii=False)[:1500]
    except Exception:
        return "دریافت قیمت دلار در حال حاضر ناموفق بود."

# =========================================================
# 12. SAFE LINK FETCHING (دریافت امن لینک)
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

class TextExtractor(HTMLParser):
    def init(self):
        super().init()
        self.parts = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg", "iframe", "nav", "footer"): self.skip += 1
    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "iframe", "nav", "footer"): self.skip = max(0, self.skip - 1)
    def handle_data(self, data):
        if not self.skip:
            text = re.sub(r"\s+", " ", data).strip()
            if text: self.parts.append(text)

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
        parser = TextExtractor()
        parser.feed(b"".join(chunks).decode(response.encoding or "utf-8", errors="replace"))
        return " ".join(parser.parts)[:10000]
    except Exception:
        return None

# =========================================================
# 13. REMINDERS (یادآور)
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
# 14. USER STATS & POLLS (آمار و نظرسنجی)
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

def create_poll(chat_id, user_id, text):
    parts = [x.strip() for x in text.split("|")]
    if len(parts) < 3: return "فرمت صحیح:\n/poll سؤال | گزینه اول | گزینه دوم"
    question, options = parts[0], parts[1:7]
    poll_id = secrets.token_hex(3)
    with storage_lock:
        POLLS[poll_id] = {"question": question, "options": options, "votes": {}, "creator": str(user_id), "chat_id": chat_id, "created": time.time()}
        save_json(POLLS_FILE, POLLS)
    buttons = [[{"text": option[:50], "callback_data": f"poll:{poll_id}:{i}"}] for i, option in enumerate(options)]
    send_keyboard(chat_id, "📊 " + question, buttons)
    return None

def vote_poll(chat_id, user_id, poll_id, index):
    with storage_lock:
        poll = POLLS.get(poll_id)
        if not poll: return "این نظرسنجی دیگر موجود نیست."
        if index < 0 or index >= len(poll["options"]): return "گزینه نامعتبر است."
        poll["votes"][str(user_id)] = index
        save_json(POLLS_FILE, POLLS)
        counts = [sum(1 for v in poll["votes"].values() if v == i) for i in range(len(poll["options"]))]
    return "نتیجه نظرسنجی:\n\n" + "\n".join(f"{poll['options'][i]}: {counts[i]} رأی" for i in range(len(counts)))

# =========================================================
# 15. COMMAND HANDLER (مدیریت دستورات)
# =========================================================
def handle_command(chat_id, user_id, text):
    parts = text.split(maxsplit=1)
    command = parts[0].split("@")[0].lower()
    argument = parts[1] if len(parts) > 1 else ""

    if command in ("/start", "/menu"):
        send_keyboard(chat_id, "سلام! به Super Bot خوش آمدی.\nیک قابلیت را انتخاب کن یا مستقیماً پیام بده.", main_menu())
        return True
    if command in ("/help", "/راهنما"):
        send_message(chat_id, "🤖 راهنمای Super Bot\n\n/start - شروع\n/help - راهنما\n/settings - تنظیمات\n/stats - آمار\n/clear - پاک کردن حافظه\n/weather تهران - هواشناسی\n/tr متن - ترجمه\n/poll سؤال | گزینه۱ | گزینه۲ - نظرسنجی")
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
            result = call_ai(user_id, "متن زیر را ترجمه کن. زبان مقصد را با توجه به زبان مبدأ انتخاب کن. فقط ترجمه را بنویس:\n" + argument)
            send_message(chat_id, result or "سرویس ترجمه موقتاً در دسترس نیست.")
        return True
    if command in ("/poll", "/quiz"):
        result = create_poll(chat_id, user_id, argument)
        if result: send_message(chat_id, result)
        return True
    return False

# =========================================================
# 16. CALLBACK HANDLER (دکمه‌های شیشه‌ای)
# =========================================================
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
    elif data == "menu:creator": send_message(chat_id, "ساخته‌شده با Python و Super Bot Engine.")
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
    elif data.startswith("poll:"):
        try:
            _, poll_id, index = data.split(":")
            result = vote_poll(chat_id, user_id, poll_id, int(index))
            send_message(chat_id, result)
        except Exception: send_message(chat_id, "ثبت رأی انجام نشد.")

# =========================================================
# 17. MESSAGE PROCESSING (پردازش پیام‌ها)
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
        result_text = "\n\n".join(f"{i+1}. {x['title']}\n{x['snippet']}\n{x['url']}" for i, x in enumerate(results))
        prompt = "بر اساس نتایج زیر به سؤال کاربر پاسخ بده. منابع را ذکر کن.\n\n" + result_text + "\n\nسؤال کاربر:\n" + text
        answer = call_ai(user_id, prompt)
        send_message(chat_id, answer or ("نتایج جستجو دریافت شدند، اما سرویس هوش مصنوعی پاسخ نداد:\n\n" + result_text[:2500]))
        return

    # چت عادی
    update_history(user_id, "user", text)
    answer = call_ai(user_id, text)
    if not answer:
        send_message(chat_id, "فعلاً همه سرویس‌های هوش مصنوعی مشغول هستند. کمی بعد امتحان کن.")
        return
    update_history(user_id, "assistant", answer)
    send_message(chat_id, answer)

    item = get_history(user_id)
    if item.get("turns", 0) >= 20 and item.get("turns", 0) % 20 == 0:
        try: maybe_summarize(user_id)
        except Exception: pass

# =========================================================
# 18. JOB QUEUE & WORKERS (صف پردازش)
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
                traceback.print_exc() # چاپ خطا در لاگ‌ها
                message = update.get("message", {})
                chat_id = message.get("chat", {}).get("id")
                if chat_id:
                    try: send_message(chat_id, "یک خطای موقت رخ داد؛ دوباره امتحان کن.")
                    except Exception: pass
            finally:
                JOB_QUEUE.task_done()
        except Empty: continue
        except Exception: logger.exception("Worker failure")

# =========================================================
# 19. UPDATE POLLING (دریافت پیام‌ها)
# =========================================================
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
                except Full:
                    break
                if update_id is not None:
                    remember_update(update_id)
                    offset = update_id + 1
        except Exception:
            time.sleep(3)

# =========================================================
# 20. FLASK HEALTHCHECK (بررسی سلامت)
# =========================================================
app = Flask(name)

@app.get("/")
def home(): return jsonify({"service": "Super Bot", "status": "running"})

@app.get("/health")
def health():
    return jsonify({"status": "ok", "uptime": int(time.time() - START_TIME), "queue": JOB_QUEUE.qsize()})

# =========================================================
# 21. STARTUP (شروع به کار)
# =========================================================
def main():
    print("Super Bot starting...", flush=True)
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=polling_worker, daemon=True).start()
    threading.Thread(target=reminder_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)

if name == "main":
    main()

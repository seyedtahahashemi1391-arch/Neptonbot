# -*- coding: utf-8 -*-
import requests, time, json, os, random, threading, re
from flask import Flask

# ==========================================================
# ⚠️ توکن ربات سروش پلاس رو دقیقاً بین دو تا "" بذار
BOT_TOKEN = "70955089:Eg66Kyf45u1tchK7O5wMzXiwy2-TIe3gTYc" 
# ==========================================================

BASE_URL = "https://api.splus.ir/bot" + BOT_TOKEN if BOT_TOKEN else ""
HISTORY_FILE = "bot_history.json"

app = Flask(__name__)
@app.route('/')
def home(): return "Bot is alive!"

def run_web(): 
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

HISTORY = {}
WAITING_FOR_CITY = {}
LAST_REQUEST = {} 
SEEN_UPDATES = set() 

SYSTEM_PROMPT = (
    "تو یک دستیار هوش مصنوعی فارسی‌زبان هستی. "
    "قوانین: ۱. فقط و فقط به زبان فارسی روان و محاوره‌ای جواب بده. "
    "۲. هرگز از کلمات یا حروف زبان‌های دیگر استفاده نکن. "
    "۳. اگر سوال نامفهوم بود، به فارسی بپرس «منظورت چیست؟». "
    "۴. پاسخ‌ها کوتاه و بدون مارک‌داون باشند."
)

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return {}
    return {}

def save_history(h):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f: json.dump(h, f, ensure_ascii=False)
    except: pass

HISTORY = load_history()

def send_message(chat_id, text, reply_markup=None):
    if len(text) > 4000: text = text[:4000] + "\n..."
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup: payload["reply_markup"] = reply_markup
    try: 
        requests.post(BASE_URL + "/sendMessage", json=payload, timeout=15)
    except Exception as e:
        print(f"[SEND ERROR] {e}", flush=True)

def answer_callback(callback_id, text=""):
    try: requests.post(BASE_URL + "/answerCallbackQuery", json={"callback_query_id": callback_id, "text": text}, timeout=10)
    except: pass

# 🔍 دستور دیباگ برای دریافت پیام‌ها
def get_updates(offset):
    try:
        r = requests.get(BASE_URL + "/getUpdates", params={"timeout": 30, "offset": offset}, timeout=40)
        data = r.json()
        if data.get("result"):
            print("[NEW UPDATE RECEIVED]", flush=True)
        return data
    except Exception as e:
        print(f"[POLL ERROR] {e}", flush=True)
        return {"ok": False}

def is_valid_persian(text):
    if not text: return False
    invalid_chars = re.findall(r'[\u0400-\u04FF\u0530-\u058F\u0900-\u097F\u0D00-\u0D7F\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]', text)
    if len(invalid_chars) > 2: return False
    return True

def get_weather(city_name):
    city_name = city_name.strip()
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        geo_res = requests.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city_name, "count": 1, "language": "fa", "format": "json"}, headers=headers, timeout=10).json()
        if not geo_res.get('results'): return f"❌ شهر «{city_name}» پیدا نشد."
        loc = geo_res['results'][0]
        w_res = requests.get("https://api.open-meteo.com/v1/forecast", params={"latitude": loc['latitude'], "longitude": loc['longitude'], "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m", "daily": "temperature_2m_max,temperature_2m_min", "timezone": "auto"}, headers=headers, timeout=10).json()
        cur, day = w_res["current"], w_res["daily"]
        cond = {0: "صاف ☀️", 1: "تقریباً صاف 🌤️", 2: "نیمه ابری ⛅", 3: "ابری ☁️", 45: "مه 🌫️", 61: "باران 🌧️", 71: "برف ❄️", 95: "رعد و برق ⛈️"}.get(cur["weather_code"], "نامشخص")
        return f"🌤️ هوای {loc['name']}:\nوضعیت: {cond}\n🌡️ دما: {cur['temperature_2m']}°C\n📈 بیشینه: {day['temperature_2m_max'][0]}°C | 📉 کمینه: {day['temperature_2m_min'][0]}°C\n💧 رطوبت: {cur['relative_humidity_2m']}% | 💨 باد: {cur['wind_speed_10m']} km/h"
    except: return "❌ خطا در ارتباط با سرور هواشناسی."

def get_dollar_price():
    try:
        r = requests.get("https://nerkh.jahankhahan.shop/data/live.json", headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        if r.status_code == 200:
            rate = r.json().get("rates", {}).get("dollar")
            if rate: return f"💵 قیمت لحظه‌ای دلار آزاد:\n{int(rate):,} تومان"
    except: pass
    return "❌ سرور قیمت دلار پاسخ نمی‌دهد."

def ask_ai(user_id, text):
    history = HISTORY.get(str(user_id), [])
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [{"role": "user", "content": text}]
    providers = [
        {"url": "https://api.kilo.ai/api/gateway/chat/completions", "json": {"model": "minimax/minimax-m2.1:free", "messages": messages, "max_tokens": 200}, "headers": {}},
        {"url": "https://text.pollinations.ai/openai", "json": {"model": "openai", "messages": messages, "max_tokens": 200}, "headers": {}},
        {"url": "https://api.llm7.io/v1/chat/completions", "json": {"model": "default", "messages": messages, "max_tokens": 200}, "headers": {"Authorization": "Bearer unused"}},
    ]
    for p in providers:
        try:
            r = requests.post(p["url"], json=p["json"], headers=p["headers"], timeout=15)
            if r.status_code == 200:
                reply = r.json()["choices"][0]["message"]["content"]
                if is_valid_persian(reply):
                    history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
                    HISTORY[str(user_id)] = history[-4:]
                    save_history(HISTORY)
                    return reply
        except: pass
    return "⚠️ متأسفانه الان نمی‌تونم جواب بدم. یه بار دیگه امتحان کن."

MAIN_MENU = {"inline_keyboard": [[{"text": "🌤️ هواشناسی", "callback_data": "weather"}, {"text": "🎲 تاس", "callback_data": "dice"}], [{"text": "🧹 حافظه", "callback_data": "clear"}, {"text": "👨‍💻 سازنده", "callback_data": "creator"}]]}

def handle_callback(cb):
    data, chat_id, user_id = cb.get("data"), cb.get("message", {}).get("chat", {}).get("id"), cb.get("from", {}).get("id")
    if not chat_id: return
    if data == "dice": send_message(chat_id, f"🎲 تاس: {random.randint(1, 6)}")
    elif data == "weather": WAITING_FOR_CITY[str(user_id)] = True; send_message(chat_id, "🌤️ اسم شهرت رو بنویس:")
    elif data == "clear": HISTORY[str(user_id)] = []; save_history(HISTORY); send_message(chat_id, "🧹 پاک شد.")
    elif data == "creator": send_message(chat_id, "👨‍💻 سازنده: Nepton\n@t91m62")
    answer_callback(cb.get("id"), "")

def handle_message(update):
    msg = update.get("message")
    if not msg: return
    chat_id, user_id, text = msg.get("chat", {}).get("id"), msg.get("from", {}).get("id"), (msg.get("text") or "").strip()
    if not chat_id or not user_id: return
    
    print(f"[MSG] Received: {text} from {user_id}", flush=True)
    
    now = time.time()
    if str(user_id) in LAST_REQUEST and now - LAST_REQUEST[str(user_id)] < 2: return 
    LAST_REQUEST[str(user_id)] = now
    if text == "/start": WAITING_FOR_CITY[str(user_id)] = False; send_message(chat_id, "سلام👋 چطور کمکت کنم؟", reply_markup=MAIN_MENU); return
    if text == "/clear": WAITING_FOR_CITY[str(user_id)] = False; HISTORY[str(user_id)] = []; save_history(HISTORY); send_message(chat_id, "🧹 پاک شد."); return
    if "سازندت" in text: WAITING_FOR_CITY[str(user_id)] = False; send_message(chat_id, "سازنده من Nepton است. 👨‍💻"); return
    if "دلار" in text: WAITING_FOR_CITY[str(user_id)] = False; threading.Thread(target=lambda: send_message(chat_id, get_dollar_price())).start(); return
    if WAITING_FOR_CITY.get(str(user_id)): WAITING_FOR_CITY[str(user_id)] = False; send_message(chat_id, "⏳ در حال دریافت..."); threading.Thread(target=lambda: send_message(chat_id, get_weather(text))).start(); return
    threading.Thread(target=lambda: send_message(chat_id, ask_ai(user_id, text))).start()

def main():
    print("ربات سروش شروع شد", flush=True)
    offset = 0
    while True:
        try:
            updates = get_updates(offset)
            if updates.get("ok"):
                for update in updates.get("result", []):
                    update_id = update.get("update_id")
                    if update_id in SEEN_UPDATES: continue
                    SEEN_UPDATES.add(update_id)
                    if len(SEEN_UPDATES) > 1000: SEEN_UPDATES.clear()
                    offset = update_id + 1
                    if "callback_query" in update: threading.Thread(target=handle_callback, args=(update["callback_query"],)).start()
                    else: threading.Thread(target=handle_message, args=(update,)).start()
            time.sleep(1)
        except KeyboardInterrupt: break
        except Exception as e: 
            print("MAIN ERROR:", e, flush=True)
            time.sleep(3)

if __name__ == "__main__":
    t = threading.Thread(target=run_web)
    t.daemon = True
    t.start()
    main()

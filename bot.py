# -*- coding: utf-8 -*-
import requests, time, json, os, random, threading, re
from flask import Flask

# ==========================================================
# ⚠️ توکن ربات سروش پلاس رو دقیقاً بین دو تا "" بذار
BOT_TOKEN = "70955089:Eg66Kyf45u1tchK7O5wMzXiwy2-TIe3gTYc" 
# ==========================================================

BASE_URL = "https://api.splus.ir/bot" + BOT_TOKEN if BOT_TOKEN else ""
HISTORY_FILE = "bot_history.json"
LOCK = threading.Lock() 

# 🌍 دیکشنری شهرهای مهم ایران (برای اینکه سایت هواشناسی گیج نشه)
IRAN_CITIES = {
    "تهران": (35.6892, 51.3890), "مشهد": (36.2972, 59.6067), "اصفهان": (32.6546, 51.6680),
    "شیراز": (29.5918, 52.5837), "تبریز": (38.0962, 46.2738), "کرج": (35.8400, 50.9391),
    "اهواز": (31.3183, 48.6706), "قم": (34.6416, 50.8746), "کرمانشاه": (34.3142, 47.0650),
    "رشت": (37.2808, 49.5832), "زاهدان": (29.4963, 60.8629), "همدان": (34.7983, 48.5147),
    "کرمان": (30.2839, 57.0834), "یزد": (31.8974, 54.3569), "اردبیل": (38.2498, 48.2933),
    "بندرعباس": (27.1865, 56.2808), "اراک": (34.0917, 49.6892), "قزوین": (36.2688, 50.0041), 
    "ساری": (36.5633, 53.0601), "بوشهر": (28.9234, 50.8203), "گرگان": (36.8456, 54.4393), 
    "خرم آباد": (33.4878, 48.3558), "سنندج": (35.3113, 46.9960), "بیرجند": (32.8663, 59.2211), 
    "زنجان": (36.6736, 48.4787), "شهرکرد": (32.3256, 50.8644), "یاسوج": (30.6682, 51.5880), 
    "ایلام": (33.6374, 46.4227), "سمنان": (35.5729, 53.3971), "بجنورد": (37.4747, 57.3290), 
    "ارومیه": (37.5527, 45.0760), "کیش": (26.5578, 53.9805), "قشم": (26.9511, 56.2717), 
    "بندرانزلی": (37.4724, 49.4622), "مراغه": (37.3893, 46.2381), "خوی": (38.5500, 44.9500), 
    "دزفول": (32.3800, 48.4000), "آبادان": (30.3392, 48.3043), "بم": (29.1061, 58.3569), 
    "کاشمر": (35.2383, 58.4656), "سبزوار": (36.2126, 57.6819), "نیشابور": (36.2133, 58.7958)
}

app = Flask(__name__)
@app.route('/')
def home(): return "Nepton Bot is alive and smarter!"

def run_web(): 
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

HISTORY = {}
WAITING_FOR_CITY = {}
LAST_REQUEST = {} 
SEEN_UPDATES = set() 

# 🧠 پرامپت جدید و صمیمی‌تر (به جای اینکه خشک بگه منظورت چیه)
SYSTEM_PROMPT = (
    "تو یک دستیار هوش مصنوعی فارسی‌زبان، باهوش، صمیمی و خودمونی هستی. "
    "قوانین: ۱. فقط و به زبان فارسی روان و محاوره‌ای جواب بده. "
    "۲. اگر کلمه‌ای یا سوالی را متوجه نشدی، مثل یک دوست بپرس «دقیقاً منظورت چیه؟» یا «یه کم واضح‌تر می‌گی؟». "
    "۳. پاسخ‌ها را کوتاه و در حد ۲ تا ۴ جمله نگه دار. "
    "۴. از مارک‌داون (ستاره، بک‌تیک) استفاده نکن. "
    "۵. اگر کاربر اسم یک شهر را پرسید، خیلی طبیعی جواب بده."
)

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return {}
    return {}

def save_history(h):
    try:
        with LOCK: 
            with open(HISTORY_FILE, "w", encoding="utf-8") as f: 
                json.dump(h, f, ensure_ascii=False)
    except Exception as e: print(f"[SAVE ERROR] {e}", flush=True)

HISTORY = load_history()

def send_message(chat_id, text, reply_markup=None):
    if len(text) > 4000: text = text[:4000] + "\n..."
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup: payload["reply_markup"] = reply_markup
    try: 
        requests.post(BASE_URL + "/sendMessage", json=payload, timeout=15)
    except Exception as e: print(f"[SEND ERROR] {e}", flush=True)

def answer_callback(callback_id, text=""):
    try: requests.post(BASE_URL + "/answerCallbackQuery", json={"callback_query_id": callback_id, "text": text}, timeout=10)
    except: pass

def get_updates(offset):
    try:
        r = requests.get(BASE_URL + "/getUpdates", params={"timeout": 30, "offset": offset}, timeout=40)
        data = r.json()
        if data.get("result"): print("[NEW UPDATE]", flush=True)
        return data
    except Exception as e:
        print(f"[POLL ERROR] {e}", flush=True)
        return {"ok": False}

def is_valid_persian(text):
    if not text: return False
    invalid_chars = re.findall(r'[\u0400-\u04FF\u0530-\u058F\u0900-\u097F\u0D00-\u0D7F\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]', text)
    return len(invalid_chars) <= 2

# 🌤️ تابع هواشناسی ضدضربه (اصلاح حروف عربی و حذف فاصله‌ها)
def get_weather(city_name):
    # ۱. تمیزکاری اسم شهر (تبدیل ي به ی، ك به ک و حذف فاصله‌ها)
    cleaned_input = city_name.strip().replace("ي", "ی").replace("ك", "ک").replace(" ", "")
    
    lat, lon, official_name = None, None, city_name
    
    # ۲. چک کردن دیکشنری داخلی با اسم تمیز شده
    for name, coords in IRAN_CITIES.items():
        if cleaned_input == name.replace(" ", ""):
            lat, lon = coords
            official_name = name
            break
            
    # ۳. اگر توی دیکشنری نبود، برو سراغ سایت (با اسم اصلی)
    if not lat:
        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            geo_res = requests.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city_name, "count": 1, "language": "fa", "format": "json"}, headers=headers, timeout=10).json()
            if geo_res.get('results'):
                loc = geo_res['results'][0]
                lat, lon = loc['latitude'], loc['longitude']
                official_name = loc['name']
            else:
                return f"❌ شهر «{city_name}» رو پیدا نکردم. اسمش رو دقیق‌تر بنویس."
        except:
            return "❌ خطا در ارتباط با سرور هواشناسی."
            
    # ۴. دریافت هوا با مختصات پیدا شده
    try:
        w_res = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m&daily=temperature_2m_max,temperature_2m_min&timezone=auto", headers={'User-Agent': 'Mozilla/5.0'}, timeout=10).json()
        cur, day = w_res["current"], w_res["daily"]
        cond = {0: "صاف ☀️", 1: "تقریباً صاف 🌤️", 2: "نیمه ابری ⛅", 3: "ابری ☁️", 45: "مه 🌫️", 61: "باران 🌧️", 71: "برف ❄️", 95: "رعد و برق ⛈️"}.get(cur["weather_code"], "نامشخص")
        return f"🌤️ هوای {official_name}:\nوضعیت: {cond}\n🌡️ دما: {cur['temperature_2m']}°C\n📈 بیشینه: {day['temperature_2m_max'][0]}°C | 📉 کمینه: {day['temperature_2m_min'][0]}°C\n💧 رطوبت: {cur['relative_humidity_2m']}% | 💨 باد: {cur['wind_speed_10m']} km/h"
    except:
        return "❌ خطا در دریافت اطلاعات هواشناسی."

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
        {"url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions", "json": {"model": "Qwen3Guard-Gen-0.6B", "messages": messages, "max_tokens": 200}, "headers": {}}
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
        except Exception as e: print(f"[AI ERROR] {e}", flush=True)
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
    
    if text == "/start": 
        WAITING_FOR_CITY[str(user_id)] = False
        send_message(chat_id, "سلام👋 چطور کمکت کنم؟\n\nدستورات:\n/start شروع مجدد\n/help راهنما\n/clear پاک کردن حافظه", reply_markup=MAIN_MENU)
        return
    if text == "/clear": 
        WAITING_FOR_CITY[str(user_id)] = False
        HISTORY[str(user_id)] = []
        save_history(HISTORY)
        send_message(chat_id, "🧹 حافظه پاک شد.")
        return
    if text == "/help":
        send_message(chat_id, "📖 راهنما:\n• هر سوالی داری بپرس\n• برای هواشناسی از دکمه‌ها استفاده کن\n• برای قیمت دلار بنویس «دلار»\n• /clear برای پاک کردن حافظه")
        return
    if "سازندت" in text or "کی ساختت" in text: 
        WAITING_FOR_CITY[str(user_id)] = False
        send_message(chat_id, "سازنده من Nepton است. 👨‍💻")
        return
    if "دلار" in text: 
        WAITING_FOR_CITY[str(user_id)] = False
        threading.Thread(target=lambda: send_message(chat_id, get_dollar_price())).start()
        return
    if WAITING_FOR_CITY.get(str(user_id)): 
        WAITING_FOR_CITY[str(user_id)] = False
        send_message(chat_id, "⏳ در حال دریافت...")
        threading.Thread(target=lambda: send_message(chat_id, get_weather(text))).start()
        return
    
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

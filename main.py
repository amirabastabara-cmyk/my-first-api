import os
import requests
import json
from flask import Flask, request, jsonify
from datetime import datetime
import uuid

app = Flask(__name__)

# ========== تنظیمات ==========
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    print("⚠️ هشدار: کلید DeepSeek تنظیم نشده!")

# ========== حافظه‌ی مکالمات (ذخیره‌ی موقت) ==========
memory = {}  # ساختار: {user_id: {"history": [...], "last_seen": timestamp}}

# ========== کش پاسخ‌ها (برای سوالات تکراری) ==========
cache = {}  # ساختار: {question: answer}

# ========== تابع جستجو در وب ==========
def search_web(query):
    """جستجوی عبارت در وب و برگرداندن خلاصه‌ای از نتایج"""
    try:
        # DuckDuckGo (نیاز به کتابخانه‌ی duckduckgo-search دارد)
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=3))
                if results:
                    summary = "\n".join([f"{r['title']}: {r['body']}" for r in results])
                    return summary
        except ImportError:
            # اگر کتابخانه نصب نیست، از روش جایگزین استفاده کن
            pass
        except Exception as e:
            print(f"⚠️ خطا در جستجو: {e}")
        
        # روش جایگزین: استفاده از Brave Search API
        # (برای این روش باید کلید Brave بگیرید)
        return None
    except Exception as e:
        print(f"⚠️ خطا در جستجو: {e}")
        return None

# ========== تابع تماس با DeepSeek (با حافظه) ==========
def ask_deepseek(prompt, history=None):
    """ارسال پیام به DeepSeek با تاریخچه"""
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # ساخت لیست پیام‌ها با تاریخچه
    messages = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})
    
    data = {
        "model": "deepseek-chat",
        "messages": messages,
        "max_tokens": 800,
        "temperature": 0.7
    }
    
    try:
        response = requests.post(url, json=data, headers=headers)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"❌ خطا در ارتباط با DeepSeek: {str(e)}"

# ========== مسیر اصلی ==========
@app.route('/')
def home():
    return jsonify({
        "message": "ربات هوشمند با حافظه و جستجو 🤖",
        "status": "online",
        "features": ["حافظه‌ی مکالمات", "جستجو در وب", "کش پاسخ‌ها"],
        "docs": {
            "/ask?prompt=سلام&user_id=123": "سوال از ربات",
            "/search?q=خبر امروز": "جستجوی وب",
            "/memory": "مشاهده‌ی حافظه",
            "/clear?user_id=123": "پاک کردن حافظه"
        }
    })

# ========== مسیر پرسش (با حافظه و جستجو) ==========
@app.route('/ask')
def ask():
    prompt = request.args.get("prompt")
    user_id = request.args.get("user_id", "default_user")
    
    if not prompt:
        return jsonify({"error": "لطفاً پارامتر prompt رو وارد کن"}), 400
    
    # ---- ۱. چک کردن کش (سوالات تکراری) ----
    if prompt in cache:
        return jsonify({
            "user": prompt,
            "response": cache[prompt],
            "source": "cache",
            "user_id": user_id
        })
    
    # ---- ۲. دریافت یا ایجاد تاریخچه ----
    if user_id not in memory:
        memory[user_id] = {"history": [], "last_seen": datetime.now().isoformat()}
    
    history = memory[user_id]["history"]
    
    # ---- ۳. تصمیم‌گیری: جستجو کنم یا نه؟ ----
    search_result = None
    response = None
    
    # اگر سوال درباره‌ی چیز جدید یا زمان حال بود، جستجو کن
    need_search = any(keyword in prompt.lower() for keyword in 
                     ["خبر", "امروز", "دیروز", "الان", "تازه", "جستجو", "پیدا کن"])
    
    if need_search:
        search_result = search_web(prompt)
    
    # ---- ۴. ساخت پرامپت نهایی ----
    final_prompt = prompt
    if search_result:
        final_prompt = f"""
سوال کاربر: {prompt}

نتایج جستجوی وب:
{search_result}

لطفاً بر اساس اطلاعات بالا، پاسخ کامل و مفیدی بده.
اگر اطلاعات کافی نیست، بگو که نتونستی پیدا کنی.
"""
    
    # ---- ۵. دریافت پاسخ از DeepSeek ----
    try:
        response = ask_deepseek(final_prompt, history)
    except Exception as e:
        return jsonify({"error": f"خطا در پردازش: {str(e)}"}), 500
    
    # ---- ۶. ذخیره در حافظه ----
    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": response})
    
    # محدود کردن تاریخچه (آخرین ۱۰ پیام)
    if len(history) > 10:
        memory[user_id]["history"] = history[-10:]
    
    # ذخیره در کش
    cache[prompt] = response
    
    return jsonify({
        "user": prompt,
        "response": response,
        "source": "search" if search_result else "deepseek",
        "user_id": user_id,
        "history_length": len(history)
    })

# ========== مسیر جستجوی مستقیم ==========
@app.route('/search')
def search():
    query = request.args.get("q")
    if not query:
        return jsonify({"error": "لطفاً پارامتر q رو وارد کن"}), 400
    
    result = search_web(query)
    if result:
        return jsonify({"query": query, "result": result})
    else:
        return jsonify({"error": "نتیجه‌ای پیدا نشد یا خطا در جستجو"}), 404

# ========== مسیر مشاهده‌ی حافظه ==========
@app.route('/memory')
def show_memory():
    """نمایش وضعیت حافظه و کش"""
    return jsonify({
        "memory": memory,
        "cache_size": len(cache),
        "users_count": len(memory)
    })

# ========== مسیر پاک کردن حافظه ==========
@app.route('/clear')
def clear_memory():
    user_id = request.args.get("user_id")
    if user_id and user_id in memory:
        memory[user_id] = {"history": [], "last_seen": datetime.now().isoformat()}
        return jsonify({"message": f"حافظه‌ی کاربر {user_id} پاک شد"})
    elif user_id:
        return jsonify({"error": "کاربر پیدا نشد"}), 404
    else:
        # پاک کردن همه
        memory.clear()
        cache.clear()
        return jsonify({"message": "همه‌ی حافظه‌ها پاک شد"})

# ========== مسیر پینگ ==========
@app.route('/ping')
def ping():
    return "pong"

# ========== راه‌اندازی ==========
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)

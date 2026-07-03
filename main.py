import os
import requests
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# ========== تنظیمات ==========
AVALAI_API_KEY = os.environ.get("AVALAI_API_KEY")
if not AVALAI_API_KEY:
    print("⚠️ هشدار: کلید AvalAI تنظیم نشده!")

# ========== حافظه‌ی مکالمات ==========
memory = {}  # {user_id: [{"role": "user", "content": "..."}, ...]}

# ========== تابع جستجو در وب (با DuckDuckGo) ==========
def search_web(query):
    """جستجوی عبارت در وب و برگرداندن خلاصه‌ای از نتایج"""
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=3))
            if results:
                summary = "\n".join([f"{r['title']}: {r['body']}" for r in results])
                return summary
            else:
                return None
    except ImportError:
        return "کتابخانه‌ی جستجو نصب نیست! لطفاً duckduckgo-search رو نصب کن."
    except Exception as e:
        return f"خطا در جستجو: {str(e)}"

# ========== تابع تماس با AvalAI (با حافظه) ==========
def ask_avalai(prompt, history=None):
    url = "https://api.avalai.ir/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {AVALAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    messages = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})
    
    data = {
        "model": "gpt-4o-mini",  # یا "deepseek-chat" یا "claude-3-haiku"
        "messages": messages,
        "max_tokens": 800,
        "temperature": 0.7
    }
    
    try:
        response = requests.post(url, json=data, headers=headers)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"❌ خطا در ارتباط با AvalAI: {str(e)}"

# ========== مسیر اصلی ==========
@app.route('/')
def home():
    return jsonify({
        "message": "ربات هوشمند با جستجو و حافظه 🤖",
        "status": "online",
        "features": ["حافظه‌ی مکالمات", "جستجو در وب", "چت هوشمند"],
        "docs": {
            "/ask?prompt=سلام&user_id=123": "سوال از ربات",
            "/search?q=خبر امروز": "جستجوی مستقیم",
            "/memory": "مشاهده‌ی حافظه",
            "/clear?user_id=123": "پاک کردن حافظه"
        }
    })

# ========== مسیر پرسش (با جستجو) ==========
@app.route('/ask')
def ask():
    prompt = request.args.get("prompt")
    user_id = request.args.get("user_id", "default_user")
    
    if not prompt:
        return jsonify({"error": "لطفاً پارامتر prompt رو وارد کن"}), 400
    
    # ---- ۱. دریافت یا ایجاد تاریخچه ----
    if user_id not in memory:
        memory[user_id] = []
    
    history = memory[user_id]
    
    # ---- ۲. تصمیم‌گیری: جستجو کنم یا نه؟ ----
    search_result = None
    need_search = any(keyword in prompt.lower() for keyword in 
                     ["خبر", "امروز", "دیروز", "الان", "تازه", "جستجو", "پیدا کن", "کی", "کجا", "چه زمانی"])
    
    if need_search:
        search_result = search_web(prompt)
    
    # ---- ۳. ساخت پرامپت نهایی ----
    final_prompt = prompt
    if search_result:
        final_prompt = f"""
سوال کاربر: {prompt}

نتایج جستجوی وب:
{search_result}

لطفاً بر اساس اطلاعات بالا، پاسخ کامل و مفیدی بده.
اگر اطلاعات کافی نیست، بگو که نتونستی پیدا کنی.
"""
    
    # ---- ۴. دریافت پاسخ از AvalAI ----
    try:
        response = ask_avalai(final_prompt, history)
    except Exception as e:
        return jsonify({"error": f"خطا در پردازش: {str(e)}"}), 500
    
    # ---- ۵. ذخیره در حافظه ----
    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": response})
    
    # محدود کردن تاریخچه (آخرین ۲۰ پیام)
    if len(history) > 20:
        memory[user_id] = history[-20:]
    
    return jsonify({
        "user": prompt,
        "response": response,
        "source": "search" if search_result else "avalai",
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
    return jsonify({
        "memory": memory,
        "users_count": len(memory)
    })

# ========== مسیر پاک کردن حافظه ==========
@app.route('/clear')
def clear_memory():
    user_id = request.args.get("user_id")
    if user_id and user_id in memory:
        memory[user_id] = []
        return jsonify({"message": f"حافظه‌ی کاربر {user_id} پاک شد"})
    elif user_id:
        return jsonify({"error": "کاربر پیدا نشد"}), 404
    else:
        memory.clear()
        return jsonify({"message": "همه‌ی حافظه‌ها پاک شد"})

# ========== مسیر پینگ ==========
@app.route('/ping')
def ping():
    return "pong"

# ========== راه‌اندازی ==========
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)

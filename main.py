import os
from flask import Flask, request, jsonify
from ddgs import DDGS  # کتابخانه جدید برای جستجو

app = Flask(__name__)

# ========== تابع جستجو در وب با DuckDuckGo ==========
def search_web(query):
    """جستجوی عبارت در وب و برگرداندن نتایج"""
    try:
        with DDGS() as ddgs:
            # جستجو و دریافت ۳ نتیجه اول
            results = list(ddgs.text(query, max_results=3))
            if results:
                # ساخت یک خلاصه از نتایج
                summary = "\n".join([f"{r['title']}: {r['body']}" for r in results])
                return summary
            else:
                return "نتیجه‌ای پیدا نشد."
    except Exception as e:
        return f"خطا در جستجو: {str(e)}"

# ========== مسیر اصلی برای پرسش ==========
@app.route('/ask')
def ask():
    prompt = request.args.get("prompt")
    if not prompt:
        return jsonify({"error": "لطفاً پارامتر prompt را وارد کن"}), 400

    # دریافت نتایج جستجو
    search_result = search_web(prompt)
    
    # ساخت پاسخ نهایی
    response_text = f"شما پرسیدید: {prompt}\n\n"
    response_text += f"نتیجه جستجو:\n{search_result}"

    return jsonify({
        "user": prompt,
        "response": response_text,
        "source": "duckduckgo"
    })

# ========== مسیرهای دیگر (پینگ، صفحه اصلی و...) ==========
@app.route('/')
def home():
    return jsonify({"message": "ربات جستجوگر با DuckDuckGo آماده است!", "status": "online"})

@app.route('/ping')
def ping():
    return "pong"

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)

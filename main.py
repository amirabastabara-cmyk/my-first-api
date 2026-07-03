import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")

def ask_deepseek(prompt):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "max_tokens": 500}
    try:
        response = requests.post(url, json=data, headers=headers)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"❌ خطا: {str(e)}"

@app.route('/')
def home():
    return jsonify({"message": "ربات DeepSeek آماده‌ست!", "status": "online"})

@app.route('/ask')
def ask():
    prompt = request.args.get("prompt")
    if not prompt:
        return jsonify({"error": "لطفاً ?prompt=سلام رو وارد کن"}), 400
    reply = ask_deepseek(prompt)
    return jsonify({"user": prompt, "response": reply})

@app.route('/ping')
def ping():
    return "pong"

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)

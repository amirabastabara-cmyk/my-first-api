from flask import Flask, jsonify

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "message": "سلام! ربات شما با موفقیت روی Render اجرا شد! 🎉",
        "status": "online"
    })

@app.route('/ping')
def ping():
    return "pong"

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)

import asyncio
import json
import os
from websockets.sync.server import serve
import websockets

# ========== مدیریت کاربران ==========
users = {}

# ========== هندلر اصلی ==========
def handler(websocket):
    username = None
    try:
        # دریافت پیام لاگین
        msg = websocket.recv()
        data = json.loads(msg)

        if data.get("type") == "login":
            username = data.get("username", "").strip()
            if not username or username in users:
                websocket.send(json.dumps({"type": "error", "message": "Invalid or duplicate username"}))
                return

            users[username] = websocket
            websocket.send(json.dumps({"type": "login_response", "status": "success"}))
            print(f"✅ {username} connected")
            broadcast(json.dumps({"type": "user_list", "users": list(users.keys())}))

            # حلقه دریافت پیام
            while True:
                try:
                    msg = websocket.recv()
                    data = json.loads(msg)
                    msg_type = data.get("type")

                    if msg_type == "chat_message":
                        broadcast(json.dumps({"type": "chat_message", "sender": username, "message": data["message"]}), username)
                    elif msg_type == "game_invite":
                        target = data.get("target")
                        if target in users:
                            users[target].send(json.dumps({"type": "game_invite", "sender": username, "game_name": data["game_name"]}))
                except:
                    break

    except:
        pass
    finally:
        if username in users:
            del users[username]
            broadcast(json.dumps({"type": "user_list", "users": list(users.keys())}))
            print(f"❌ {username} disconnected")

# ========== ارسال به همه ==========
def broadcast(message, exclude=None):
    for name, ws in list(users.items()):
        if name != exclude:
            try:
                ws.send(message)
            except:
                pass

# ========== سرور HTTP ساده برای Health Check ==========
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)

def run_health_server(port):
    server = HTTPServer(("", port), HealthCheckHandler)
    server.serve_forever()

# ========== اجرای همزمان ==========
import threading
import time

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    # اجرای سرور HTTP برای Health Check در یه ترد جداگانه
    threading.Thread(target=run_health_server, args=(port,), daemon=True).start()
    time.sleep(1)

    # اجرای سرور WebSocket
    print(f"🚀 Server running on port {port}")
    with serve(handler, "", port) as server:
        server.serve_forever()

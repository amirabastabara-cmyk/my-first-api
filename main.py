from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn
import os
import json
import hashlib
import sqlite3
import time

app = FastAPI()

# ===================== دیتابیس SQLite =====================
def init_db():
    """ایجاد دیتابیس و جدول کاربران"""
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at INTEGER
        )
    ''')
    conn.commit()
    conn.close()

def hash_password(password):
    """هش کردن پسورد با SHA-256"""
    return hashlib.sha256(password.encode()).hexdigest()

def register_user(username, password):
    """ثبت‌نام کاربر جدید"""
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        password_hash = hash_password(password)
        c.execute('INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)',
                  (username, password_hash, int(time.time())))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False  # یوزرنیم تکراری

def login_user(username, password):
    """ورود کاربر با بررسی پسورد"""
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT password_hash FROM users WHERE username = ?', (username,))
    result = c.fetchone()
    conn.close()
    if result:
        return result[0] == hash_password(password)
    return False

def user_exists(username):
    """بررسی وجود کاربر"""
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT 1 FROM users WHERE username = ?', (username,))
    result = c.fetchone()
    conn.close()
    return result is not None

# مقداردهی اولیه دیتابیس
init_db()

# ===================== مدیریت کاربران آنلاین =====================
connected_users = {}  # {username: websocket}

@app.get("/")
async def root():
    return {"message": "VoidVision Server is running!"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    username = None
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            msg_type = msg.get("type")
            
            if msg_type == "register":
                # ===== ثبت‌نام =====
                username = msg.get("username", "").strip()
                password = msg.get("password", "").strip()
                
                if not username or not password:
                    await websocket.send_text(json.dumps({
                        "type": "register_response",
                        "success": False,
                        "message": "Username and password are required!"
                    }))
                    continue
                
                if len(password) < 4:
                    await websocket.send_text(json.dumps({
                        "type": "register_response",
                        "success": False,
                        "message": "Password must be at least 4 characters!"
                    }))
                    continue
                
                if register_user(username, password):
                    await websocket.send_text(json.dumps({
                        "type": "register_response",
                        "success": True,
                        "message": "Registration successful! Please login."
                    }))
                else:
                    await websocket.send_text(json.dumps({
                        "type": "register_response",
                        "success": False,
                        "message": "Username already exists!"
                    }))
                    
            elif msg_type == "login":
                # ===== ورود =====
                username = msg.get("username", "").strip()
                password = msg.get("password", "").strip()
                
                if not username or not password:
                    await websocket.send_text(json.dumps({
                        "type": "login_response",
                        "success": False,
                        "message": "Username and password are required!"
                    }))
                    continue
                
                if not user_exists(username):
                    await websocket.send_text(json.dumps({
                        "type": "login_response",
                        "success": False,
                        "message": "Username not found! Please register first."
                    }))
                    continue
                
                if login_user(username, password):
                    # بررسی اینکه کاربر قبلاً آنلاین نباشه
                    if username in connected_users:
                        await websocket.send_text(json.dumps({
                            "type": "login_response",
                            "success": False,
                            "message": "User already logged in from another device!"
                        }))
                        continue
                    
                    connected_users[username] = websocket
                    await websocket.send_text(json.dumps({
                        "type": "login_response",
                        "success": True,
                        "message": "Login successful!"
                    }))
                    
                    print(f"✅ {username} connected (Total: {len(connected_users)})")
                    await broadcast_user_list()
                    
                    # حلقه دریافت پیام‌ها
                    while True:
                        try:
                            data = await websocket.receive_text()
                            msg = json.loads(data)
                            msg_type = msg.get("type")
                            
                            if msg_type == "chat_message":
                                await broadcast({
                                    "type": "chat_message",
                                    "sender": username,
                                    "message": msg.get("message", "")
                                }, username)
                                print(f"💬 [{username}]: {msg.get('message', '')}")
                                
                            elif msg_type == "game_invite":
                                target = msg.get("target")
                                if target in connected_users:
                                    await connected_users[target].send_text(json.dumps({
                                        "type": "game_invite",
                                        "sender": username,
                                        "game_name": msg.get("game_name", "Unknown Game"),
                                        "ip": msg.get("ip", ""),
                                        "port": msg.get("port", 0)
                                    }))
                                    print(f"🎮 {username} invited {target}")
                                    
                            elif msg_type == "get_users":
                                await connected_users[username].send_text(json.dumps({
                                    "type": "user_list",
                                    "users": list(connected_users.keys())
                                }))
                                
                        except WebSocketDisconnect:
                            break
                            
                else:
                    await websocket.send_text(json.dumps({
                        "type": "login_response",
                        "success": False,
                        "message": "Invalid password!"
                    }))
                    
            elif msg_type == "logout":
                if username:
                    await websocket.send_text(json.dumps({
                        "type": "logout_response",
                        "success": True,
                        "message": "Logged out successfully!"
                    }))
                break
                
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"⚠️ Error: {e}")
    finally:
        if username and username in connected_users:
            del connected_users[username]
            await broadcast_user_list()
            print(f"❌ {username} disconnected")

async def broadcast(data, exclude=None):
    for name, ws in list(connected_users.items()):
        if name != exclude:
            try:
                await ws.send_text(json.dumps(data))
            except:
                pass

async def broadcast_user_list():
    await broadcast({"type": "user_list", "users": list(connected_users.keys())})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)

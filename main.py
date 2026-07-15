from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn
import os
import json
import hashlib
import sqlite3
import time

app = FastAPI()

# ===================== دیتابیس =====================
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    # جدول کاربران
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at INTEGER
        )
    ''')
    # جدول دوستان
    c.execute('''
        CREATE TABLE IF NOT EXISTS friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            friend_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',  -- pending, accepted, rejected
            created_at INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (friend_id) REFERENCES users(id),
            UNIQUE(user_id, friend_id)
        )
    ''')
    conn.commit()
    conn.close()

def get_user_id(username):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT id FROM users WHERE username = ?', (username,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def register_user(username, password):
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
        return False

def login_user(username, password):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT password_hash FROM users WHERE username = ?', (username,))
    result = c.fetchone()
    conn.close()
    if result:
        return result[0] == hash_password(password)
    return False

def add_friend_request(user_id, friend_username):
    friend_id = get_user_id(friend_username)
    if not friend_id or user_id == friend_id:
        return False
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    try:
        c.execute('INSERT INTO friends (user_id, friend_id, status, created_at) VALUES (?, ?, ?, ?)',
                  (user_id, friend_id, 'pending', int(time.time())))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False

def accept_friend_request(user_id, friend_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('UPDATE friends SET status = "accepted" WHERE user_id = ? AND friend_id = ? AND status = "pending"',
              (friend_id, user_id))
    c.execute('INSERT OR IGNORE INTO friends (user_id, friend_id, status, created_at) VALUES (?, ?, ?, ?)',
              (user_id, friend_id, 'accepted', int(time.time())))
    conn.commit()
    conn.close()
    return True

def get_friends(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''
        SELECT u.username, f.status 
        FROM friends f
        JOIN users u ON u.id = f.friend_id
        WHERE f.user_id = ? AND f.status = 'accepted'
    ''', (user_id,))
    friends = c.fetchall()
    conn.close()
    return friends

def get_friend_requests(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''
        SELECT u.id, u.username
        FROM friends f
        JOIN users u ON u.id = f.user_id
        WHERE f.friend_id = ? AND f.status = 'pending'
    ''', (user_id,))
    requests = c.fetchall()
    conn.close()
    return requests

init_db()

# ===================== مدیریت کاربران آنلاین =====================
connected_users = {}  # {username: websocket}
user_sessions = {}    # {username: user_id}

@app.get("/")
async def root():
    return {"message": "VoidVision Server is running!"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    username = None
    user_id = None
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            msg_type = msg.get("type")
            
            if msg_type == "register":
                username = msg.get("username", "").strip()
                password = msg.get("password", "").strip()
                if not username or not password:
                    await websocket.send_text(json.dumps({"type": "register_response", "success": False, "message": "Username and password are required!"}))
                    continue
                if len(password) < 4:
                    await websocket.send_text(json.dumps({"type": "register_response", "success": False, "message": "Password must be at least 4 characters!"}))
                    continue
                if register_user(username, password):
                    await websocket.send_text(json.dumps({"type": "register_response", "success": True, "message": "Registration successful! Please login."}))
                else:
                    await websocket.send_text(json.dumps({"type": "register_response", "success": False, "message": "Username already exists!"}))
                    
            elif msg_type == "login":
                username = msg.get("username", "").strip()
                password = msg.get("password", "").strip()
                if not username or not password:
                    await websocket.send_text(json.dumps({"type": "login_response", "success": False, "message": "Username and password are required!"}))
                    continue
                if not login_user(username, password):
                    await websocket.send_text(json.dumps({"type": "login_response", "success": False, "message": "Invalid credentials!"}))
                    continue
                if username in connected_users:
                    await websocket.send_text(json.dumps({"type": "login_response", "success": False, "message": "User already logged in!"}))
                    continue
                
                user_id = get_user_id(username)
                connected_users[username] = websocket
                user_sessions[username] = user_id
                await websocket.send_text(json.dumps({"type": "login_response", "success": True, "message": "Login successful!"}))
                await broadcast_user_list()
                await send_friend_list(username)
                await send_friend_requests(username)
                
                # حلقه اصلی
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
                                
                        elif msg_type == "get_users":
                            await connected_users[username].send_text(json.dumps({"type": "user_list", "users": list(connected_users.keys())}))
                            
                        # ===== بخش دوست‌یابی =====
                        elif msg_type == "add_friend":
                            friend_username = msg.get("friend_username", "").strip()
                            if not friend_username:
                                await websocket.send_text(json.dumps({"type": "add_friend_response", "success": False, "message": "Invalid username!"}))
                                continue
                            if friend_username == username:
                                await websocket.send_text(json.dumps({"type": "add_friend_response", "success": False, "message": "You cannot add yourself!"}))
                                continue
                            if not get_user_id(friend_username):
                                await websocket.send_text(json.dumps({"type": "add_friend_response", "success": False, "message": "User not found!"}))
                                continue
                            if add_friend_request(user_id, friend_username):
                                await websocket.send_text(json.dumps({"type": "add_friend_response", "success": True, "message": f"Friend request sent to {friend_username}!"}))
                                # اطلاع به طرف مقابل
                                if friend_username in connected_users:
                                    await connected_users[friend_username].send_text(json.dumps({
                                        "type": "friend_request_received",
                                        "from": username
                                    }))
                            else:
                                await websocket.send_text(json.dumps({"type": "add_friend_response", "success": False, "message": "Request already sent or error!"}))
                                
                        elif msg_type == "accept_friend":
                            friend_id = msg.get("friend_id")
                            if not friend_id:
                                continue
                            accept_friend_request(user_id, friend_id)
                            await send_friend_list(username)
                            await send_friend_requests(username)
                            # اطلاع به طرف مقابل
                            friend_username = get_username_by_id(friend_id)
                            if friend_username and friend_username in connected_users:
                                await connected_users[friend_username].send_text(json.dumps({
                                    "type": "friend_request_accepted",
                                    "from": username
                                }))
                                
                        elif msg_type == "get_friends":
                            await send_friend_list(username)
                            
                        elif msg_type == "get_friend_requests":
                            await send_friend_requests(username)
                            
                    except WebSocketDisconnect:
                        break
                        
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"⚠️ Error: {e}")
    finally:
        if username in connected_users:
            del connected_users[username]
            if username in user_sessions:
                del user_sessions[username]
            await broadcast_user_list()

def get_username_by_id(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT username FROM users WHERE id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

async def send_friend_list(username):
    user_id = get_user_id(username)
    if not user_id or username not in connected_users:
        return
    friends = get_friends(user_id)
    friend_list = [{"username": f[0], "status": f[1]} for f in friends]
    await connected_users[username].send_text(json.dumps({
        "type": "friend_list",
        "friends": friend_list
    }))

async def send_friend_requests(username):
    user_id = get_user_id(username)
    if not user_id or username not in connected_users:
        return
    requests = get_friend_requests(user_id)
    request_list = [{"id": r[0], "username": r[1]} for r in requests]
    await connected_users[username].send_text(json.dumps({
        "type": "friend_requests",
        "requests": request_list
    }))

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

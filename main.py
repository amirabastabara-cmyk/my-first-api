# main.py - VoidVision Server with aiohttp (100% working on Render)
import os
import json
import hashlib
import sqlite3
import time
import asyncio
from aiohttp import web, WSMsgType

# ===================== DATABASE =====================
def init_db():
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

def user_exists(username):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT 1 FROM users WHERE username = ?', (username,))
    result = c.fetchone()
    conn.close()
    return result is not None

# ===================== INIT =====================
init_db()
connected_users = {}  # {username: websocket}
app = web.Application()

# ===================== ROUTES =====================
async def index(request):
    return web.Response(text="VoidVision Server is running!", content_type="text/plain")

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    username = None
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get("type")
                    
                    if msg_type == "register":
                        username = data.get("username", "").strip()
                        password = data.get("password", "").strip()
                        
                        if not username or not password:
                            await ws.send_str(json.dumps({
                                "type": "register_response",
                                "success": False,
                                "message": "Username and password are required!"
                            }))
                            continue
                        
                        if len(password) < 4:
                            await ws.send_str(json.dumps({
                                "type": "register_response",
                                "success": False,
                                "message": "Password must be at least 4 characters!"
                            }))
                            continue
                        
                        if register_user(username, password):
                            await ws.send_str(json.dumps({
                                "type": "register_response",
                                "success": True,
                                "message": "Registration successful! Please login."
                            }))
                        else:
                            await ws.send_str(json.dumps({
                                "type": "register_response",
                                "success": False,
                                "message": "Username already exists!"
                            }))
                    
                    elif msg_type == "login":
                        username = data.get("username", "").strip()
                        password = data.get("password", "").strip()
                        
                        if not username or not password:
                            await ws.send_str(json.dumps({
                                "type": "login_response",
                                "success": False,
                                "message": "Username and password are required!"
                            }))
                            continue
                        
                        if not user_exists(username):
                            await ws.send_str(json.dumps({
                                "type": "login_response",
                                "success": False,
                                "message": "Username not found! Please register first."
                            }))
                            continue
                        
                        if login_user(username, password):
                            if username in connected_users:
                                await ws.send_str(json.dumps({
                                    "type": "login_response",
                                    "success": False,
                                    "message": "User already logged in from another device!"
                                }))
                                continue
                            
                            connected_users[username] = ws
                            await ws.send_str(json.dumps({
                                "type": "login_response",
                                "success": True,
                                "message": "Login successful!"
                            }))
                            
                            print(f"✅ {username} connected (Total: {len(connected_users)})")
                            await broadcast_user_list()
                            
                            # دریافت پیام‌های بعدی
                            async for msg in ws:
                                if msg.type == WSMsgType.TEXT:
                                    try:
                                        data = json.loads(msg.data)
                                        msg_type = data.get("type")
                                        
                                        if msg_type == "chat_message":
                                            await broadcast({
                                                "type": "chat_message",
                                                "sender": username,
                                                "message": data.get("message", "")
                                            })
                                            print(f"💬 [{username}]: {data.get('message', '')}")
                                        
                                        elif msg_type == "game_invite":
                                            target = data.get("target")
                                            if target in connected_users:
                                                await connected_users[target].send_str(json.dumps({
                                                    "type": "game_invite",
                                                    "sender": username,
                                                    "game_name": data.get("game_name", "Unknown Game"),
                                                    "ip": data.get("ip", ""),
                                                    "port": data.get("port", 0)
                                                }))
                                                print(f"🎮 {username} invited {target}")
                                        
                                        elif msg_type == "get_users":
                                            await ws.send_str(json.dumps({
                                                "type": "user_list",
                                                "users": list(connected_users.keys())
                                            }))
                                        
                                        elif msg_type == "room_created":
                                            await broadcast({
                                                "type": "room_created",
                                                "room": data.get("room", {})
                                            })
                                        
                                        elif msg_type == "room_joined":
                                            await broadcast({
                                                "type": "room_joined",
                                                "room": data.get("room", {})
                                            })
                                        
                                        elif msg_type == "room_players":
                                            await broadcast({
                                                "type": "room_players",
                                                "players": data.get("players", []),
                                                "ips": data.get("ips", {}),
                                                "host": data.get("host", ""),
                                                "subnet": data.get("subnet", "10.77.0.0"),
                                                "room_key": data.get("room_key", "")
                                            })
                                        
                                        elif msg_type == "offer_received":
                                            target = data.get("target")
                                            if target in connected_users:
                                                await connected_users[target].send_str(json.dumps({
                                                    "type": "offer_received",
                                                    "from": username,
                                                    "sdp": data.get("sdp"),
                                                    "game_name": data.get("game_name", ""),
                                                    "public_key": data.get("public_key", "")
                                                }))
                                        
                                        elif msg_type == "answer_received":
                                            target = data.get("target")
                                            if target in connected_users:
                                                await connected_users[target].send_str(json.dumps({
                                                    "type": "answer_received",
                                                    "from": username,
                                                    "sdp": data.get("sdp")
                                                }))
                                        
                                        elif msg_type == "ice_candidate_received":
                                            target = data.get("target")
                                            if target in connected_users:
                                                await connected_users[target].send_str(json.dumps({
                                                    "type": "ice_candidate_received",
                                                    "from": username,
                                                    "candidate": data.get("candidate")
                                                }))
                                        
                                        elif msg_type == "friend_request":
                                            target = data.get("target")
                                            if target in connected_users:
                                                await connected_users[target].send_str(json.dumps({
                                                    "type": "friend_request_received",
                                                    "from": username
                                                }))
                                        
                                        elif msg_type == "friend_accept":
                                            target = data.get("target")
                                            if target in connected_users:
                                                await connected_users[target].send_str(json.dumps({
                                                    "type": "friend_accepted",
                                                    "from": username
                                                }))
                                        
                                        elif msg_type == "friend_reject":
                                            target = data.get("target")
                                            if target in connected_users:
                                                await connected_users[target].send_str(json.dumps({
                                                    "type": "friend_rejected",
                                                    "from": username
                                                }))
                                        
                                        elif msg_type == "player_ready":
                                            await broadcast({
                                                "type": "player_ready",
                                                "player": username,
                                                "room": data.get("room")
                                            })
                                        
                                        elif msg_type == "launch_game":
                                            await broadcast({
                                                "type": "launch_game_command",
                                                "game": data.get("game"),
                                                "room_id": data.get("room_id")
                                            })
                                        
                                        elif msg_type == "invite":
                                            target = data.get("target")
                                            if target in connected_users:
                                                await connected_users[target].send_str(json.dumps({
                                                    "type": "game_invite",
                                                    "from": username,
                                                    "room_id": data.get("room_id")
                                                }))
                                        
                                        elif msg_type == "logout":
                                            await ws.send_str(json.dumps({
                                                "type": "logout_response",
                                                "success": True,
                                                "message": "Logged out successfully!"
                                            }))
                                            await ws.close()
                                            
                                    except json.JSONDecodeError:
                                        pass
                            
                        else:
                            await ws.send_str(json.dumps({
                                "type": "login_response",
                                "success": False,
                                "message": "Invalid password!"
                            }))
                            
                except json.JSONDecodeError:
                    await ws.send_str(json.dumps({
                        "type": "error",
                        "message": "Invalid JSON format!"
                    }))
                    
            elif msg.type == WSMsgType.ERROR:
                break
                
    except Exception as e:
        print(f"⚠️ Error: {e}")
    finally:
        if username and username in connected_users:
            del connected_users[username]
            await broadcast_user_list()
            print(f"❌ {username} disconnected")
    
    return ws

async def broadcast(data):
    to_remove = []
    for name, ws in list(connected_users.items()):
        try:
            await ws.send_str(json.dumps(data))
        except:
            to_remove.append(name)
    
    for name in to_remove:
        if name in connected_users:
            del connected_users[name]

async def broadcast_user_list():
    await broadcast({"type": "user_list", "users": list(connected_users.keys())})

# ===================== APP ROUTES =====================
app.router.add_get('/', index)
app.router.add_get('/ws', websocket_handler)

# ===================== ENTRY =====================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"🚀 VoidVision Server starting on port {port}")
    web.run_app(app, host="0.0.0.0", port=port)

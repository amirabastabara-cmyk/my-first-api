# main.py - VoidVision Server (Full WebSocket + JWT + NAT Traversal)
import os
import json
import sqlite3
import hashlib
import uuid
import jwt
import time
import asyncio
import socket
import struct
import logging
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# ===== تنظیمات لاگ =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voidvision-server")

app = FastAPI(title="VoidVision Server")

# CORS - اجازه دسترسی از همه جا
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================== Config ==================
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret-on-render")
JWT_ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

# ================== Database ==================
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            user_id TEXT UNIQUE NOT NULL,
            created_at INTEGER,
            last_seen INTEGER,
            public_ip TEXT,
            nat_type TEXT DEFAULT 'Unknown'
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized")

init_db()

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def register_user(username, password, public_ip="", nat_type="Unknown"):
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        user_id = str(uuid.uuid4())
        password_hash = hash_password(password)
        c.execute(
            'INSERT INTO users (username, password_hash, user_id, created_at, last_seen, public_ip, nat_type) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (username, password_hash, user_id, int(time.time()), int(time.time()), public_ip, nat_type)
        )
        conn.commit()
        conn.close()
        return {"success": True, "user_id": user_id}
    except sqlite3.IntegrityError:
        return {"success": False, "message": "Username already exists"}
    except Exception as e:
        logger.error(f"Register error: {e}")
        return {"success": False, "message": str(e)}

def login_user(username, password):
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT password_hash, user_id FROM users WHERE username = ?', (username,))
        result = c.fetchone()
        conn.close()
        if result and result[0] == hash_password(password):
            return {"success": True, "user_id": result[1]}
        return {"success": False, "message": "Invalid credentials"}
    except Exception as e:
        logger.error(f"Login error: {e}")
        return {"success": False, "message": str(e)}

def user_exists(username):
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT 1 FROM users WHERE username = ?', (username,))
        result = c.fetchone()
        conn.close()
        return result is not None
    except:
        return False

def update_user_last_seen(username, public_ip, nat_type):
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute(
            'UPDATE users SET last_seen = ?, public_ip = ?, nat_type = ? WHERE username = ?',
            (int(time.time()), public_ip, nat_type, username)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Update user error: {e}")
        return False

# ================== JWT ==================
def create_token(username, user_id):
    payload = {
        "sub": username,
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_token(token):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    except Exception as e:
        logger.error(f"Token verify error: {e}")
        return None

# ================== NAT Type Detection ==================
def detect_nat_type(ip, port):
    """تشخیص نوع NAT با استفاده از STUN-like mechanism"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2)
        sock.connect(('stun.cloudflare.com', 3478))
        local_ip = sock.getsockname()[0]
        sock.close()
        if local_ip == ip:
            return "Full Cone (Open)"
        return "Symmetric NAT"
    except:
        return "Unknown"

# ================== TURN/STUN Helper ==================
def get_turn_servers():
    """بازگرداندن لیست TURN/STUN سرورها"""
    return [
        {
            "urls": ["turn:openrelay.metered.ca:80", "turn:openrelay.metered.ca:443", "turn:openrelay.metered.ca:5349"],
            "username": "openrelayproject",
            "credential": "openrelayproject"
        },
        {
            "urls": ["turn:turn.anyfirewall.com:3478"],
            "username": "anyfirewall",
            "credential": "anyfirewall"
        },
        {
            "urls": ["stun:stun.cloudflare.com:3478"],
            "username": "",
            "credential": ""
        },
        {
            "urls": ["stun:stun.l.google.com:19302"],
            "username": "",
            "credential": ""
        },
        {
            "urls": ["stun:stun.stunprotocol.org:3478"],
            "username": "",
            "credential": ""
        }
    ]

# ================== WebSocket ==================
connected_users = {}  # username -> websocket
user_data = {}        # username -> {"user_id": str, "token": str}
rooms = {}            # room_id -> {"name": str, "password": str, "host": str, "players": list, "ips": dict, "room_key": str}
room_counter = 0

@app.get("/")
async def root():
    return {
        "message": "VoidVision Server is running!",
        "version": "2.0",
        "status": "online",
        "turn_servers": get_turn_servers(),
        "online_users": len(connected_users),
        "rooms": len(rooms)
    }

@app.get("/api/turn")
async def get_turn_config():
    """Endpoint برای دریافت تنظیمات TURN/STUN"""
    return {
        "iceServers": get_turn_servers()
    }

@app.get("/api/status")
async def server_status():
    return {
        "online_users": len(connected_users),
        "rooms": len(rooms),
        "uptime": int(time.time())
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    current_user = None
    try:
        while True:
            try:
                data = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"WebSocket receive error: {e}")
                break

            try:
                msg = json.loads(data)
                msg_type = msg.get("type")
                client_ip = websocket.client.host if hasattr(websocket, 'client') and websocket.client else "unknown"
                nat_type = detect_nat_type(client_ip, 0)

                # ========== REGISTER ==========
                if msg_type == "register":
                    username = msg.get("username", "").strip()
                    password = msg.get("password", "").strip()
                    if not username or len(password) < 4:
                        await websocket.send_text(json.dumps({
                            "type": "register_response",
                            "success": False,
                            "message": "Username and password (min 4 chars) required"
                        }))
                        continue
                    result = register_user(username, password, client_ip, nat_type)
                    await websocket.send_text(json.dumps({
                        "type": "register_response",
                        "success": result.get("success", False),
                        "message": result.get("message", "Registration successful")
                    }))
                    continue

                # ========== LOGIN ==========
                if msg_type == "login":
                    username = msg.get("username", "").strip()
                    password = msg.get("password", "").strip()
                    if not username or not password:
                        await websocket.send_text(json.dumps({
                            "type": "login_response",
                            "success": False,
                            "message": "Username and password required"
                        }))
                        continue
                    result = login_user(username, password)
                    if not result.get("success"):
                        await websocket.send_text(json.dumps({
                            "type": "login_response",
                            "success": False,
                            "message": result.get("message", "Invalid credentials")
                        }))
                        continue
                    # Remove old connection if any
                    if username in connected_users:
                        try:
                            await connected_users[username].close()
                        except:
                            pass
                        del connected_users[username]
                    token = create_token(username, result["user_id"])
                    user_data[username] = {"user_id": result["user_id"], "token": token}
                    connected_users[username] = websocket
                    current_user = username
                    
                    # Update last seen and NAT info
                    update_user_last_seen(username, client_ip, nat_type)
                    
                    await websocket.send_text(json.dumps({
                        "type": "login_response",
                        "success": True,
                        "username": username,
                        "user_id": result["user_id"],
                        "token": token,
                        "nat_type": nat_type,
                        "turn_servers": get_turn_servers()
                    }))
                    # Broadcast user list
                    await broadcast_user_list()
                    continue

                # ========== AUTH ==========
                if msg_type == "auth":
                    token = msg.get("token")
                    if not token:
                        await websocket.send_text(json.dumps({
                            "type": "auth_response",
                            "success": False,
                            "message": "No token"
                        }))
                        continue
                    payload = verify_token(token)
                    if not payload:
                        await websocket.send_text(json.dumps({
                            "type": "auth_response",
                            "success": False,
                            "message": "Invalid token"
                        }))
                        continue
                    username = payload.get("sub")
                    user_id = payload.get("user_id")
                    if not username or not user_exists(username):
                        await websocket.send_text(json.dumps({
                            "type": "auth_response",
                            "success": False,
                            "message": "User not found"
                        }))
                        continue
                    if username in connected_users:
                        try:
                            await connected_users[username].close()
                        except:
                            pass
                        del connected_users[username]
                    connected_users[username] = websocket
                    user_data[username] = {"user_id": user_id, "token": token}
                    current_user = username
                    
                    update_user_last_seen(username, client_ip, nat_type)
                    
                    await websocket.send_text(json.dumps({
                        "type": "auth_response",
                        "success": True,
                        "username": username,
                        "user_id": user_id,
                        "nat_type": nat_type,
                        "turn_servers": get_turn_servers()
                    }))
                    await broadcast_user_list()
                    continue

                # ========== AUTH REQUIRED ==========
                if not current_user:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Not authenticated"
                    }))
                    continue

                # ========== GET USERS ==========
                if msg_type == "get_users":
                    await websocket.send_text(json.dumps({
                        "type": "user_list",
                        "users": list(connected_users.keys())
                    }))
                    continue

                # ========== CHAT ==========
                if msg_type == "chat_message":
                    text = msg.get("message", "")[:500]
                    if text:
                        await broadcast({
                            "type": "chat_message",
                            "sender": current_user,
                            "message": text
                        }, exclude=current_user)
                    continue

                # ========== GAME INVITE ==========
                if msg_type == "game_invite":
                    target = msg.get("target")
                    game_name = msg.get("game_name", "Unknown Game")
                    ip = msg.get("ip", "")
                    port = msg.get("port", 0)
                    if target and target in connected_users:
                        await connected_users[target].send_text(json.dumps({
                            "type": "game_invite",
                            "sender": current_user,
                            "game_name": game_name,
                            "ip": ip,
                            "port": port,
                            "nat_type": nat_type,
                            "turn_servers": get_turn_servers()
                        }))
                    continue

                # ========== ROOM LIST ==========
                if msg_type == "get_room_list":
                    room_list = []
                    for rid, room in rooms.items():
                        room_list.append({
                            "room_id": rid,
                            "room_name": room["name"],
                            "has_password": bool(room.get("password")),
                            "count": len(room["players"]),
                            "max": room.get("max_players", 8)
                        })
                    await websocket.send_text(json.dumps({
                        "type": "room_list",
                        "rooms": room_list
                    }))
                    continue

                # ========== CREATE ROOM ==========
                if msg_type == "create_room":
                    room_name = msg.get("room_name", "Room")
                    password = msg.get("password", "")
                    max_players = msg.get("max_players", 8)
                    room_id = str(uuid.uuid4())[:6]
                    rooms[room_id] = {
                        "name": room_name,
                        "password": password,
                        "max_players": max_players,
                        "host": current_user,
                        "players": [current_user],
                        "ips": {current_user: "10.77.0.1"},
                        "room_key": str(uuid.uuid4()),
                        "next_ip": 2,
                        "created_at": int(time.time()),
                        "turn_servers": get_turn_servers()
                    }
                    await websocket.send_text(json.dumps({
                        "type": "room_created",
                        "room": {
                            "room_id": room_id,
                            "room_name": room_name,
                            "room_key": rooms[room_id]["room_key"],
                            "subnet": "10.77.0.0",
                            "turn_servers": get_turn_servers()
                        }
                    }))
                    await broadcast_room_list()
                    continue

                # ========== JOIN ROOM ==========
                if msg_type == "join_room":
                    room_id = msg.get("room_id")
                    password = msg.get("password", "")
                    if room_id not in rooms:
                        await websocket.send_text(json.dumps({
                            "type": "room_joined",
                            "success": False,
                            "message": "Room not found"
                        }))
                        continue
                    room = rooms[room_id]
                    if room.get("password") and room["password"] != password:
                        await websocket.send_text(json.dumps({
                            "type": "room_joined",
                            "success": False,
                            "message": "Wrong password"
                        }))
                        continue
                    if len(room["players"]) >= room["max_players"]:
                        await websocket.send_text(json.dumps({
                            "type": "room_joined",
                            "success": False,
                            "message": "Room full"
                        }))
                        continue
                    if current_user not in room["players"]:
                        room["players"].append(current_user)
                        ip_idx = room.get("next_ip", len(room["players"]) + 1)
                        room["ips"][current_user] = f"10.77.0.{ip_idx}"
                        room["next_ip"] = ip_idx + 1
                    await websocket.send_text(json.dumps({
                        "type": "room_joined",
                        "room": {
                            "room_id": room_id,
                            "room_name": room["name"],
                            "room_key": room["room_key"],
                            "subnet": "10.77.0.0",
                            "players": room["players"],
                            "ips": room["ips"],
                            "host": room["host"],
                            "turn_servers": get_turn_servers()
                        }
                    }))
                    await broadcast_room_players(room_id)
                    continue

                # ========== LEAVE ROOM ==========
                if msg_type == "leave_room":
                    for rid, room in list(rooms.items()):
                        if current_user in room["players"]:
                            room["players"].remove(current_user)
                            room["ips"].pop(current_user, None)
                            if not room["players"] or room["host"] == current_user:
                                del rooms[rid]
                                await broadcast_room_list()
                            else:
                                await broadcast_room_players(rid)
                            break
                    await websocket.send_text(json.dumps({
                        "type": "left_room",
                        "success": True
                    }))
                    continue

                # ========== OFFER ==========
                if msg_type == "offer":
                    target = msg.get("target")
                    sdp = msg.get("sdp")
                    game = msg.get("game_name", "")
                    public_key = msg.get("public_key", "")
                    if target and target in connected_users and sdp:
                        await connected_users[target].send_text(json.dumps({
                            "type": "offer_received",
                            "from": current_user,
                            "sdp": sdp,
                            "game_name": game,
                            "public_key": public_key,
                            "turn_servers": get_turn_servers()
                        }))
                    continue

                # ========== ANSWER ==========
                if msg_type == "answer":
                    target = msg.get("target")
                    sdp = msg.get("sdp")
                    if target and target in connected_users and sdp:
                        await connected_users[target].send_text(json.dumps({
                            "type": "answer_received",
                            "from": current_user,
                            "sdp": sdp
                        }))
                    continue

                # ========== ICE CANDIDATE ==========
                if msg_type == "ice_candidate":
                    target = msg.get("target")
                    candidate = msg.get("candidate")
                    if target and target in connected_users and candidate:
                        await connected_users[target].send_text(json.dumps({
                            "type": "ice_candidate_received",
                            "from": current_user,
                            "candidate": candidate
                        }))
                    continue

                # ========== FRIEND REQUEST ==========
                if msg_type == "friend_request":
                    target = msg.get("target")
                    if target and target in user_data and target in connected_users:
                        await connected_users[target].send_text(json.dumps({
                            "type": "friend_request_received",
                            "from": current_user
                        }))
                    continue

                # ========== FRIEND ACCEPT ==========
                if msg_type == "friend_accept":
                    target = msg.get("target")
                    if target and target in user_data and target in connected_users:
                        await connected_users[target].send_text(json.dumps({
                            "type": "friend_accepted",
                            "from": current_user
                        }))
                    continue

                # ========== GET TURN ==========
                if msg_type == "get_turn":
                    await websocket.send_text(json.dumps({
                        "type": "turn_response",
                        "turn_servers": get_turn_servers()
                    }))
                    continue

                # ========== PLAYER READY ==========
                if msg_type == "player_ready":
                    room_id = msg.get("room_id")
                    if room_id and room_id in rooms:
                        await broadcast({
                            "type": "player_ready",
                            "player": current_user,
                            "room_id": room_id
                        })
                    continue

                # ========== LAUNCH GAME COMMAND ==========
                if msg_type == "launch_game_command":
                    game = msg.get("game", "")
                    room_id = msg.get("room_id")
                    if room_id and room_id in rooms:
                        await broadcast({
                            "type": "launch_game_command",
                            "game": game,
                            "room_id": room_id,
                            "sender": current_user
                        }, exclude=current_user)
                    continue

                # ========== UNKNOWN ==========
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": f"Unknown message type: {msg_type}"
                }))

            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Invalid JSON"
                }))
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                try:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": str(e)
                    }))
                except:
                    pass

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if current_user and current_user in connected_users:
            del connected_users[current_user]
            await broadcast_user_list()
            # Remove from rooms
            for rid, room in list(rooms.items()):
                if current_user in room["players"]:
                    room["players"].remove(current_user)
                    room["ips"].pop(current_user, None)
                    if not room["players"] or room["host"] == current_user:
                        del rooms[rid]
                        await broadcast_room_list()
                    else:
                        await broadcast_room_players(rid)

# ================== Broadcast Helpers ==================
async def broadcast(data, exclude=None):
    for name, ws in list(connected_users.items()):
        if name != exclude:
            try:
                await ws.send_text(json.dumps(data))
            except Exception as e:
                logger.error(f"Broadcast to {name} failed: {e}")

async def broadcast_user_list():
    await broadcast({
        "type": "user_list",
        "users": list(connected_users.keys())
    })

async def broadcast_room_list():
    room_list = []
    for rid, room in rooms.items():
        room_list.append({
            "room_id": rid,
            "room_name": room["name"],
            "has_password": bool(room.get("password")),
            "count": len(room["players"]),
            "max": room.get("max_players", 8)
        })
    await broadcast({
        "type": "room_list",
        "rooms": room_list
    })

async def broadcast_room_players(room_id):
    if room_id not in rooms:
        return
    room = rooms[room_id]
    await broadcast({
        "type": "room_players",
        "players": room["players"],
        "ips": room["ips"],
        "host": room["host"],
        "subnet": "10.77.0.0",
        "room_key": room["room_key"],
        "turn_servers": get_turn_servers()
    })

# ================== Cleanup old users ==================
async def cleanup_users():
    """پاک کردن کاربران قدیمی از دیتابیس"""
    while True:
        try:
            await asyncio.sleep(3600)  # هر یک ساعت
            conn = sqlite3.connect('users.db')
            c = conn.cursor()
            # حذف کاربرانی که بیش از 30 روز قبل آخرین بار دیده شده‌اند
            c.execute('DELETE FROM users WHERE last_seen < ?', (int(time.time()) - 2592000,))
            conn.commit()
            conn.close()
            logger.info("🧹 Cleaned old users from database")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

# ================== Entry ==================
if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    
    # Start cleanup task in background
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    # Run cleanup in background
    asyncio.create_task(cleanup_users())
    
    logger.info(f"🚀 VoidVision Server starting on port {port}")
    logger.info(f"🌐 WebSocket endpoint: ws://0.0.0.0:{port}/ws")
    logger.info(f"📡 TURN/STUN servers available")
    logger.info(f"📊 Online users: {len(connected_users)}")
    
    uvicorn.run(app, host="0.0.0.0", port=port)

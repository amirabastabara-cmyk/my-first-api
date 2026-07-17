# main.py - VoidVision Server (Full Compatible with Your Launcher)
import os
import json
import sqlite3
import hashlib
import uuid
import time
import random
import socket
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

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
            created_at INTEGER
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def register_user(username, password):
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        user_id = str(uuid.uuid4())
        password_hash = hash_password(password)
        c.execute(
            'INSERT INTO users (username, password_hash, user_id, created_at) VALUES (?, ?, ?, ?)',
            (username, password_hash, user_id, int(time.time()))
        )
        conn.commit()
        conn.close()
        return {"success": True, "user_id": user_id}
    except sqlite3.IntegrityError:
        return {"success": False, "message": "Username already exists"}

def login_user(username, password):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT password_hash, user_id FROM users WHERE username = ?', (username,))
    result = c.fetchone()
    conn.close()
    if result and result[0] == hash_password(password):
        return {"success": True, "user_id": result[1]}
    return {"success": False, "message": "Invalid credentials"}

def user_exists(username):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT 1 FROM users WHERE username = ?', (username,))
    result = c.fetchone()
    conn.close()
    return result is not None

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
    except:
        return None

# ================== WebSocket ==================
connected_users = {}  # username -> websocket
user_data = {}        # username -> {"user_id": str, "token": str}
rooms = {}            # room_id -> {"name": str, "password": str, "host": str, "players": list, "ips": dict, "room_key": str}

@app.get("/")
async def root():
    return {"message": "VoidVision Server is running!"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    current_user = None
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                msg_type = msg.get("type")

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
                    result = register_user(username, password)
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
                    
                    # چک کردن وجود کاربر
                    if not user_exists(username):
                        await websocket.send_text(json.dumps({
                            "type": "login_response",
                            "success": False,
                            "message": "Username not found! Please register first."
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
                    
                    await websocket.send_text(json.dumps({
                        "type": "login_response",
                        "success": True,
                        "username": username,
                        "user_id": result["user_id"],
                        "token": token
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
                    await websocket.send_text(json.dumps({
                        "type": "auth_response",
                        "success": True,
                        "username": username,
                        "user_id": user_id
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
                        })
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
                            "port": port
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
                            "max": 8
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
                        "next_ip": 2
                    }
                    await websocket.send_text(json.dumps({
                        "type": "room_created",
                        "room": {
                            "room_id": room_id,
                            "room_name": room_name,
                            "room_key": rooms[room_id]["room_key"],
                            "subnet": "10.77.0.0"
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
                            "host": room["host"]
                        }
                    }))
                    # Broadcast room players to all
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
                            "public_key": public_key
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
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": str(e)
                }))

    except WebSocketDisconnect:
        pass
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
            except:
                pass

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
        "room_key": room["room_key"]
    })

# ================== Entry ==================
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

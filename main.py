# main.py - VoidVision Server (FastAPI + WebSocket)
import os
import json
import sqlite3
import hashlib
import uuid
import jwt
import time
import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, List, Optional

# ===== لاگ =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voidvision-server")

# ===== App =====
app = FastAPI(title="VoidVision Server", version="35.0")

# ===== CORS =====
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== تنظیمات =====
JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_urlsafe(32))
JWT_ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * 24 * 7

# ===== دیتابیس =====
DB_FILE = "users.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            user_id TEXT UNIQUE NOT NULL,
            created_at INTEGER,
            last_seen INTEGER
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("✅ دیتابیس آماده شد")

init_db()

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def register_user(username: str, password: str):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        user_id = str(uuid.uuid4())
        c.execute(
            'INSERT INTO users (username, password_hash, user_id, created_at, last_seen) VALUES (?, ?, ?, ?, ?)',
            (username, hash_password(password), user_id, int(time.time()), int(time.time()))
        )
        conn.commit()
        conn.close()
        return {"success": True, "user_id": user_id}
    except sqlite3.IntegrityError:
        return {"success": False, "message": "نام کاربری تکراری است"}
    except Exception as e:
        return {"success": False, "message": str(e)}

def login_user(username: str, password: str):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT password_hash, user_id FROM users WHERE username = ?', (username,))
        result = c.fetchone()
        conn.close()
        if result and result[0] == hash_password(password):
            return {"success": True, "user_id": result[1]}
        return {"success": False, "message": "نام کاربری یا رمز اشتباه است"}
    except Exception as e:
        return {"success": False, "message": str(e)}

def user_exists(username: str) -> bool:
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT 1 FROM users WHERE username = ?', (username,))
        return c.fetchone() is not None
    except:
        return False

# ===== JWT =====
def create_token(username: str, user_id: str) -> str:
    payload = {
        "sub": username,
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_token(token: str):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except:
        return None

# ===== TURN/STUN =====
def get_turn_servers():
    return [
        {"urls": ["turn:openrelay.metered.ca:80?transport=udp", "turn:openrelay.metered.ca:443?transport=tcp"],
         "username": "openrelayproject", "credential": "openrelayproject"},
        {"urls": ["turn:turn.anyfirewall.com:3478?transport=udp", "turn:turn.anyfirewall.com:3478?transport=tcp"],
         "username": "anyfirewall", "credential": "anyfirewall"},
        {"urls": ["stun:stun.cloudflare.com:3478"], "username": "", "credential": ""},
        {"urls": ["stun:stun.l.google.com:19302"], "username": "", "credential": ""},
        {"urls": ["stun:stun.stunprotocol.org:3478"], "username": "", "credential": ""}
    ]

# ===== WebSocket State =====
connected_users: Dict[str, WebSocket] = {}
user_data: Dict[str, Dict] = {}
rooms: Dict[str, Dict] = {}

# ===== REST API =====
@app.get("/")
async def root():
    return {
        "message": "VoidVision Server is running!",
        "version": "35.0",
        "status": "online",
        "online_users": len(connected_users),
        "rooms": len(rooms),
        "turn_servers": get_turn_servers()
    }

@app.get("/api/status")
async def status():
    return {"online_users": len(connected_users), "rooms": len(rooms)}

# ===== WebSocket =====
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("✅ WebSocket connected")
    current_user = None

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            msg_type = msg.get("type")
            logger.info(f"📩 Received: {msg_type}")

            # ===== REGISTER =====
            if msg_type == "register":
                username = msg.get("username", "").strip()
                password = msg.get("password", "").strip()
                if not username or len(password) < 4:
                    await websocket.send_text(json.dumps({
                        "type": "register_response",
                        "success": False,
                        "message": "نام کاربری و رمز (حداقل ۴ کاراکتر) الزامی است"
                    }))
                    continue
                result = register_user(username, password)
                await websocket.send_text(json.dumps({
                    "type": "register_response",
                    "success": result.get("success", False),
                    "message": result.get("message", "ثبت‌نام موفق")
                }))
                continue

            # ===== LOGIN =====
            if msg_type == "login":
                username = msg.get("username", "").strip()
                password = msg.get("password", "").strip()
                if not username or not password:
                    await websocket.send_text(json.dumps({
                        "type": "login_response",
                        "success": False,
                        "message": "نام کاربری و رمز الزامی است"
                    }))
                    continue
                result = login_user(username, password)
                if not result.get("success"):
                    await websocket.send_text(json.dumps({
                        "type": "login_response",
                        "success": False,
                        "message": result.get("message")
                    }))
                    continue
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
                    "token": token,
                    "turn_servers": get_turn_servers()
                }))
                await broadcast_user_list()
                continue

            # ===== AUTH =====
            if msg_type == "auth":
                token = msg.get("token")
                if not token:
                    await websocket.send_text(json.dumps({
                        "type": "auth_response",
                        "success": False,
                        "message": "توکن ارسال نشده"
                    }))
                    continue
                payload = verify_token(token)
                if not payload:
                    await websocket.send_text(json.dumps({
                        "type": "auth_response",
                        "success": False,
                        "message": "توکن نامعتبر"
                    }))
                    continue
                username = payload.get("sub")
                user_id = payload.get("user_id")
                if not username or not user_exists(username):
                    await websocket.send_text(json.dumps({
                        "type": "auth_response",
                        "success": False,
                        "message": "کاربر یافت نشد"
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
                    "user_id": user_id,
                    "turn_servers": get_turn_servers()
                }))
                await broadcast_user_list()
                continue

            # ===== AUTH REQUIRED =====
            if not current_user:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "احراز هویت نشده‌اید"
                }))
                continue

            # ===== GET USERS =====
            if msg_type == "get_users":
                await websocket.send_text(json.dumps({
                    "type": "user_list",
                    "users": list(connected_users.keys())
                }))
                continue

            # ===== CHAT =====
            if msg_type == "chat_message":
                text = msg.get("message", "")[:500]
                if text:
                    await broadcast({
                        "type": "chat_message",
                        "sender": current_user,
                        "message": text
                    }, exclude=current_user)
                continue

            # ===== CREATE ROOM =====
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
                    "created_at": int(time.time())
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

            # ===== JOIN ROOM =====
            if msg_type == "join_room":
                room_id = msg.get("room_id")
                password = msg.get("password", "")
                if room_id not in rooms:
                    await websocket.send_text(json.dumps({
                        "type": "room_joined",
                        "success": False,
                        "message": "اتاق یافت نشد"
                    }))
                    continue
                room = rooms[room_id]
                if room.get("password") and room["password"] != password:
                    await websocket.send_text(json.dumps({
                        "type": "room_joined",
                        "success": False,
                        "message": "رمز اشتباه است"
                    }))
                    continue
                if len(room.get("players", [])) >= room.get("max_players", 8):
                    await websocket.send_text(json.dumps({
                        "type": "room_joined",
                        "success": False,
                        "message": "اتاق پر است"
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
                        "room_name": room.get("name"),
                        "room_key": room.get("room_key"),
                        "subnet": "10.77.0.0",
                        "players": room.get("players", []),
                        "ips": room.get("ips", {}),
                        "host": room.get("host")
                    }
                }))
                await broadcast_room_players(room_id)
                continue

            # ===== LEAVE ROOM =====
            if msg_type == "leave_room":
                for rid, room in list(rooms.items()):
                    if current_user in room.get("players", []):
                        room["players"].remove(current_user)
                        room["ips"].pop(current_user, None)
                        if not room.get("players") or room.get("host") == current_user:
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

            # ===== OFFER =====
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

            # ===== ANSWER =====
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

            # ===== GAME INVITE =====
            if msg_type == "game_invite":
                target = msg.get("target")
                game_name = msg.get("game_name", "")
                if target and target in connected_users:
                    await connected_users[target].send_text(json.dumps({
                        "type": "game_invite",
                        "sender": current_user,
                        "game_name": game_name
                    }))
                continue

            # ===== FRIEND REQUEST =====
            if msg_type == "friend_request":
                target = msg.get("target")
                if target and target in user_data:
                    if target in connected_users:
                        await connected_users[target].send_text(json.dumps({
                            "type": "friend_request_received",
                            "from": current_user
                        }))
                continue

            # ===== FRIEND ACCEPT =====
            if msg_type == "friend_accept":
                target = msg.get("target")
                if target and target in user_data:
                    if target in connected_users:
                        await connected_users[target].send_text(json.dumps({
                            "type": "friend_accepted",
                            "from": current_user
                        }))
                continue

            # ===== GET FRIENDS =====
            if msg_type == "get_friends":
                await websocket.send_text(json.dumps({
                    "type": "friends_list",
                    "friends": []
                }))
                continue

            # ===== GET FRIEND REQUESTS =====
            if msg_type == "get_friend_requests":
                await websocket.send_text(json.dumps({
                    "type": "friend_requests_list",
                    "requests": []
                }))
                continue

            # ===== PLAYER READY =====
            if msg_type == "player_ready":
                room_id = msg.get("room_id")
                if room_id and room_id in rooms:
                    await broadcast({
                        "type": "player_ready",
                        "player": current_user,
                        "room_id": room_id
                    })
                continue

            # ===== LAUNCH GAME =====
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

            # ===== GET TURN =====
            if msg_type == "get_turn":
                await websocket.send_text(json.dumps({
                    "type": "turn_response",
                    "turn_servers": get_turn_servers()
                }))
                continue

            # ===== UNKNOWN =====
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": f"نوع پیام ناشناس: {msg_type}"
            }))

    except WebSocketDisconnect:
        logger.info(f"🔴 WebSocket disconnected: {current_user}")
    except Exception as e:
        logger.error(f"❌ WebSocket error: {e}")
    finally:
        if current_user and current_user in connected_users:
            del connected_users[current_user]
            await broadcast_user_list()
            for rid, room in list(rooms.items()):
                if current_user in room.get("players", []):
                    room["players"].remove(current_user)
                    room["ips"].pop(current_user, None)
                    if not room.get("players") or room.get("host") == current_user:
                        del rooms[rid]
                        await broadcast_room_list()
                    else:
                        await broadcast_room_players(rid)

# ===== Broadcast Helpers =====
async def broadcast(data: dict, exclude: Optional[str] = None):
    for name, ws in list(connected_users.items()):
        if name != exclude:
            try:
                await ws.send_text(json.dumps(data))
            except:
                pass

async def broadcast_user_list():
    await broadcast({"type": "user_list", "users": list(connected_users.keys())})

async def broadcast_room_list():
    room_list = []
    for rid, room in rooms.items():
        room_list.append({
            "room_id": rid,
            "room_name": room.get("name", "Room"),
            "has_password": bool(room.get("password")),
            "count": len(room.get("players", [])),
            "max": room.get("max_players", 8)
        })
    await broadcast({"type": "room_list", "rooms": room_list})

async def broadcast_room_players(room_id: str):
    if room_id not in rooms:
        return
    room = rooms[room_id]
    await broadcast({
        "type": "room_players",
        "players": room.get("players", []),
        "ips": room.get("ips", {}),
        "host": room.get("host"),
        "subnet": "10.77.0.0",
        "room_key": room.get("room_key")
    })

# ===== Entry =====
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"🚀 Server starting on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)

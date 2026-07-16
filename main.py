#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
VoidVision Server v3.0 - Stable Signaling Server
با WebSocket Keep-Alive و مدیریت کامل Room
"""

import asyncio
import json
import os
import uuid
import time
import hashlib
import secrets
import logging
from typing import Dict, Optional, Set
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ============================================================
# CONFIGURATION
# ============================================================
VERSION = "3.0.0"
MAX_USERNAME_LENGTH = 32
HEARTBEAT_INTERVAL = 15  # ثانیه
HEARTBEAT_TIMEOUT = 10   # ثانیه

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | %(message)s"
)
logger = logging.getLogger("voidvision-server")

# ============================================================
# DATABASE (در حافظه)
# ============================================================
users: Dict[str, dict] = {}        # username -> {user_id, password_hash, ...}
connections: Dict[str, WebSocket] = {}  # user_id -> WebSocket
user_rooms: Dict[str, str] = {}    # user_id -> room_id
rooms: Dict[str, dict] = {}        # room_id -> {...}
ip_pools: Dict[str, Set[str]] = {} # subnet -> used_ips

# ============================================================
# Helper Functions
# ============================================================
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed

def generate_room_id() -> str:
    return secrets.token_hex(4).upper()

def generate_subnet() -> str:
    import random
    x = random.randint(1, 254)
    return f"10.77.{x}."

def get_next_ip(subnet: str, used_ips: set) -> Optional[str]:
    for i in range(2, 255):
        ip = f"{subnet}{i}"
        if ip not in used_ips:
            return ip
    return None

# ============================================================
# FastAPI App
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"🚀 VoidVision Server v{VERSION} starting...")
    # شروع تسک پاک‌سازی روم‌های قدیمی
    asyncio.create_task(cleanup_rooms_task())
    yield
    logger.info("🛑 VoidVision Server stopped")

app = FastAPI(
    title="VoidVision Server",
    version=VERSION,
    lifespan=lifespan
)

# ============================================================
# CORS
# ============================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# Background Tasks
# ============================================================
async def cleanup_rooms_task():
    """پاک‌سازی خودکار روم‌های خالی هر ۶۰ ثانیه"""
    while True:
        await asyncio.sleep(60)
        try:
            now = time.time()
            to_delete = []
            for room_id, room in rooms.items():
                # حذف روم‌هایی که بیش از ۳۰ دقیقه غیرفعال بودن
                if now - room.get("last_activity", now) > 1800:
                    to_delete.append(room_id)
                # حذف روم‌های خالی
                elif not room.get("players", []):
                    to_delete.append(room_id)
            
            for room_id in to_delete:
                if room_id in rooms:
                    subnet = rooms[room_id].get("subnet")
                    if subnet and subnet in ip_pools:
                        del ip_pools[subnet]
                    del rooms[room_id]
                    logger.info(f"🗑️ Room {room_id} cleaned up")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

# ============================================================
# WebSocket Endpoint (با Keep-Alive)
# ============================================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    user_id = None
    username = None
    connected = True

    try:
        # ====== دریافت اولین پیام (Login/Register/Auth) ======
        raw = await websocket.receive_text()
        try:
            data = json.loads(raw)
        except:
            await websocket.send_text(json.dumps({"type": "error", "code": 400, "message": "Invalid JSON"}))
            await websocket.close()
            return

        msg_type = data.get("type")

        # ---------- REGISTER ----------
        if msg_type == "register":
            username_val = data.get("username", "").strip()
            password_val = data.get("password", "")
            
            if not username_val or not password_val or len(password_val) < 4:
                await websocket.send_text(json.dumps({
                    "type": "register_response",
                    "success": False,
                    "message": "Invalid username or password (min 4 chars)"
                }))
                await websocket.close()
                return
                
            if username_val in users:
                await websocket.send_text(json.dumps({
                    "type": "register_response",
                    "success": False,
                    "message": "Username already taken"
                }))
                await websocket.close()
                return
                
            user_id = str(uuid.uuid4())
            users[username_val] = {
                "user_id": user_id,
                "password_hash": hash_password(password_val),
                "created_at": time.time()
            }
            
            await websocket.send_text(json.dumps({
                "type": "register_response",
                "success": True,
                "message": "Registration successful! Please login."
            }))
            await websocket.close()
            return

        # ---------- LOGIN ----------
        if msg_type == "login":
            username_val = data.get("username", "").strip()
            password_val = data.get("password", "")
            
            user_data = users.get(username_val)
            if not user_data:
                await websocket.send_text(json.dumps({
                    "type": "login_response",
                    "success": False,
                    "message": "User not found"
                }))
                await websocket.close()
                return
                
            if not verify_password(password_val, user_data["password_hash"]):
                await websocket.send_text(json.dumps({
                    "type": "login_response",
                    "success": False,
                    "message": "Invalid password"
                }))
                await websocket.close()
                return
                
            user_id = user_data["user_id"]
            username = username_val
            token = secrets.token_hex(32)

            connections[user_id] = websocket
            users[username_val]["connected_at"] = time.time()
            users[username_val]["token"] = token

            await websocket.send_text(json.dumps({
                "type": "login_response",
                "success": True,
                "username": username,
                "user_id": user_id,
                "token": token,
            }))

            logger.info(f"✅ User {username} ({user_id}) logged in")
            await broadcast_user_list()

            # ====== MAIN MESSAGE LOOP (با Keep-Alive) ======
            while connected:
                try:
                    # دریافت پیام با timeout برای Keep-Alive
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=HEARTBEAT_INTERVAL)
                    data = json.loads(raw)
                    await handle_message(user_id, username, data, websocket)
                except asyncio.TimeoutError:
                    # ارسال Ping برای بررسی زنده بودن اتصال
                    try:
                        await websocket.send_text(json.dumps({"type": "ping"}))
                    except:
                        connected = False
                        break
                except WebSocketDisconnect:
                    connected = False
                    break
                except json.JSONDecodeError:
                    await websocket.send_text(json.dumps({"type": "error", "code": 400, "message": "Invalid JSON"}))
                except Exception as e:
                    logger.error(f"❌ Message error: {e}")
                    await websocket.send_text(json.dumps({"type": "error", "code": 500, "message": str(e)}))

            await cleanup_user(user_id, username)
            return

        # ---------- AUTH WITH TOKEN ----------
        if msg_type == "auth":
            token = data.get("token", "")
            if not token:
                await websocket.send_text(json.dumps({"type": "error", "code": 401, "message": "Token required"}))
                await websocket.close()
                return
                
            found = False
            for uname, udata in users.items():
                if udata.get("token") == token:
                    user_id = udata["user_id"]
                    username = uname
                    connections[user_id] = websocket
                    await websocket.send_text(json.dumps({
                        "type": "auth_success",
                        "username": username,
                        "user_id": user_id,
                        "token": token,
                    }))
                    logger.info(f"✅ User {username} reconnected via token")
                    await broadcast_user_list()
                    found = True
                    break
                    
            if not found:
                await websocket.send_text(json.dumps({"type": "error", "code": 401, "message": "Invalid token"}))
                await websocket.close()
                return
                
            # ====== MAIN MESSAGE LOOP (با Keep-Alive) ======
            while connected:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=HEARTBEAT_INTERVAL)
                    data = json.loads(raw)
                    await handle_message(user_id, username, data, websocket)
                except asyncio.TimeoutError:
                    try:
                        await websocket.send_text(json.dumps({"type": "ping"}))
                    except:
                        connected = False
                        break
                except WebSocketDisconnect:
                    connected = False
                    break
                except json.JSONDecodeError:
                    await websocket.send_text(json.dumps({"type": "error", "code": 400, "message": "Invalid JSON"}))
                except Exception as e:
                    logger.error(f"❌ Message error: {e}")
                    await websocket.send_text(json.dumps({"type": "error", "code": 500, "message": str(e)}))
                    
            await cleanup_user(user_id, username)
            return

        else:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 400,
                "message": "First message must be 'login', 'register' or 'auth'"
            }))
            await websocket.close()
            return

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"❌ WebSocket error: {e}")
    finally:
        if user_id and username:
            await cleanup_user(user_id, username)

# ============================================================
# Cleanup
# ============================================================
async def cleanup_user(user_id: str, username: str):
    room_id = user_rooms.get(user_id)
    if room_id:
        await leave_room(user_id, room_id)
    connections.pop(user_id, None)
    user_rooms.pop(user_id, None)
    await broadcast_user_list()

# ============================================================
# Message Handler
# ============================================================
async def handle_message(user_id: str, username: str, data: dict, websocket: WebSocket):
    msg_type = data.get("type")

    # ====== PING (پاسخ به Keep-Alive) ======
    if msg_type == "ping" or msg_type == "pong":
        await websocket.send_text(json.dumps({"type": "pong"}))
        return

    elif msg_type == "get_user_list":
        await send_user_list(websocket)

    elif msg_type == "chat_message":
        message = data.get("message", "").strip()
        if not message or len(message) > 500:
            return
        room_id = user_rooms.get(user_id)
        if room_id:
            room = rooms.get(room_id)
            if room:
                for uid in room.get("players", []):
                    if uid in connections:
                        try:
                            await connections[uid].send_text(json.dumps({
                                "type": "chat_message",
                                "sender": username,
                                "message": message,
                                "room_id": room_id,
                            }))
                        except:
                            pass

    elif msg_type == "create_room":
        game_name = data.get("game_name", "Unknown Game")
        max_players = min(data.get("max_players", 8), 16)
        room_name = data.get("room_name", game_name)
        password = data.get("password", "")

        # هر کاربر حداکثر ۵ روم
        user_rooms_count = len([r for r in rooms.values() if user_id in r.get("players", [])])
        if user_rooms_count >= 5:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 429,
                "message": "Maximum 5 rooms per user",
            }))
            return

        room_id = generate_room_id()
        while room_id in rooms:
            room_id = generate_room_id()

        subnet = generate_subnet()
        while subnet in ip_pools:
            subnet = generate_subnet()

        ip_pools[subnet] = {f"{subnet}1"}
        host_ip = f"{subnet}1"

        room_key = secrets.token_hex(16)
        room_data = {
            "room_id": room_id,
            "room_name": room_name,
            "game_name": game_name,
            "host": user_id,
            "host_username": username,
            "host_ip": host_ip,
            "players": [user_id],
            "player_usernames": [username],
            "player_ips": {user_id: host_ip},
            "subnet": subnet,
            "room_key": room_key,
            "has_password": bool(password),
            "max_players": max_players,
            "created_at": time.time(),
            "last_activity": time.time(),
        }
        rooms[room_id] = room_data
        user_rooms[user_id] = room_id

        await websocket.send_text(json.dumps({
            "type": "room_created",
            "room": {
                "room_id": room_id,
                "room_name": room_name,
                "game_name": game_name,
                "host": username,
                "subnet": subnet,
                "room_key": room_key,
                "max_players": max_players,
                "players": [username],
                "ip": host_ip,
                "has_password": bool(password),
            },
            "subnet": subnet,
            "room_key": room_key,
            "host_ip": host_ip,
        }))
        logger.info(f"🏠 Room {room_id} created by {username} ({subnet})")
        await broadcast_user_list()
        await broadcast_room_list()

    elif msg_type == "join_room":
        room_id = data.get("room_id")
        password = data.get("password", "")

        if not room_id or room_id not in rooms:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 404,
                "message": "Room not found",
            }))
            return

        room = rooms[room_id]
        if len(room["players"]) >= room["max_players"]:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 403,
                "message": "Room is full",
            }))
            return

        if room.get("has_password") and not password:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 403,
                "message": "Password required",
            }))
            return

        current = user_rooms.get(user_id)
        if current:
            await leave_room(user_id, current)

        subnet = room["subnet"]
        used_ips = set(room.get("player_ips", {}).values())
        ip_pools[subnet] = used_ips.union({f"{subnet}1"})

        player_ip = get_next_ip(subnet, used_ips)
        if not player_ip:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 500,
                "message": "No IP available in subnet",
            }))
            return

        room["players"].append(user_id)
        room["player_usernames"].append(username)
        room["player_ips"][user_id] = player_ip
        room["last_activity"] = time.time()
        user_rooms[user_id] = room_id

        await websocket.send_text(json.dumps({
            "type": "room_joined",
            "room": {
                "room_id": room_id,
                "room_name": room.get("room_name"),
                "game_name": room["game_name"],
                "host": room["host_username"],
                "subnet": subnet,
                "room_key": room["room_key"],
                "players": room["player_usernames"],
                "ips": room["player_ips"],
                "max_players": room["max_players"],
                "has_password": room.get("has_password", False),
            },
            "subnet": subnet,
            "room_key": room["room_key"],
            "my_ip": player_ip,
        }))
        logger.info(f"👤 {username} joined room {room_id} with IP {player_ip}")
        await broadcast_room_players(room_id)
        await broadcast_user_list()
        await broadcast_room_list()

    elif msg_type == "leave_room":
        room_id = user_rooms.get(user_id)
        if room_id:
            await leave_room(user_id, room_id)
            await websocket.send_text(json.dumps({
                "type": "left_room",
                "room_id": room_id,
            }))
            await broadcast_user_list()
            await broadcast_room_list()

    elif msg_type == "get_room_list":
        await send_room_list(websocket)

    elif msg_type == "offer":
        target = data.get("target")
        sdp = data.get("sdp")
        game_name = data.get("game_name", "")

        if not target:
            return
        target_id = None
        for uname, udata in users.items():
            if uname == target:
                target_id = udata.get("user_id")
                break
        if not target_id or target_id not in connections:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 404,
                "message": "Target user not online",
            }))
            return

        room_id = user_rooms.get(user_id)
        if not room_id:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 403,
                "message": "You must be in a room to send offers",
            }))
            return

        await connections[target_id].send_text(json.dumps({
            "type": "offer_received",
            "from": username,
            "from_id": user_id,
            "sdp": sdp,
            "game_name": game_name,
            "room_id": room_id,
        }))

    elif msg_type == "answer":
        target = data.get("target")
        sdp = data.get("sdp")

        if not target:
            return
        target_id = None
        for uname, udata in users.items():
            if uname == target:
                target_id = udata.get("user_id")
                break
        if not target_id or target_id not in connections:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 404,
                "message": "Target user not online",
            }))
            return

        await connections[target_id].send_text(json.dumps({
            "type": "answer_received",
            "from": username,
            "from_id": user_id,
            "sdp": sdp,
        }))

    elif msg_type == "ice_candidate":
        target = data.get("target")
        candidate = data.get("candidate")

        if not target:
            return
        target_id = None
        for uname, udata in users.items():
            if uname == target:
                target_id = udata.get("user_id")
                break
        if not target_id or target_id not in connections:
            return

        await connections[target_id].send_text(json.dumps({
            "type": "ice_candidate_received",
            "from": username,
            "from_id": user_id,
            "candidate": candidate,
        }))

# ============================================================
# Room Management
# ============================================================
async def leave_room(user_id: str, room_id: str):
    room = rooms.get(room_id)
    if not room:
        return

    if user_id in room["players"]:
        room["players"].remove(user_id)
        username = next((u for u, uid in users.items() if uid.get("user_id") == user_id), "")
        if username in room["player_usernames"]:
            room["player_usernames"].remove(username)
        room["player_ips"].pop(user_id, None)
        room["last_activity"] = time.time()

    user_rooms.pop(user_id, None)

    if not room["players"]:
        # حذف روم خالی
        subnet = room["subnet"]
        if subnet in ip_pools:
            del ip_pools[subnet]
        del rooms[room_id]
        logger.info(f"🗑️ Room {room_id} deleted (empty)")
    else:
        if room["host"] == user_id:
            room["host"] = room["players"][0]
            room["host_username"] = room["player_usernames"][0]
            room["host_ip"] = room["player_ips"][room["players"][0]]
            logger.info(f"👑 New host for {room_id}: {room['host_username']}")
        await broadcast_room_players(room_id)

async def broadcast_room_players(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return

    for uid in room["players"]:
        if uid in connections:
            try:
                await connections[uid].send_text(json.dumps({
                    "type": "room_players",
                    "room_id": room_id,
                    "players": room["player_usernames"],
                    "ips": room["player_ips"],
                    "host": room["host_username"],
                    "subnet": room["subnet"],
                    "room_key": room["room_key"],
                }))
            except:
                pass

async def broadcast_user_list():
    user_list = []
    for username, user in users.items():
        if user.get("user_id") in connections:
            user_list.append(username)

    for uid, ws in connections.items():
        try:
            await ws.send_text(json.dumps({
                "type": "user_list",
                "users": user_list,
            }))
        except:
            pass

async def broadcast_room_list():
    room_list = []
    for room_id, room in rooms.items():
        room_list.append({
            "room_id": room_id,
            "room_name": room.get("room_name", room["game_name"]),
            "game": room["game_name"],
            "host": room["host_username"],
            "count": len(room["players"]),
            "max": room["max_players"],
            "has_password": room.get("has_password", False),
        })

    for uid, ws in connections.items():
        try:
            await ws.send_text(json.dumps({
                "type": "room_list",
                "rooms": room_list,
            }))
        except:
            pass

async def send_user_list(websocket: WebSocket):
    user_list = []
    for username, user in users.items():
        if user.get("user_id") in connections:
            user_list.append(username)
    await websocket.send_text(json.dumps({
        "type": "user_list",
        "users": user_list,
    }))

async def send_room_list(websocket: WebSocket):
    room_list = []
    for room_id, room in rooms.items():
        room_list.append({
            "room_id": room_id,
            "room_name": room.get("room_name", room["game_name"]),
            "game": room["game_name"],
            "host": room["host_username"],
            "count": len(room["players"]),
            "max": room["max_players"],
            "has_password": room.get("has_password", False),
        })
    await websocket.send_text(json.dumps({
        "type": "room_list",
        "rooms": room_list,
    }))

# ============================================================
# Health Check
# ============================================================
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": VERSION,
        "users": len(users),
        "rooms": len(rooms),
        "connections": len(connections),
    }

@app.get("/version")
async def version():
    return {"version": VERSION}

# ============================================================
# Run
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        workers=1,  # برای WebSocket حتماً ۱ worker
    )

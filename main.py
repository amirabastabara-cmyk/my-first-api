#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
VoidVision Server v2.0 - FastAPI + WebSocket + Room + Cloud + Matchmaking
برای اجرا روی VPS
"""

import asyncio
import json
import os
import uuid
import time
import secrets
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import logging

# ============================================================
# CONFIGURATION
# ============================================================
VERSION = "2.0.0"
MAX_ROOMS_PER_USER = 5
MAX_PLAYERS_PER_ROOM = 16
MAX_USERNAME_LENGTH = 32
SECRET_KEY = os.environ.get("VOIDVISION_SECRET", secrets.token_hex(32))
TURN_HOST = os.environ.get("TURN_HOST", "your-vps-ip")
TURN_USERNAME = os.environ.get("TURN_USERNAME", "voidvision")
TURN_PASSWORD = os.environ.get("TURN_PASSWORD", secrets.token_hex(16))

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | %(message)s"
)
logger = logging.getLogger("voidvision-server")

# ============================================================
# MODELS
# ============================================================
class CloudSaveData(BaseModel):
    settings: dict
    game_profiles: dict
    friends: list

# ============================================================
# FastAPI APP
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"🚀 VoidVision Server v{VERSION} starting...")
    app.state.rooms: Dict[str, dict] = {}
    app.state.users: Dict[str, dict] = {}
    app.state.connections: Dict[str, WebSocket] = {}
    app.state.user_rooms: Dict[str, str] = {}
    app.state.ip_pools: Dict[str, Set[str]] = {}
    app.state.cloud_saves: Dict[str, dict] = {}
    app.state.tokens: Dict[str, dict] = {}
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
# HELPER FUNCTIONS
# ============================================================
def generate_room_id() -> str:
    return secrets.token_hex(4).upper()

def generate_subnet() -> str:
    x = secrets.randbelow(254) + 1
    return f"10.77.{x}."

def get_next_ip(subnet: str, used_ips: Set[str]) -> Optional[str]:
    for i in range(2, 255):
        ip = f"{subnet}{i}"
        if ip not in used_ips:
            return ip
    return None

def create_token(user_id: str, username: str) -> str:
    payload = f"{user_id}:{username}:{int(time.time())}:{secrets.token_hex(8)}"
    signature = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"

def verify_token(token: str) -> Optional[tuple]:
    try:
        parts = token.split(":")
        if len(parts) != 5:
            return None
        user_id, username, ts, nonce, signature = parts
        payload = f"{user_id}:{username}:{ts}:{nonce}"
        expected = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        if int(ts) < time.time() - 86400:
            return None
        return user_id, username
    except:
        return None

def get_turn_servers() -> List[dict]:
    return [
        {
            "urls": f"turn:{TURN_HOST}:3478",
            "username": TURN_USERNAME,
            "credential": TURN_PASSWORD,
        },
        {
            "urls": f"turn:{TURN_HOST}:3478?transport=tcp",
            "username": TURN_USERNAME,
            "credential": TURN_PASSWORD,
        },
        {"urls": "stun:stun.cloudflare.com:3478"},
        {"urls": "stun:stun.l.google.com:19302"},
    ]

# ============================================================
# WEBSOCKET ENDPOINT
# ============================================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    user_id = None
    username = None

    try:
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
            if len(username_val) > MAX_USERNAME_LENGTH:
                await websocket.send_text(json.dumps({
                    "type": "register_response",
                    "success": False,
                    "message": f"Username too long (max {MAX_USERNAME_LENGTH})"
                }))
                await websocket.close()
                return
            if username_val in app.state.users:
                await websocket.send_text(json.dumps({
                    "type": "register_response",
                    "success": False,
                    "message": "Username already taken"
                }))
                await websocket.close()
                return
            user_id = str(uuid.uuid4())
            app.state.users[username_val] = {
                "user_id": user_id,
                "password": password_val,
                "created_at": time.time()
            }
            await websocket.send_text(json.dumps({
                "type": "register_response",
                "success": True,
                "message": "Registration successful"
            }))
            await websocket.close()
            return

        # ---------- LOGIN ----------
        if msg_type == "login":
            username_val = data.get("username", "").strip()
            password_val = data.get("password", "")
            if not username_val or not password_val:
                await websocket.send_text(json.dumps({
                    "type": "login_response",
                    "success": False,
                    "message": "Missing credentials"
                }))
                await websocket.close()
                return
            user_data = app.state.users.get(username_val)
            if not user_data:
                await websocket.send_text(json.dumps({
                    "type": "login_response",
                    "success": False,
                    "message": "User not found"
                }))
                await websocket.close()
                return
            if user_data["password"] != password_val:
                await websocket.send_text(json.dumps({
                    "type": "login_response",
                    "success": False,
                    "message": "Invalid password"
                }))
                await websocket.close()
                return
            user_id = user_data["user_id"]
            username = username_val
            token = create_token(user_id, username)

            app.state.connections[user_id] = websocket
            app.state.users[username_val]["connected_at"] = time.time()
            app.state.tokens[user_id] = {"token": token, "created": time.time()}

            await websocket.send_text(json.dumps({
                "type": "login_response",
                "success": True,
                "username": username,
                "user_id": user_id,
                "token": token,
                "turn_servers": get_turn_servers(),
                "version": VERSION,
            }))

            logger.info(f"✅ User {username} ({user_id}) logged in")
            await broadcast_user_list()

            # ---------- MAIN MESSAGE LOOP ----------
            while True:
                try:
                    raw = await websocket.receive_text()
                    data = json.loads(raw)
                    await handle_message(user_id, username, data, websocket)
                except json.JSONDecodeError:
                    await websocket.send_text(json.dumps({"type": "error", "code": 400, "message": "Invalid JSON"}))
                except WebSocketDisconnect:
                    break
                except Exception as e:
                    logger.error(f"❌ Message error: {e}")
                    await websocket.send_text(json.dumps({"type": "error", "code": 500, "message": str(e)}))

            await cleanup_user(user_id, username)
            return

        # ---------- AUTH (Reconnect) ----------
        if msg_type == "auth":
            token = data.get("token", "")
            if not token:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "code": 401,
                    "message": "Token required"
                }))
                await websocket.close()
                return
            user_info = verify_token(token)
            if not user_info:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "code": 401,
                    "message": "Invalid token"
                }))
                await websocket.close()
                return
            user_id, username = user_info

            app.state.connections[user_id] = websocket
            app.state.users[username]["connected_at"] = time.time()

            await websocket.send_text(json.dumps({
                "type": "auth_success",
                "token": token,
                "username": username,
                "user_id": user_id,
                "turn_servers": get_turn_servers(),
                "version": VERSION,
            }))

            logger.info(f"✅ User {username} ({user_id}) reconnected via token")
            await broadcast_user_list()

            while True:
                try:
                    raw = await websocket.receive_text()
                    data = json.loads(raw)
                    await handle_message(user_id, username, data, websocket)
                except json.JSONDecodeError:
                    await websocket.send_text(json.dumps({"type": "error", "code": 400, "message": "Invalid JSON"}))
                except WebSocketDisconnect:
                    break
                except Exception as e:
                    logger.error(f"❌ Message error: {e}")
                    await websocket.send_text(json.dumps({"type": "error", "code": 500, "message": str(e)}))

            await cleanup_user(user_id, username)
            return

        else:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 400,
                "message": "First message must be 'login', 'register', or 'auth'"
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
# CLEANUP
# ============================================================
async def cleanup_user(user_id: str, username: str):
    room_id = app.state.user_rooms.get(user_id)
    if room_id:
        await leave_room(user_id, room_id)
    app.state.connections.pop(user_id, None)
    app.state.user_rooms.pop(user_id, None)
    app.state.tokens.pop(user_id, None)
    await broadcast_user_list()

# ============================================================
# MESSAGE HANDLER
# ============================================================
async def handle_message(user_id: str, username: str, data: dict, websocket: WebSocket):
    msg_type = data.get("type")

    # ---------- PING ----------
    if msg_type == "ping":
        await websocket.send_text(json.dumps({"type": "pong", "time": time.time()}))
        return

    # ---------- USER LIST ----------
    elif msg_type == "get_user_list":
        await send_user_list(websocket)

    # ---------- CHAT ----------
    elif msg_type == "chat_message":
        message = data.get("message", "").strip()
        if not message or len(message) > 500:
            return
        room_id = app.state.user_rooms.get(user_id)
        if room_id:
            room = app.state.rooms.get(room_id)
            if room:
                for uid in room.get("players", []):
                    if uid in app.state.connections:
                        try:
                            await app.state.connections[uid].send_text(json.dumps({
                                "type": "chat_message",
                                "sender": username,
                                "message": message,
                                "room_id": room_id,
                            }))
                        except:
                            pass
        else:
            await websocket.send_text(json.dumps({
                "type": "chat_message",
                "sender": "system",
                "message": "Join a room to chat",
            }))

    # ---------- CREATE ROOM ----------
    elif msg_type == "create_room":
        game_name = data.get("game_name", "Unknown Game")
        max_players = min(data.get("max_players", 8), MAX_PLAYERS_PER_ROOM)
        password = data.get("password", "")
        room_name = data.get("room_name", game_name)

        user_rooms = [r for r in app.state.rooms.values() if user_id in r.get("players", [])]
        if len(user_rooms) >= MAX_ROOMS_PER_USER:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 429,
                "message": f"Maximum {MAX_ROOMS_PER_USER} rooms per user",
            }))
            return

        room_id = generate_room_id()
        while room_id in app.state.rooms:
            room_id = generate_room_id()

        subnet = generate_subnet()
        while subnet in app.state.ip_pools:
            subnet = generate_subnet()

        app.state.ip_pools[subnet] = {f"{subnet}1"}
        host_ip = f"{subnet}1"

        room_key = secrets.token_hex(16)
        room_data = {
            "room_id": room_id,
            "game_name": game_name,
            "room_name": room_name,
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
        if password:
            room_data["password"] = password
        app.state.rooms[room_id] = room_data
        app.state.user_rooms[user_id] = room_id

        await websocket.send_text(json.dumps({
            "type": "room_created",
            "room": {
                "room_id": room_id,
                "game_name": game_name,
                "room_name": room_name,
                "host": username,
                "subnet": subnet,
                "room_key": room_key,
                "has_password": bool(password),
                "max_players": max_players,
                "players": [username],
                "ip": host_ip,
                "turn_servers": get_turn_servers(),
            },
            "subnet": subnet,
            "room_key": room_key,
            "host_ip": host_ip,
        }))
        logger.info(f"🏠 Room {room_id} created by {username} ({subnet})")
        await broadcast_user_list()
        await broadcast_room_list()

    # ---------- JOIN ROOM ----------
    elif msg_type == "join_room":
        room_id = data.get("room_id")
        password = data.get("password", "")
        if not room_id or room_id not in app.state.rooms:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 404,
                "message": "Room not found",
            }))
            return

        room = app.state.rooms[room_id]
        if len(room["players"]) >= room["max_players"]:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 403,
                "message": "Room is full",
            }))
            return

        if room.get("has_password"):
            if not password or room.get("password") != password:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "code": 403,
                    "message": "Invalid password",
                }))
                return

        current = app.state.user_rooms.get(user_id)
        if current:
            await leave_room(user_id, current)

        subnet = room["subnet"]
        used_ips = set(room.get("player_ips", {}).values())
        app.state.ip_pools[subnet] = used_ips.union({f"{subnet}1"})

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
        app.state.user_rooms[user_id] = room_id

        await websocket.send_text(json.dumps({
            "type": "room_joined",
            "room": {
                "room_id": room_id,
                "game_name": room["game_name"],
                "room_name": room["room_name"],
                "host": room["host_username"],
                "subnet": subnet,
                "room_key": room["room_key"],
                "has_password": room.get("has_password", False),
                "players": room["player_usernames"],
                "ips": room["player_ips"],
                "max_players": room["max_players"],
                "turn_servers": get_turn_servers(),
            },
            "subnet": subnet,
            "room_key": room["room_key"],
            "my_ip": player_ip,
        }))
        logger.info(f"👤 {username} joined room {room_id} with IP {player_ip}")
        await broadcast_room_players(room_id)
        await broadcast_user_list()
        await broadcast_room_list()

    # ---------- LEAVE ROOM ----------
    elif msg_type == "leave_room":
        room_id = app.state.user_rooms.get(user_id)
        if room_id:
            await leave_room(user_id, room_id)
            await websocket.send_text(json.dumps({
                "type": "left_room",
                "room_id": room_id,
            }))
            await broadcast_user_list()
            await broadcast_room_list()

    # ---------- ROOM LIST ----------
    elif msg_type == "get_room_list":
        await send_room_list(websocket)

    # ---------- OFFER ----------
    elif msg_type == "offer":
        target = data.get("target")
        sdp = data.get("sdp")
        game_name = data.get("game_name", "")

        if not target or target not in app.state.connections:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 404,
                "message": "Target user not online",
            }))
            return

        room_id = app.state.user_rooms.get(user_id)
        if not room_id:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 403,
                "message": "You must be in a room to send offers",
            }))
            return

        await app.state.connections[target].send_text(json.dumps({
            "type": "offer_received",
            "from": username,
            "from_id": user_id,
            "sdp": sdp,
            "game_name": game_name,
            "room_id": room_id,
        }))

    # ---------- ANSWER ----------
    elif msg_type == "answer":
        target = data.get("target")
        sdp = data.get("sdp")

        if not target or target not in app.state.connections:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 404,
                "message": "Target user not online",
            }))
            return

        await app.state.connections[target].send_text(json.dumps({
            "type": "answer_received",
            "from": username,
            "from_id": user_id,
            "sdp": sdp,
        }))

    # ---------- ICE CANDIDATE ----------
    elif msg_type == "ice_candidate":
        target = data.get("target")
        candidate = data.get("candidate")

        if not target or target not in app.state.connections:
            return

        await app.state.connections[target].send_text(json.dumps({
            "type": "ice_candidate_received",
            "from": username,
            "from_id": user_id,
            "candidate": candidate,
        }))

    # ---------- FRIEND REQUEST ----------
    elif msg_type == "friend_request":
        target = data.get("target")
        if target and target in app.state.connections:
            await app.state.connections[target].send_text(json.dumps({
                "type": "friend_request",
                "from": username,
                "from_id": user_id,
                "message": f"Friend request from {username}"
            }))

    # ---------- FRIEND ACCEPT ----------
    elif msg_type == "friend_accept":
        target = data.get("target")
        if target and target in app.state.connections:
            await app.state.connections[target].send_text(json.dumps({
                "type": "friend_accepted",
                "from": username,
                "from_id": user_id,
            }))

    # ---------- FRIEND REJECT ----------
    elif msg_type == "friend_reject":
        target = data.get("target")
        if target and target in app.state.connections:
            await app.state.connections[target].send_text(json.dumps({
                "type": "friend_rejected",
                "from": username,
            }))

    # ---------- GAME INVITE ----------
    elif msg_type == "game_invite":
        target = data.get("target")
        game_name = data.get("game_name", "")
        room_id = data.get("room_id", "")

        if target and target in app.state.connections:
            await app.state.connections[target].send_text(json.dumps({
                "type": "game_invite",
                "from": username,
                "game_name": game_name,
                "room_id": room_id,
            }))

    # ---------- CLOUD SAVE ----------
    elif msg_type == "cloud_save":
        save_data = data.get("data", {})
        app.state.cloud_saves[user_id] = {
            "data": save_data,
            "updated_at": time.time()
        }
        await websocket.send_text(json.dumps({
            "type": "cloud_save_response",
            "success": True,
        }))
        logger.info(f"☁️ Cloud save for {username}")

    # ---------- CLOUD LOAD ----------
    elif msg_type == "cloud_load":
        save_data = app.state.cloud_saves.get(user_id, {}).get("data", {})
        await websocket.send_text(json.dumps({
            "type": "cloud_load_response",
            "data": save_data,
        }))

    # ---------- MATCHMAKING START ----------
    elif msg_type == "matchmaking_start":
        game_name = data.get("game_name", "")
        if not game_name:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 400,
                "message": "Game name required",
            }))
            return

        # پیدا کردن روم موجود با بازی مورد نظر
        found_room = None
        for room_id, room in app.state.rooms.items():
            if room["game_name"] == game_name and len(room["players"]) < room["max_players"]:
                found_room = room
                break

        if found_room:
            room_id = found_room["room_id"]
            await websocket.send_text(json.dumps({
                "type": "match_found",
                "room_id": room_id,
                "players": found_room["player_usernames"],
            }))
        else:
            await websocket.send_text(json.dumps({
                "type": "matchmaking_status",
                "status": "searching",
                "message": "No room found, try creating one",
            }))

    # ---------- MATCHMAKING STOP ----------
    elif msg_type == "matchmaking_stop":
        await websocket.send_text(json.dumps({
            "type": "matchmaking_stopped",
        }))

    # ---------- ROOM CLOSED (when host leaves) ----------
    elif msg_type == "room_closed":
        room_id = app.state.user_rooms.get(user_id)
        if room_id:
            await broadcast_room_closed(room_id)
            await leave_room(user_id, room_id)

# ============================================================
# ROOM MANAGEMENT
# ============================================================
async def leave_room(user_id: str, room_id: str):
    room = app.state.rooms.get(room_id)
    if not room:
        return

    if user_id in room["players"]:
        room["players"].remove(user_id)
        username = next((u for u, uid in app.state.users.items() if uid.get("user_id") == user_id), "")
        if username in room["player_usernames"]:
            room["player_usernames"].remove(username)
        room["player_ips"].pop(user_id, None)
        room["last_activity"] = time.time()

    app.state.user_rooms.pop(user_id, None)

    if not room["players"]:
        del app.state.rooms[room_id]
        subnet = room["subnet"]
        app.state.ip_pools.pop(subnet, None)
        logger.info(f"🗑️ Room {room_id} deleted (empty)")
    else:
        if room["host"] == user_id:
            room["host"] = room["players"][0]
            room["host_username"] = room["player_usernames"][0]
            room["host_ip"] = room["player_ips"][room["players"][0]]
            logger.info(f"👑 New host for {room_id}: {room['host_username']}")
            await broadcast_room_players(room_id)

async def broadcast_room_players(room_id: str):
    room = app.state.rooms.get(room_id)
    if not room:
        return

    for uid in room["players"]:
        if uid in app.state.connections:
            try:
                await app.state.connections[uid].send_text(json.dumps({
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

async def broadcast_room_closed(room_id: str):
    room = app.state.rooms.get(room_id)
    if not room:
        return

    for uid in room["players"]:
        if uid in app.state.connections:
            try:
                await app.state.connections[uid].send_text(json.dumps({
                    "type": "room_closed",
                    "room_id": room_id,
                }))
            except:
                pass

async def broadcast_user_list():
    user_list = []
    for username, user in app.state.users.items():
        if user.get("user_id") in app.state.connections:
            user_list.append(username)

    for uid, ws in app.state.connections.items():
        try:
            await ws.send_text(json.dumps({
                "type": "user_list",
                "users": user_list,
            }))
        except:
            pass

async def broadcast_room_list():
    room_list = []
    for room_id, room in app.state.rooms.items():
        room_list.append({
            "room_id": room_id,
            "game": room["game_name"],
            "room_name": room.get("room_name", "Unknown"),
            "host": room["host_username"],
            "count": len(room["players"]),
            "max": room["max_players"],
            "has_password": room.get("has_password", False),
        })

    for uid, ws in app.state.connections.items():
        try:
            await ws.send_text(json.dumps({
                "type": "room_list",
                "rooms": room_list,
            }))
        except:
            pass

async def send_user_list(websocket: WebSocket):
    user_list = []
    for username, user in app.state.users.items():
        if user.get("user_id") in app.state.connections:
            user_list.append(username)
    await websocket.send_text(json.dumps({
        "type": "user_list",
        "users": user_list,
    }))

async def send_room_list(websocket: WebSocket):
    room_list = []
    for room_id, room in app.state.rooms.items():
        room_list.append({
            "room_id": room_id,
            "game": room["game_name"],
            "room_name": room.get("room_name", "Unknown"),
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
# HEALTH CHECK
# ============================================================
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": VERSION,
        "users": len(app.state.users),
        "rooms": len(app.state.rooms),
        "connections": len(app.state.connections),
    }

@app.get("/version")
async def version():
    return {"version": VERSION}

# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        workers=1,
    )

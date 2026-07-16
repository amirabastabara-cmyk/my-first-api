#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
VoidVision Server v1.0 - FastAPI + WebSocket Signaling
Run this on your VPS or Render
"""

import asyncio
import json
import os
import uuid
import time
import secrets
from typing import Dict, List, Optional, Set
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import logging

# ============================================================
# CONFIGURATION
# ============================================================
VERSION = "1.0.0"
MAX_ROOMS_PER_USER = 5
MAX_PLAYERS_PER_ROOM = 16
MAX_USERNAME_LENGTH = 32

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | %(message)s"
)
logger = logging.getLogger("voidvision-server")

# ============================================================
# FastAPI App
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"🚀 VoidVision Server v{VERSION} starting...")
    app.state.rooms: Dict[str, dict] = {}
    app.state.users: Dict[str, dict] = {}
    app.state.connections: Dict[str, WebSocket] = {}
    app.state.user_rooms: Dict[str, str] = {}
    app.state.ip_pools: Dict[str, Set[str]] = {}
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
# Helper Functions
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

# ============================================================
# WebSocket Endpoint
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

            app.state.connections[user_id] = websocket
            app.state.users[username_val]["connected_at"] = time.time()

            await websocket.send_text(json.dumps({
                "type": "login_response",
                "success": True,
                "username": username,
                "user_id": user_id,
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

        else:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": 400,
                "message": "First message must be 'login' or 'register'"
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
    room_id = app.state.user_rooms.get(user_id)
    if room_id:
        await leave_room(user_id, room_id)
    app.state.connections.pop(user_id, None)
    app.state.user_rooms.pop(user_id, None)
    await broadcast_user_list()

# ============================================================
# Message Handler
# ============================================================
async def handle_message(user_id: str, username: str, data: dict, websocket: WebSocket):
    msg_type = data.get("type")

    if msg_type == "ping":
        await websocket.send_text(json.dumps({"type": "pong"}))
        return

    elif msg_type == "get_user_list":
        await send_user_list(websocket)

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

    elif msg_type == "create_room":
        game_name = data.get("game_name", "Unknown Game")
        max_players = min(data.get("max_players", 8), MAX_PLAYERS_PER_ROOM)

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
            "host": user_id,
            "host_username": username,
            "host_ip": host_ip,
            "players": [user_id],
            "player_usernames": [username],
            "player_ips": {user_id: host_ip},
            "subnet": subnet,
            "room_key": room_key,
            "max_players": max_players,
            "created_at": time.time(),
            "last_activity": time.time(),
        }
        app.state.rooms[room_id] = room_data
        app.state.user_rooms[user_id] = room_id

        await websocket.send_text(json.dumps({
            "type": "room_created",
            "room": {
                "room_id": room_id,
                "game_name": game_name,
                "host": username,
                "subnet": subnet,
                "room_key": room_key,
                "max_players": max_players,
                "players": [username],
                "ip": host_ip,
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
                "host": room["host_username"],
                "subnet": subnet,
                "room_key": room["room_key"],
                "players": room["player_usernames"],
                "ips": room["player_ips"],
                "max_players": room["max_players"],
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
        room_id = app.state.user_rooms.get(user_id)
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

# ============================================================
# Room Management
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
            "host": room["host_username"],
            "count": len(room["players"]),
            "max": room["max_players"],
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
            "host": room["host_username"],
            "count": len(room["players"]),
            "max": room["max_players"],
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
        "users": len(app.state.users),
        "rooms": len(app.state.rooms),
        "connections": len(app.state.connections),
    }

# ============================================================
# Run
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        workers=1,
    )

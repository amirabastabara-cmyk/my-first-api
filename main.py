#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
VoidVision Server v26.0.0 - Lightweight for VPS 512MB
"""

import json
import uuid
import bcrypt
import jwt
import os
import time
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="VoidVision Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================== Config ==================
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret-in-production")
JWT_ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days
BCRYPT_ROUNDS = 10  # کاهش برای VPS ضعیف

# ================== Models ==================
class UserRegister(BaseModel):
    username: str
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

# ================== Database (in-memory) ==================
users_db = {}          # username -> {"password_hash": str, "user_id": str}
online_users = {}      # username -> websocket
rooms = {}             # room_id -> dict
friend_requests = {}   # username -> [from_user]
friends = {}           # username -> [friend_username]

# ================== REST API ==================
@app.post("/api/register")
async def register(user: UserRegister):
    username = user.username.strip()
    password = user.password.strip()
    if not username or len(password) < 6:
        return {"success": False, "message": "Invalid credentials"}
    if username in users_db:
        return {"success": False, "message": "Username exists"}
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()
    user_id = str(uuid.uuid4())
    users_db[username] = {"password_hash": password_hash, "user_id": user_id}
    friends[username] = []
    friend_requests[username] = []
    return {"success": True, "message": "Registered"}

@app.post("/api/login")
async def login(user: UserLogin):
    username = user.username.strip()
    password = user.password.strip()
    if username not in users_db:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    stored = users_db[username]
    if not bcrypt.checkpw(password.encode(), stored["password_hash"].encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    payload = {
        "sub": username,
        "user_id": stored["user_id"],
        "exp": datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {"access_token": token, "token_type": "bearer", "username": username, "user_id": stored["user_id"]}

# ================== WebSocket Signaling ==================
class ConnectionManager:
    def __init__(self):
        self.active_connections = []
        self.username_map = {}

    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        self.active_connections.append(websocket)
        self.username_map[websocket] = username
        online_users[username] = websocket

    def disconnect(self, websocket: WebSocket):
        username = self.username_map.pop(websocket, None)
        if username:
            online_users.pop(username, None)
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def send_to_user(self, username: str, data: dict):
        ws = online_users.get(username)
        if ws:
            try:
                await ws.send_json(data)
            except:
                pass

    async def broadcast(self, data: dict, exclude=None):
        for conn in self.active_connections[:]:
            if conn != exclude:
                try:
                    await conn.send_json(data)
                except:
                    pass

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    current_user = None
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=60)
            except asyncio.TimeoutError:
                # Heartbeat timeout
                if current_user:
                    await manager.send_to_user(current_user, {"type": "ping"})
                continue
            except Exception:
                break

            try:
                msg = json.loads(raw)
                t = msg.get("type")

                # ---------- AUTH ----------
                if t == "auth":
                    token = msg.get("token")
                    if not token:
                        await websocket.send_json({"type": "auth_response", "success": False, "message": "No token"})
                        continue
                    try:
                        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
                        username = payload["sub"]
                        user_id = payload["user_id"]
                        if username not in users_db or users_db[username]["user_id"] != user_id:
                            raise Exception("Invalid user")
                        if username in online_users:
                            old = online_users[username]
                            if old != websocket:
                                try:
                                    await old.close(code=1000, reason="Duplicate login")
                                except:
                                    pass
                        await manager.connect(websocket, username)
                        current_user = username
                        await websocket.send_json({
                            "type": "auth_response",
                            "success": True,
                            "username": username,
                            "user_id": user_id
                        })
                        await manager.broadcast({"type": "user_list", "users": list(online_users.keys())})
                        await websocket.send_json({
                            "type": "friend_requests_list",
                            "requests": friend_requests.get(username, [])
                        })
                        await websocket.send_json({
                            "type": "friends_list",
                            "friends": friends.get(username, [])
                        })
                    except Exception as e:
                        await websocket.send_json({"type": "auth_response", "success": False, "message": str(e)})
                    continue

                if not current_user:
                    await websocket.send_json({"type": "error", "message": "Not authenticated"})
                    continue

                # ---------- ROOM ----------
                if t == "get_room_list":
                    room_list = []
                    for rid, r in rooms.items():
                        room_list.append({
                            "room_id": rid,
                            "room_name": r["name"][:30],
                            "has_password": bool(r.get("password")),
                            "count": len(r["players"]),
                            "max": r["max_players"]
                        })
                    await websocket.send_json({"type": "room_list", "rooms": room_list})

                elif t == "create_room":
                    room_id = str(uuid.uuid4())[:6]
                    rooms[room_id] = {
                        "name": msg.get("room_name", "Room")[:30],
                        "password": msg.get("password", ""),
                        "max_players": min(msg.get("max_players", 6), 8),
                        "host": current_user,
                        "players": [current_user],
                        "room_key": str(uuid.uuid4()),
                        "next_ip": 2,
                        "created": time.time()
                    }
                    ips = {current_user: "10.77.0.1"}
                    await websocket.send_json({
                        "type": "room_created",
                        "room": {
                            "room_id": room_id,
                            "room_name": rooms[room_id]["name"],
                            "room_key": rooms[room_id]["room_key"],
                            "subnet": "10.77.0."
                        }
                    })
                    # Broadcast updated room list
                    await manager.broadcast({"type": "room_list", "rooms": []})

                elif t == "join_room":
                    room_id = msg.get("room_id")
                    pwd = msg.get("password", "")
                    if room_id not in rooms:
                        await websocket.send_json({"type": "room_joined", "success": False, "message": "Not found"})
                        continue
                    room = rooms[room_id]
                    if room.get("password") and room["password"] != pwd:
                        await websocket.send_json({"type": "room_joined", "success": False, "message": "Wrong password"})
                        continue
                    if len(room["players"]) >= room["max_players"]:
                        await websocket.send_json({"type": "room_joined", "success": False, "message": "Full"})
                        continue
                    if current_user not in room["players"]:
                        room["players"].append(current_user)
                        ip_idx = room.get("next_ip", len(room["players"]) + 1)
                        room["next_ip"] = ip_idx + 1
                    ips = {p: f"10.77.0.{i+1}" for i, p in enumerate(room["players"])}
                    await websocket.send_json({
                        "type": "room_joined",
                        "room": {
                            "room_id": room_id,
                            "room_name": room["name"],
                            "room_key": room["room_key"],
                            "subnet": "10.77.0."
                        }
                    })
                    await manager.broadcast({
                        "type": "room_players",
                        "players": room["players"],
                        "ips": ips,
                        "host": room["host"],
                        "subnet": "10.77.0.",
                        "room_key": room["room_key"]
                    })

                elif t == "leave_room":
                    for rid, room in list(rooms.items()):
                        if current_user in room["players"]:
                            room["players"].remove(current_user)
                            if room["host"] == current_user or not room["players"]:
                                del rooms[rid]
                                await manager.broadcast({"type": "room_closed", "room_id": rid})
                            else:
                                ips = {p: f"10.77.0.{i+1}" for i, p in enumerate(room["players"])}
                                await manager.broadcast({
                                    "type": "room_players",
                                    "players": room["players"],
                                    "ips": ips,
                                    "host": room["host"],
                                    "subnet": "10.77.0.",
                                    "room_key": room["room_key"]
                                })
                            break
                    await websocket.send_json({"type": "left_room", "success": True})

                # ---------- SIGNALING ----------
                elif t == "offer":
                    target = msg.get("target")
                    sdp = msg.get("sdp")
                    game = msg.get("game_name", "")
                    public_key = msg.get("public_key", "")
                    if target and sdp:
                        await manager.send_to_user(target, {
                            "type": "offer_received",
                            "from": current_user,
                            "sdp": sdp,
                            "game_name": game,
                            "public_key": public_key
                        })

                elif t == "answer":
                    target = msg.get("target")
                    sdp = msg.get("sdp")
                    if target and sdp:
                        await manager.send_to_user(target, {
                            "type": "answer_received",
                            "from": current_user,
                            "sdp": sdp
                        })

                elif t == "ice_candidate":
                    target = msg.get("target")
                    cand = msg.get("candidate")
                    if target and cand:
                        await manager.send_to_user(target, {
                            "type": "ice_candidate_received",
                            "from": current_user,
                            "candidate": cand
                        })

                # ---------- CHAT ----------
                elif t == "chat_message":
                    text = msg.get("message", "")[:500]
                    if current_user and text:
                        await manager.broadcast({
                            "type": "chat_message",
                            "sender": current_user,
                            "message": text
                        })

                # ---------- FRIENDS ----------
                elif t == "friend_request":
                    target = msg.get("target")
                    if target and target in users_db and target != current_user:
                        if target not in friend_requests.get(target, []):
                            friend_requests.setdefault(target, []).append(current_user)
                            await manager.send_to_user(target, {
                                "type": "friend_request_received",
                                "from": current_user
                            })

                elif t == "friend_accept":
                    target = msg.get("target")
                    if target:
                        if target in friend_requests.get(current_user, []):
                            friend_requests[current_user].remove(target)
                        if current_user not in friends.get(target, []):
                            friends.setdefault(target, []).append(current_user)
                        if target not in friends.get(current_user, []):
                            friends.setdefault(current_user, []).append(target)
                        await manager.send_to_user(target, {
                            "type": "friend_accepted",
                            "from": current_user
                        })
                        await websocket.send_json({
                            "type": "friends_list",
                            "friends": friends.get(current_user, [])
                        })
                        await manager.send_to_user(target, {
                            "type": "friends_list",
                            "friends": friends.get(target, [])
                        })

                elif t == "friend_reject":
                    target = msg.get("target")
                    if target and target in friend_requests.get(current_user, []):
                        friend_requests[current_user].remove(target)

                # ---------- READY & LAUNCH ----------
                elif t == "ready_to_launch":
                    await manager.broadcast({
                        "type": "player_ready",
                        "player": current_user,
                        "room": msg.get("room_id")
                    })

                elif t == "launch_game":
                    game = msg.get("game_name")
                    room_id = msg.get("room_id")
                    room = rooms.get(room_id)
                    if room and room["host"] == current_user:
                        await manager.broadcast({
                            "type": "launch_game_command",
                            "game": game,
                            "room_id": room_id
                        })

                else:
                    await websocket.send_json({"type": "error", "message": f"Unknown: {t}"})

            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
            except Exception as e:
                await websocket.send_json({"type": "error", "message": str(e)})

    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)
        if current_user:
            await manager.broadcast({"type": "user_list", "users": list(online_users.keys())})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")

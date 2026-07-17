# main.py - با aiohttp (بدون uvicorn/fastapi)
import os
import json
import uuid
import bcrypt
import jwt
import hashlib
from datetime import datetime, timedelta
from aiohttp import web, WSMsgType
import aiohttp_cors

app = web.Application()

# CORS
cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(
        allow_credentials=True,
        expose_headers="*",
        allow_methods="*",
        allow_headers="*"
    )
})

# ================== Config ==================
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret-on-render")
JWT_ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * 24 * 7
BCRYPT_ROUNDS = 12

# ================== Database ==================
users_db = {}
online_users = {}
rooms = {}
friends = {}
friend_requests = {}

# ================== Helper ==================
def create_token(username: str, user_id: str) -> str:
    payload = {
        "sub": username,
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except:
        return None

# ================== REST API ==================
async def register(request):
    try:
        data = await request.json()
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()
        if not username or len(password) < 4:
            return web.json_response({"success": False, "message": "Invalid"})
        if username in users_db:
            return web.json_response({"success": False, "message": "Username exists"})
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()
        user_id = str(uuid.uuid4())
        users_db[username] = {"password_hash": password_hash, "user_id": user_id}
        friends[username] = []
        friend_requests[username] = []
        return web.json_response({"success": True, "message": "Registered"})
    except:
        return web.json_response({"success": False, "message": "Error"})

async def login(request):
    try:
        data = await request.json()
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()
        if username not in users_db:
            return web.json_response({"error": "Invalid credentials"}, status=401)
        stored = users_db[username]
        if not bcrypt.checkpw(password.encode(), stored["password_hash"].encode()):
            return web.json_response({"error": "Invalid credentials"}, status=401)
        token = create_token(username, stored["user_id"])
        return web.json_response({
            "access_token": token,
            "token_type": "bearer",
            "username": username,
            "user_id": stored["user_id"]
        })
    except:
        return web.json_response({"error": "Error"}, status=500)

# ================== WebSocket ==================
class ConnectionManager:
    def __init__(self):
        self.active_connections = []
        self.username_map = {}

    async def connect(self, ws, username: str):
        self.active_connections.append(ws)
        self.username_map[ws] = username
        online_users[username] = ws

    def disconnect(self, ws):
        username = self.username_map.pop(ws, None)
        if username:
            online_users.pop(username, None)
        if ws in self.active_connections:
            self.active_connections.remove(ws)

    async def send_to_user(self, username: str, data: dict):
        ws = online_users.get(username)
        if ws:
            await ws.send_json(data)

    async def broadcast(self, data: dict, exclude=None):
        for conn in self.active_connections:
            if conn != exclude:
                try:
                    await conn.send_json(data)
                except:
                    pass

manager = ConnectionManager()

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    current_user = None

    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
                t = data.get("type")

                # ========== AUTH ==========
                if t == "auth":
                    token = data.get("token")
                    if not token:
                        await ws.send_json({"type": "auth_response", "success": False, "message": "No token"})
                        continue
                    payload = verify_token(token)
                    if not payload:
                        await ws.send_json({"type": "auth_response", "success": False, "message": "Invalid token"})
                        continue
                    username = payload.get("sub")
                    user_id = payload.get("user_id")
                    if username not in users_db or users_db[username]["user_id"] != user_id:
                        await ws.send_json({"type": "auth_response", "success": False, "message": "Invalid user"})
                        continue
                    if username in online_users:
                        old = online_users[username]
                        if old != ws:
                            await old.close()
                    await manager.connect(ws, username)
                    current_user = username
                    await ws.send_json({
                        "type": "auth_response",
                        "success": True,
                        "username": username,
                        "user_id": user_id
                    })
                    await manager.broadcast({"type": "user_list", "users": list(online_users.keys())})
                    await ws.send_json({
                        "type": "friend_requests_list",
                        "requests": friend_requests.get(username, [])
                    })
                    await ws.send_json({
                        "type": "friends_list",
                        "friends": friends.get(username, [])
                    })
                    continue

                if not current_user:
                    await ws.send_json({"type": "error", "message": "Not authenticated"})
                    continue

                # ========== ROOM ==========
                if t == "get_room_list":
                    room_list = []
                    for rid, r in rooms.items():
                        room_list.append({
                            "room_id": rid,
                            "room_name": r["name"],
                            "has_password": bool(r.get("password")),
                            "count": len(r["players"]),
                            "max": r["max_players"]
                        })
                    await ws.send_json({"type": "room_list", "rooms": room_list})

                elif t == "create_room":
                    room_id = str(uuid.uuid4())[:6]
                    rooms[room_id] = {
                        "name": data.get("room_name", "Room"),
                        "password": data.get("password", ""),
                        "max_players": data.get("max_players", 8),
                        "host": current_user,
                        "players": [current_user],
                        "room_key": str(uuid.uuid4()),
                        "ips": {current_user: "10.77.0.1"},
                        "next_ip": 2
                    }
                    await ws.send_json({
                        "type": "room_created",
                        "room": {
                            "room_id": room_id,
                            "room_name": rooms[room_id]["name"],
                            "room_key": rooms[room_id]["room_key"],
                            "subnet": "10.77.0.0"
                        }
                    })
                    await manager.broadcast({"type": "room_list", "rooms": []})

                elif t == "join_room":
                    room_id = data.get("room_id")
                    pwd = data.get("password", "")
                    if room_id not in rooms:
                        await ws.send_json({"type": "room_joined", "success": False, "message": "Not found"})
                        continue
                    room = rooms[room_id]
                    if room.get("password") and room["password"] != pwd:
                        await ws.send_json({"type": "room_joined", "success": False, "message": "Wrong password"})
                        continue
                    if len(room["players"]) >= room["max_players"]:
                        await ws.send_json({"type": "room_joined", "success": False, "message": "Full"})
                        continue
                    if current_user not in room["players"]:
                        room["players"].append(current_user)
                        ip_idx = room.get("next_ip", len(room["players"]) + 1)
                        room["ips"][current_user] = f"10.77.0.{ip_idx}"
                        room["next_ip"] = ip_idx + 1
                    await ws.send_json({
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
                    })
                    await manager.broadcast({
                        "type": "room_players",
                        "players": room["players"],
                        "ips": room["ips"],
                        "host": room["host"],
                        "subnet": "10.77.0.0",
                        "room_key": room["room_key"]
                    })

                elif t == "leave_room":
                    for rid, room in list(rooms.items()):
                        if current_user in room["players"]:
                            room["players"].remove(current_user)
                            room["ips"].pop(current_user, None)
                            if room["host"] == current_user or not room["players"]:
                                del rooms[rid]
                                await manager.broadcast({"type": "room_closed", "room_id": rid})
                            else:
                                await manager.broadcast({
                                    "type": "room_players",
                                    "players": room["players"],
                                    "ips": room["ips"],
                                    "host": room["host"],
                                    "subnet": "10.77.0.0",
                                    "room_key": room["room_key"]
                                })
                            break
                    await ws.send_json({"type": "left_room", "success": True})

                # ========== SIGNALING ==========
                elif t == "offer":
                    target = data.get("target")
                    sdp = data.get("sdp")
                    game = data.get("game_name", "")
                    public_key = data.get("public_key", "")
                    if target and sdp:
                        await manager.send_to_user(target, {
                            "type": "offer_received",
                            "from": current_user,
                            "sdp": sdp,
                            "game_name": game,
                            "public_key": public_key
                        })

                elif t == "answer":
                    target = data.get("target")
                    sdp = data.get("sdp")
                    if target and sdp:
                        await manager.send_to_user(target, {
                            "type": "answer_received",
                            "from": current_user,
                            "sdp": sdp
                        })

                elif t == "ice_candidate":
                    target = data.get("target")
                    cand = data.get("candidate")
                    if target and cand:
                        await manager.send_to_user(target, {
                            "type": "ice_candidate_received",
                            "from": current_user,
                            "candidate": cand
                        })

                # ========== CHAT ==========
                elif t == "chat_message":
                    text = data.get("message", "")[:500]
                    if current_user and text:
                        await manager.broadcast({
                            "type": "chat_message",
                            "sender": current_user,
                            "message": text
                        })

                # ========== FRIENDS ==========
                elif t == "friend_request":
                    target = data.get("target")
                    if target and target in users_db:
                        if target not in friend_requests.get(target, []):
                            friend_requests.setdefault(target, []).append(current_user)
                            await manager.send_to_user(target, {
                                "type": "friend_request_received",
                                "from": current_user
                            })

                elif t == "friend_accept":
                    target = data.get("target")
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
                        await ws.send_json({
                            "type": "friends_list",
                            "friends": friends.get(current_user, [])
                        })
                        await manager.send_to_user(target, {
                            "type": "friends_list",
                            "friends": friends.get(target, [])
                        })

                elif t == "friend_reject":
                    target = data.get("target")
                    if target and target in friend_requests.get(current_user, []):
                        friend_requests[current_user].remove(target)

                # ========== READY & LAUNCH ==========
                elif t == "ready_to_launch":
                    await manager.broadcast({
                        "type": "player_ready",
                        "player": current_user,
                        "room": data.get("room_id")
                    })

                elif t == "launch_game":
                    game = data.get("game_name")
                    room_id = data.get("room_id")
                    room = rooms.get(room_id)
                    if room and room["host"] == current_user:
                        await manager.broadcast({
                            "type": "launch_game_command",
                            "game": game,
                            "room_id": room_id
                        })

                elif t == "invite":
                    target = data.get("target")
                    room_id = data.get("room_id")
                    if target and room_id:
                        await manager.send_to_user(target, {
                            "type": "game_invite",
                            "from": current_user,
                            "room_id": room_id
                        })

                else:
                    await ws.send_json({"type": "error", "message": f"Unknown: {t}"})

            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "Invalid JSON"})

    manager.disconnect(ws)
    if current_user:
        await manager.broadcast({"type": "user_list", "users": list(online_users.keys())})
    return ws

# ================== Routes ==================
app.router.add_post("/api/register", register)
app.router.add_post("/api/login", login)
app.router.add_get("/ws", websocket_handler)

# ================== CORS ==================
cors.add(app.router._routes[0])
cors.add(app.router._routes[1])
cors.add(app.router._routes[2])

# ================== ENTRY ==================
if __name__ == "__main__":
    import sys
    port = int(os.environ.get("PORT", 8000))
    web.run_app(app, host="0.0.0.0", port=port)

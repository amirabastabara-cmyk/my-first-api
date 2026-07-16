# server.py
import json
import uuid
import bcrypt
import jwt
import os
import time
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
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
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days
BCRYPT_ROUNDS = 12

# ================== Models ==================
class UserRegister(BaseModel):
    username: str
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    user_id: str

# ================== Database (in-memory) ==================
users_db = {}       # username -> {"password_hash": str, "user_id": str, "created": float}
online_users = {}   # username -> websocket
user_tokens = {}    # username -> token
rooms = {}          # room_id -> dict
friend_requests = {}  # username -> [from_user]
friends = {}        # username -> [friend_username]

security = HTTPBearer()

def create_token(username: str, user_id: str) -> str:
    payload = {
        "sub": username,
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES),
        "iat": datetime.utcnow()
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

# ================== REST API ==================
@app.post("/api/register", response_model=dict)
async def register(user: UserRegister):
    username = user.username.strip()
    password = user.password.strip()
    if not username or len(password) < 6:
        return {"success": False, "message": "Username and password (min 6 chars) required"}
    if username in users_db:
        return {"success": False, "message": "Username already exists"}
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()
    user_id = str(uuid.uuid4())
    users_db[username] = {"password_hash": password_hash, "user_id": user_id, "created": time.time()}
    friends[username] = []
    friend_requests[username] = []
    return {"success": True, "message": "Registered successfully"}

@app.post("/api/login", response_model=TokenResponse)
async def login(user: UserLogin):
    username = user.username.strip()
    password = user.password.strip()
    if username not in users_db:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    stored = users_db[username]
    if not bcrypt.checkpw(password.encode(), stored["password_hash"].encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(username, stored["user_id"])
    user_tokens[username] = token
    return TokenResponse(access_token=token, username=username, user_id=stored["user_id"])

@app.get("/api/verify")
async def verify(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    payload = verify_token(token)
    return {"valid": True, "username": payload["sub"], "user_id": payload["user_id"]}

# ================== WebSocket ==================
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
            await ws.send_json(data)

    async def broadcast(self, data: dict, exclude=None):
        for conn in self.active_connections:
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
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                t = msg.get("type")

                # ========== AUTH ==========
                if t == "auth":
                    token = msg.get("token")
                    if not token:
                        await websocket.send_json({"type": "auth_response", "success": False, "message": "No token"})
                        continue
                    try:
                        payload = verify_token(token)
                        username = payload["sub"]
                        user_id = payload["user_id"]
                        if username not in users_db or users_db[username]["user_id"] != user_id:
                            raise Exception("Invalid user")
                        # اگر قبلاً آنلاین بود، قطعش کن
                        if username in online_users:
                            old = online_users[username]
                            if old != websocket:
                                await old.close(code=1000, reason="Duplicate login")
                        await manager.connect(websocket, username)
                        current_user = username
                        await websocket.send_json({
                            "type": "auth_response",
                            "success": True,
                            "username": username,
                            "user_id": user_id
                        })
                        # ارسال لیست کاربران آنلاین و دوستان
                        await manager.broadcast({"type": "user_list", "users": list(online_users.keys())})
                        # ارسال درخواست‌های دوستی
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

                # ========== اگر احراز نشده ==========
                if not current_user:
                    await websocket.send_json({"type": "error", "message": "Not authenticated"})
                    continue

                # ========== ROOM LIST ==========
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
                    await websocket.send_json({"type": "room_list", "rooms": room_list})

                # ========== CREATE ROOM ==========
                elif t == "create_room":
                    room_id = str(uuid.uuid4())[:6]
                    rooms[room_id] = {
                        "name": msg.get("room_name", "Room"),
                        "password": msg.get("password", ""),
                        "max_players": msg.get("max_players", 8),
                        "host": current_user,
                        "players": [current_user],
                        "room_key": str(uuid.uuid4())  # برای رمزگذاری
                    }
                    await websocket.send_json({
                        "type": "room_created",
                        "room": {
                            "room_id": room_id,
                            "room_name": rooms[room_id]["name"],
                            "room_key": rooms[room_id]["room_key"],
                            "subnet": "10.77.0."
                        }
                    })
                    # بروزرسانی لیست روم‌ها برای همه
                    await manager.broadcast({"type": "room_list", "rooms": []})  # ساده‌سازی

                # ========== JOIN ROOM ==========
                elif t == "join_room":
                    room_id = msg.get("room_id")
                    pwd = msg.get("password", "")
                    if room_id not in rooms:
                        await websocket.send_json({"type": "room_joined", "success": False, "message": "Room not found"})
                        continue
                    room = rooms[room_id]
                    if room.get("password") and room["password"] != pwd:
                        await websocket.send_json({"type": "room_joined", "success": False, "message": "Wrong password"})
                        continue
                    if len(room["players"]) >= room["max_players"]:
                        await websocket.send_json({"type": "room_joined", "success": False, "message": "Room full"})
                        continue
                    if current_user not in room["players"]:
                        room["players"].append(current_user)
                    await websocket.send_json({
                        "type": "room_joined",
                        "room": {
                            "room_id": room_id,
                            "room_name": room["name"],
                            "room_key": room["room_key"],
                            "subnet": "10.77.0."
                        }
                    })
                    # ارسال لیست بازیکنان
                    ips = {p: f"10.77.0.{i+1}" for i, p in enumerate(room["players"])}
                    await manager.broadcast({
                        "type": "room_players",
                        "players": room["players"],
                        "ips": ips,
                        "host": room["host"],
                        "subnet": "10.77.0.",
                        "room_key": room["room_key"]
                    })

                # ========== LEAVE ROOM ==========
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

                # ========== OFFER / ANSWER / ICE ==========
                elif t == "offer":
                    target = msg.get("target")
                    sdp = msg.get("sdp")
                    game = msg.get("game_name", "")
                    if target and sdp:
                        await manager.send_to_user(target, {
                            "type": "offer_received",
                            "from": current_user,
                            "sdp": sdp,
                            "game_name": game
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

                # ========== CHAT ==========
                elif t == "chat_message":
                    text = msg.get("message", "")[:500]
                    if current_user and text:
                        await manager.broadcast({
                            "type": "chat_message",
                            "sender": current_user,
                            "message": text
                        })

                # ========== FRIENDS ==========
                elif t == "friend_request":
                    target = msg.get("target")
                    if target and target in users_db:
                        if target not in friend_requests.get(target, []):
                            friend_requests.setdefault(target, []).append(current_user)
                            await manager.send_to_user(target, {
                                "type": "friend_request_received",
                                "from": current_user
                            })
                            await websocket.send_json({
                                "type": "friend_request_sent",
                                "to": target
                            })

                elif t == "friend_accept":
                    target = msg.get("target")
                    if target:
                        # حذف از درخواست‌ها
                        if target in friend_requests.get(current_user, []):
                            friend_requests[current_user].remove(target)
                        # اضافه کردن به دوستان
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
                        # بروزرسانی لیست دوستان برای طرف مقابل
                        await manager.send_to_user(target, {
                            "type": "friends_list",
                            "friends": friends.get(target, [])
                        })

                elif t == "friend_reject":
                    target = msg.get("target")
                    if target and target in friend_requests.get(current_user, []):
                        friend_requests[current_user].remove(target)
                        await websocket.send_json({
                            "type": "friend_request_rejected",
                            "from": current_user
                        })

                elif t == "get_friend_requests":
                    await websocket.send_json({
                        "type": "friend_requests_list",
                        "requests": friend_requests.get(current_user, [])
                    })

                # ========== GAME LAUNCH SYNC ==========
                elif t == "ready_to_launch":
                    # کاربر اعلام آمادگی برای اجرای بازی
                    await manager.broadcast({
                        "type": "player_ready",
                        "player": current_user,
                        "room": msg.get("room_id")
                    })

                elif t == "launch_game":
                    # هاست دستور اجرای بازی می‌دهد
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
                    await websocket.send_json({"type": "error", "message": f"Unknown type: {t}"})

            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})

    except WebSocketDisconnect:
        manager.disconnect(websocket)
        if current_user:
            await manager.broadcast({"type": "user_list", "users": list(online_users.keys())})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

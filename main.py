# main.py - سرور FastAPI برای لانچر VoidVision
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
import uuid
import hashlib
import time
import jwt
import sqlite3
import os
from datetime import datetime, timedelta

app = FastAPI(title="VoidVision API", version="36.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== تنظیمات =====
SECRET_KEY = os.getenv("JWT_SECRET", "voidvision-super-secret-key-2025")
ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * 24 * 7

# ===== دیتابیس =====
DB_FILE = "users.db"

def init_db():
    try:
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
        print("✅ Database initialized")
    except Exception as e:
        print(f"❌ Database error: {e}")
        if os.path.exists(DB_FILE):
            os.remove(DB_FILE)
            print(f"🗑️ Removed corrupted {DB_FILE}")
        init_db()

init_db()

def get_user(username):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT user_id, password_hash FROM users WHERE username = ?', (username,))
    result = c.fetchone()
    conn.close()
    return result

def create_user(username, password):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    user_id = str(uuid.uuid4())
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    c.execute(
        'INSERT INTO users (username, password_hash, user_id, created_at, last_seen) VALUES (?, ?, ?, ?, ?)',
        (username, password_hash, user_id, int(time.time()), int(time.time()))
    )
    conn.commit()
    conn.close()
    return user_id

# ===== Models =====
class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

class CreateRoomRequest(BaseModel):
    game_name: str
    room_name: str
    password: str = ""
    max_players: int = 8

class JoinRoomRequest(BaseModel):
    room_id: str
    password: str = ""

class ChatRequest(BaseModel):
    message: str

class FriendRequest(BaseModel):
    target: str

class InviteRequest(BaseModel):
    target: str
    game_name: str
    room_id: str

class WebRTCOffer(BaseModel):
    target: str
    sdp: str
    game_name: str
    public_key: str = ""

class WebRTCAnswer(BaseModel):
    target: str
    sdp: str

class WebRTCIce(BaseModel):
    target: str
    candidate: dict

# ===== State =====
online_users: Dict[str, dict] = {}
rooms: Dict[str, dict] = {}
friendships: Dict[str, set] = {}
friend_requests: Dict[str, set] = {}
pending_events: Dict[str, List[dict]] = {}

# ===== JWT =====
def create_token(username, user_id):
    payload = {
        "sub": username,
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except:
        return None

def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.replace("Bearer ", "")
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    username = payload.get("sub")
    if username not in online_users:
        raise HTTPException(status_code=401, detail="User not online")
    return username

# ===== API =====

@app.get("/")
async def root():
    return {"message": "VoidVision API is running!", "version": "36.0"}

@app.post("/api/register")
async def register(req: RegisterRequest):
    if len(req.username) < 3 or len(req.password) < 4:
        return {"success": False, "message": "Username (min 3) and password (min 4) required"}
    try:
        user_id = create_user(req.username, req.password)
        return {"success": True, "user_id": user_id}
    except:
        return {"success": False, "message": "Username already exists"}

@app.post("/api/login")
async def login(req: LoginRequest):
    result = get_user(req.username)
    if not result:
        return {"success": False, "message": "User not found"}
    user_id, password_hash = result
    if password_hash != hashlib.sha256(req.password.encode()).hexdigest():
        return {"success": False, "message": "Wrong password"}
    
    token = create_token(req.username, user_id)
    online_users[req.username] = {"user_id": user_id, "last_seen": int(time.time())}
    broadcast_event({"type": "user_list", "data": {"users": list(online_users.keys())}})
    return {"success": True, "token": token, "user_id": user_id, "username": req.username}

@app.post("/api/auth")
async def auth(token: str):
    payload = verify_token(token)
    if not payload:
        return {"success": False, "message": "Invalid token"}
    username = payload.get("sub")
    user_id = payload.get("user_id")
    if username not in online_users:
        online_users[username] = {"user_id": user_id, "last_seen": int(time.time())}
    return {"success": True, "username": username, "user_id": user_id}

@app.get("/api/users")
async def get_users(username: str = Depends(get_current_user)):
    return {"success": True, "users": list(online_users.keys())}

@app.post("/api/chat")
async def send_chat(req: ChatRequest, username: str = Depends(get_current_user)):
    broadcast_event({
        "type": "chat_message",
        "data": {"sender": username, "message": req.message}
    })
    return {"success": True}

@app.post("/api/rooms/create")
async def create_room(req: CreateRoomRequest, username: str = Depends(get_current_user)):
    room_id = str(uuid.uuid4())[:6]
    rooms[room_id] = {
        "name": req.room_name,
        "game_name": req.game_name,
        "password": req.password,
        "max_players": req.max_players,
        "host": username,
        "players": [username],
        "ips": {username: "10.77.0.1"},
        "room_key": str(uuid.uuid4()),
        "next_ip": 2,
        "created_at": int(time.time())
    }
    broadcast_event({"type": "room_list", "data": {"rooms": get_room_list()}})
    return {"success": True, "room": rooms[room_id]}

@app.post("/api/rooms/join")
async def join_room(req: JoinRoomRequest, username: str = Depends(get_current_user)):
    if req.room_id not in rooms:
        return {"success": False, "message": "Room not found"}
    room = rooms[req.room_id]
    if room.get("password") and room["password"] != req.password:
        return {"success": False, "message": "Wrong password"}
    if len(room.get("players", [])) >= room.get("max_players", 8):
        return {"success": False, "message": "Room full"}
    if username not in room["players"]:
        room["players"].append(username)
        ip_idx = room.get("next_ip", len(room["players"]) + 1)
        room["ips"][username] = f"10.77.0.{ip_idx}"
        room["next_ip"] = ip_idx + 1
    broadcast_event({"type": "room_players", "data": {
        "players": room["players"],
        "ips": room["ips"],
        "host": room["host"],
        "subnet": "10.77.0.0",
        "room_key": room["room_key"]
    }})
    return {"success": True, "room": room}

@app.post("/api/rooms/leave")
async def leave_room(username: str = Depends(get_current_user)):
    for rid, room in list(rooms.items()):
        if username in room.get("players", []):
            room["players"].remove(username)
            room["ips"].pop(username, None)
            if not room.get("players") or room.get("host") == username:
                del rooms[rid]
                broadcast_event({"type": "room_list", "data": {"rooms": get_room_list()}})
            else:
                broadcast_event({"type": "room_players", "data": {
                    "players": room["players"],
                    "ips": room["ips"],
                    "host": room["host"],
                    "subnet": "10.77.0.0",
                    "room_key": room["room_key"]
                }})
            break
    return {"success": True}

@app.get("/api/rooms/list")
async def get_rooms(username: str = Depends(get_current_user)):
    return {"success": True, "rooms": get_room_list()}

def get_room_list():
    result = []
    for rid, room in rooms.items():
        result.append({
            "room_id": rid,
            "room_name": room.get("name", "Room"),
            "has_password": bool(room.get("password")),
            "count": len(room.get("players", [])),
            "max": room.get("max_players", 8)
        })
    return result

@app.post("/api/rooms/ready")
async def player_ready(room_id: str, username: str = Depends(get_current_user)):
    broadcast_event({
        "type": "player_ready",
        "data": {"player": username, "room_id": room_id}
    })
    return {"success": True}

@app.post("/api/rooms/launch")
async def launch_game(req: dict, username: str = Depends(get_current_user)):
    room_id = req.get("room_id")
    game = req.get("game")
    if room_id not in rooms:
        return {"success": False, "message": "Room not found"}
    room = rooms[room_id]
    for player in room.get("players", []):
        if player != username:
            add_event(player, {
                "type": "launch_game_command",
                "data": {"game": game, "room_id": room_id}
            })
    return {"success": True}

@app.post("/api/game/invite")
async def game_invite(req: InviteRequest, username: str = Depends(get_current_user)):
    if req.target not in online_users:
        return {"success": False, "message": "User not online"}
    add_event(req.target, {
        "type": "game_invite",
        "data": {
            "sender": username,
            "game_name": req.game_name,
            "room_id": req.room_id
        }
    })
    return {"success": True}

@app.post("/api/friends/request")
async def send_friend_request(req: FriendRequest, username: str = Depends(get_current_user)):
    if req.target not in online_users:
        return {"success": False, "message": "User not online"}
    if req.target == username:
        return {"success": False, "message": "Cannot add yourself"}
    if req.target not in friend_requests:
        friend_requests[req.target] = set()
    friend_requests[req.target].add(username)
    add_event(req.target, {
        "type": "friend_request_received",
        "data": {"from": username}
    })
    return {"success": True}

@app.post("/api/friends/accept")
async def accept_friend(req: FriendRequest, username: str = Depends(get_current_user)):
    if username not in friendships:
        friendships[username] = set()
    if req.target not in friendships:
        friendships[req.target] = set()
    friendships[username].add(req.target)
    friendships[req.target].add(username)
    if username in friend_requests.get(req.target, set()):
        friend_requests[req.target].discard(username)
    add_event(req.target, {
        "type": "friend_accepted",
        "data": {"from": username}
    })
    return {"success": True}

@app.get("/api/friends/list")
async def get_friends_list(username: str = Depends(get_current_user)):
    return {"success": True, "friends": list(friendships.get(username, set()))}

@app.get("/api/friends/requests")
async def get_friend_requests(username: str = Depends(get_current_user)):
    return {"success": True, "requests": list(friend_requests.get(username, set()))}

@app.post("/api/webrtc/offer")
async def webrtc_offer(req: WebRTCOffer, username: str = Depends(get_current_user)):
    add_event(req.target, {
        "type": "offer_received",
        "data": {
            "from": username,
            "sdp": req.sdp,
            "game_name": req.game_name,
            "public_key": req.public_key
        }
    })
    return {"success": True}

@app.post("/api/webrtc/answer")
async def webrtc_answer(req: WebRTCAnswer, username: str = Depends(get_current_user)):
    add_event(req.target, {
        "type": "answer_received",
        "data": {"from": username, "sdp": req.sdp}
    })
    return {"success": True}

@app.post("/api/webrtc/ice")
async def webrtc_ice(req: WebRTCIce, username: str = Depends(get_current_user)):
    add_event(req.target, {
        "type": "ice_candidate_received",
        "data": {"from": username, "candidate": req.candidate}
    })
    return {"success": True}

@app.get("/api/events")
async def get_events(username: str = Depends(get_current_user)):
    events = pending_events.get(username, [])
    pending_events[username] = []
    return {"success": True, "events": events}

# ===== Helper Functions =====
def add_event(username: str, event: dict):
    if username not in pending_events:
        pending_events[username] = []
    pending_events[username].append(event)

def broadcast_event(event: dict):
    for username in online_users.keys():
        add_event(username, event)

# ===== Cleanup =====
@app.on_event("startup")
async def startup():
    import threading
    def cleanup():
        while True:
            time.sleep(60)
            now = time.time()
            for username in list(online_users.keys()):
                if now - online_users[username]["last_seen"] > 300:
                    del online_users[username]
                    broadcast_event({"type": "user_list", "data": {"users": list(online_users.keys())}})
    threading.Thread(target=cleanup, daemon=True).start()

# ===== Entry =====
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

# main.py - سرور WebSocket با FastAPI برای VoidVision
import json
import uuid
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="VoidVision Server")

# CORS برای اتصال از هر کلاینت
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ذخیره‌سازی موقت در حافظه
users = {}          # username -> password
online = set()      # active usernames
rooms = {}          # room_id -> dict
user_ws = {}        # username -> WebSocket (برای ارسال پیام مستقیم)
pending_offers = {} # target_username -> list of pending offers

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.connection_username: Dict[WebSocket, str] = {}

    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        self.active_connections.append(websocket)
        self.connection_username[websocket] = username
        online.add(username)
        user_ws[username] = websocket

    def disconnect(self, websocket: WebSocket):
        username = self.connection_username.pop(websocket, None)
        if username:
            online.discard(username)
            user_ws.pop(username, None)
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def send_personal(self, username: str, message: dict):
        ws = user_ws.get(username)
        if ws:
            await ws.send_json(message)

    async def broadcast(self, message: dict, exclude=None):
        for conn in self.active_connections:
            if conn != exclude:
                await conn.send_json(message)

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
                msg_type = msg.get("type")

                # ---------- REGISTER ----------
                if msg_type == "register":
                    username = msg.get("username", "").strip()
                    password = msg.get("password", "").strip()
                    if not username or len(password) < 4:
                        await websocket.send_json({"type": "register_response", "success": False, "message": "Invalid credentials"})
                        continue
                    if username in users:
                        await websocket.send_json({"type": "register_response", "success": False, "message": "Username already exists"})
                    else:
                        users[username] = password
                        await websocket.send_json({"type": "register_response", "success": True, "message": "Registered successfully"})

                # ---------- LOGIN ----------
                elif msg_type == "login":
                    username = msg.get("username", "").strip()
                    password = msg.get("password", "").strip()
                    if username in users and users[username] == password:
                        # logout previous session if any
                        if username in online:
                            old_ws = user_ws.get(username)
                            if old_ws and old_ws != websocket:
                                await old_ws.close(code=1000, reason="Duplicate login")
                        await manager.connect(websocket, username)
                        current_user = username
                        await websocket.send_json({
                            "type": "login_response",
                            "success": True,
                            "username": username,
                            "user_id": str(uuid.uuid4())
                        })
                        # Send updated user list to all
                        await manager.broadcast({"type": "user_list", "users": list(online)})
                    else:
                        await websocket.send_json({"type": "login_response", "success": False, "message": "Invalid username/password"})

                # ---------- GET USER LIST ----------
                elif msg_type == "get_user_list":
                    await websocket.send_json({"type": "user_list", "users": list(online)})

                # ---------- GET ROOM LIST ----------
                elif msg_type == "get_room_list":
                    room_list = []
                    for rid, rdata in rooms.items():
                        room_list.append({
                            "room_id": rid,
                            "room_name": rdata["name"],
                            "has_password": bool(rdata.get("password")),
                            "count": len(rdata["players"]),
                            "max": rdata["max_players"]
                        })
                    await websocket.send_json({"type": "room_list", "rooms": room_list})

                # ---------- CREATE ROOM ----------
                elif msg_type == "create_room":
                    if not current_user:
                        await websocket.send_json({"type": "error", "message": "Not logged in"})
                        continue
                    room_id = str(uuid.uuid4())[:6]
                    rooms[room_id] = {
                        "name": msg.get("room_name", "New Room"),
                        "password": msg.get("password", ""),
                        "max_players": msg.get("max_players", 8),
                        "host": current_user,
                        "players": [current_user],
                        "room_key": hashlib.sha256(os.urandom(32)).hexdigest()  # for P2P encryption
                    }
                    await websocket.send_json({
                        "type": "room_created",
                        "room": {
                            "room_id": room_id,
                            "room_name": rooms[room_id]["name"],
                            "room_key": rooms[room_id]["room_key"],
                            "subnet": DEFAULT_SUBNET  # از تنظیمات سرور
                        }
                    })
                    # ارسال لیست روم‌ها به همه
                    await manager.broadcast({"type": "room_list", "rooms": [...]})  # به‌روزرسانی ساده

                # ---------- JOIN ROOM ----------
                elif msg_type == "join_room":
                    if not current_user:
                        await websocket.send_json({"type": "error", "message": "Not logged in"})
                        continue
                    room_id = msg.get("room_id")
                    password = msg.get("password", "")
                    if room_id not in rooms:
                        await websocket.send_json({"type": "room_joined", "success": False, "message": "Room not found"})
                        continue
                    room = rooms[room_id]
                    if room.get("password") and room["password"] != password:
                        await websocket.send_json({"type": "room_joined", "success": False, "message": "Wrong password"})
                        continue
                    if len(room["players"]) >= room["max_players"]:
                        await websocket.send_json({"type": "room_joined", "success": False, "message": "Room full"})
                        continue
                    if current_user not in room["players"]:
                        room["players"].append(current_user)
                    # پاسخ به کاربر
                    await websocket.send_json({
                        "type": "room_joined",
                        "room": {
                            "room_id": room_id,
                            "room_name": room["name"],
                            "room_key": room["room_key"],
                            "subnet": DEFAULT_SUBNET
                        }
                    })
                    # ارسال لیست بازیکنان به همه اعضای روم (شامل هاست)
                    player_list = room["players"]
                    host = room["host"]
                    # تخصیص IP مجازی
                    ips = {}
                    for idx, p in enumerate(player_list, start=1):
                        ips[p] = f"{DEFAULT_SUBNET}{idx}"
                    await manager.broadcast({
                        "type": "room_players",
                        "players": player_list,
                        "ips": ips,
                        "host": host,
                        "subnet": DEFAULT_SUBNET,
                        "room_key": room["room_key"]
                    }, exclude=None)  # به همه بفرست

                # ---------- LEAVE ROOM ----------
                elif msg_type == "leave_room":
                    if current_user:
                        for rid, room in rooms.items():
                            if current_user in room["players"]:
                                room["players"].remove(current_user)
                                # اگر هاست رفت، روم حذف شود
                                if room["host"] == current_user or not room["players"]:
                                    del rooms[rid]
                                    await manager.broadcast({"type": "room_closed", "room_id": rid})
                                else:
                                    await manager.broadcast({"type": "room_players", ...})  # به‌روزرسانی
                                break
                        await websocket.send_json({"type": "left_room", "success": True})

                # ---------- OFFER (WebRTC Signaling) ----------
                elif msg_type == "offer":
                    target = msg.get("target")
                    sdp = msg.get("sdp")
                    game_name = msg.get("game_name", "")
                    if target and sdp:
                        # ارسال به target
                        await manager.send_personal(target, {
                            "type": "offer_received",
                            "from": current_user,
                            "sdp": sdp,
                            "game_name": game_name
                        })

                # ---------- ANSWER ----------
                elif msg_type == "answer":
                    target = msg.get("target")
                    sdp = msg.get("sdp")
                    if target and sdp:
                        await manager.send_personal(target, {
                            "type": "answer_received",
                            "from": current_user,
                            "sdp": sdp
                        })

                # ---------- ICE CANDIDATE ----------
                elif msg_type == "ice_candidate":
                    target = msg.get("target")
                    candidate = msg.get("candidate")
                    if target and candidate:
                        await manager.send_personal(target, {
                            "type": "ice_candidate_received",
                            "from": current_user,
                            "candidate": candidate
                        })

                # ---------- CHAT MESSAGE ----------
                elif msg_type == "chat_message":
                    text = msg.get("message", "")[:500]
                    if current_user and text:
                        await manager.broadcast({
                            "type": "chat_message",
                            "sender": current_user,
                            "message": text
                        })

                # ---------- FRIEND REQUEST (ساده) ----------
                elif msg_type == "friend_request":
                    target = msg.get("target")
                    if target and target in online:
                        await manager.send_personal(target, {
                            "type": "friend_request",
                            "from": current_user,
                            "message": msg.get("message", "")
                        })
                elif msg_type == "friend_accept":
                    target = msg.get("target")
                    if target and target in online:
                        await manager.send_personal(target, {
                            "type": "friend_accepted",
                            "from": current_user
                        })
                elif msg_type == "friend_reject":
                    target = msg.get("target")
                    if target and target in online:
                        await manager.send_personal(target, {
                            "type": "friend_rejected",
                            "from": current_user
                        })

                # ---------- GAME INVITE ----------
                elif msg_type == "game_invite":
                    target = msg.get("target")
                    game_name = msg.get("game_name", "")
                    room_id = msg.get("room_id", "")
                    if target and target in online:
                        await manager.send_personal(target, {
                            "type": "game_invite",
                            "from": current_user,
                            "game_name": game_name,
                            "room_id": room_id
                        })

                # ---------- UNKNOWN ----------
                else:
                    await websocket.send_json({"type": "error", "message": f"Unknown type: {msg_type}"})

            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})

    except WebSocketDisconnect:
        manager.disconnect(websocket)
        if current_user:
            # به‌روزرسانی لیست کاربران
            await manager.broadcast({"type": "user_list", "users": list(online)})
        print(f"User {current_user or 'Unknown'} disconnected")

if __name__ == "__main__":
    import uvicorn
    import os
    # برای تولید کلید روم از hashlib نیازمندیم
    import hashlib
    DEFAULT_SUBNET = "10.77.0."
    uvicorn.run(app, host="0.0.0.0", port=8000)

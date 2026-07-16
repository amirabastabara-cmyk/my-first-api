# server.py
import json
import uuid
import hashlib
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="VoidVision Signaling Server")

# CORS برای دسترسی از هر کلاینتی
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# دیتابیس موقت در حافظه (برای تست)
users_db = {}          # username -> password
online_users = set()   # username
rooms = {}             # room_id -> dict
user_connections = {}  # username -> WebSocket

DEFAULT_SUBNET = "10.77.0."

class Manager:
    def __init__(self):
        self.active_connections = []
        self.user_map = {}

    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        self.active_connections.append(websocket)
        self.user_map[websocket] = username
        online_users.add(username)
        user_connections[username] = websocket

    def disconnect(self, websocket: WebSocket):
        username = self.user_map.pop(websocket, None)
        if username:
            online_users.discard(username)
            user_connections.pop(username, None)
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def send_to_user(self, username: str, data: dict):
        ws = user_connections.get(username)
        if ws:
            await ws.send_json(data)

    async def broadcast(self, data: dict, exclude=None):
        for conn in self.active_connections:
            if conn != exclude:
                try:
                    await conn.send_json(data)
                except:
                    pass

manager = Manager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    current_user = None
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                t = msg.get("type")

                # ---------- REGISTER ----------
                if t == "register":
                    u = msg.get("username", "").strip()
                    p = msg.get("password", "").strip()
                    if not u or len(p) < 4:
                        await websocket.send_json({"type": "register_response", "success": False, "message": "Invalid"})
                    elif u in users_db:
                        await websocket.send_json({"type": "register_response", "success": False, "message": "Exists"})
                    else:
                        users_db[u] = p
                        await websocket.send_json({"type": "register_response", "success": True, "message": "OK"})

                # ---------- LOGIN ----------
                elif t == "login":
                    u = msg.get("username", "").strip()
                    p = msg.get("password", "").strip()
                    if u in users_db and users_db[u] == p:
                        # قطع جلسه قبلی اگر باشد
                        if u in online_users:
                            old = user_connections.get(u)
                            if old and old != websocket:
                                await old.close(code=1000, reason="Duplicate login")
                        await manager.connect(websocket, u)
                        current_user = u
                        await websocket.send_json({
                            "type": "login_response",
                            "success": True,
                            "username": u,
                            "user_id": str(uuid.uuid4())
                        })
                        await manager.broadcast({"type": "user_list", "users": list(online_users)})
                    else:
                        await websocket.send_json({"type": "login_response", "success": False, "message": "Wrong"})

                # ---------- GET USER LIST ----------
                elif t == "get_user_list":
                    await websocket.send_json({"type": "user_list", "users": list(online_users)})

                # ---------- ROOM LIST ----------
                elif t == "get_room_list":
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

                # ---------- CREATE ROOM ----------
                elif t == "create_room":
                    if not current_user:
                        await websocket.send_json({"type": "error", "message": "Not logged in"})
                        continue
                    room_id = str(uuid.uuid4())[:6]
                    rooms[room_id] = {
                        "name": msg.get("room_name", "Room"),
                        "password": msg.get("password", ""),
                        "max_players": msg.get("max_players", 8),
                        "host": current_user,
                        "players": [current_user],
                        "room_key": hashlib.sha256(os.urandom(32)).hexdigest()
                    }
                    await websocket.send_json({
                        "type": "room_created",
                        "room": {
                            "room_id": room_id,
                            "room_name": rooms[room_id]["name"],
                            "room_key": rooms[room_id]["room_key"],
                            "subnet": DEFAULT_SUBNET
                        }
                    })
                    # به‌روزرسانی لیست روم‌ها برای همه
                    await manager.broadcast({"type": "room_list", "rooms": room_list})  # بازسازی لیست

                # ---------- JOIN ROOM ----------
                elif t == "join_room":
                    if not current_user:
                        await websocket.send_json({"type": "error", "message": "Not logged in"})
                        continue
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
                    await websocket.send_json({
                        "type": "room_joined",
                        "room": {
                            "room_id": room_id,
                            "room_name": room["name"],
                            "room_key": room["room_key"],
                            "subnet": DEFAULT_SUBNET
                        }
                    })
                    # ارسال لیست بازیکنان به همه اعضای روم
                    ips = {p: f"{DEFAULT_SUBNET}{i+1}" for i, p in enumerate(room["players"])}
                    await manager.broadcast({
                        "type": "room_players",
                        "players": room["players"],
                        "ips": ips,
                        "host": room["host"],
                        "subnet": DEFAULT_SUBNET,
                        "room_key": room["room_key"]
                    })

                # ---------- LEAVE ROOM ----------
                elif t == "leave_room":
                    if current_user:
                        for rid, room in list(rooms.items()):
                            if current_user in room["players"]:
                                room["players"].remove(current_user)
                                if room["host"] == current_user or not room["players"]:
                                    del rooms[rid]
                                    await manager.broadcast({"type": "room_closed", "room_id": rid})
                                else:
                                    ips = {p: f"{DEFAULT_SUBNET}{i+1}" for i, p in enumerate(room["players"])}
                                    await manager.broadcast({
                                        "type": "room_players",
                                        "players": room["players"],
                                        "ips": ips,
                                        "host": room["host"],
                                        "subnet": DEFAULT_SUBNET,
                                        "room_key": room["room_key"]
                                    })
                                break
                        await websocket.send_json({"type": "left_room", "success": True})

                # ---------- OFFER (WebRTC signaling) ----------
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

                # ---------- ANSWER ----------
                elif t == "answer":
                    target = msg.get("target")
                    sdp = msg.get("sdp")
                    if target and sdp:
                        await manager.send_to_user(target, {
                            "type": "answer_received",
                            "from": current_user,
                            "sdp": sdp
                        })

                # ---------- ICE CANDIDATE ----------
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

                # ---------- FRIEND ----------
                elif t == "friend_request":
                    target = msg.get("target")
                    if target:
                        await manager.send_to_user(target, {
                            "type": "friend_request",
                            "from": current_user,
                            "message": msg.get("message", "")
                        })
                elif t == "friend_accept":
                    target = msg.get("target")
                    if target:
                        await manager.send_to_user(target, {
                            "type": "friend_accepted",
                            "from": current_user
                        })
                elif t == "friend_reject":
                    target = msg.get("target")
                    if target:
                        await manager.send_to_user(target, {
                            "type": "friend_rejected",
                            "from": current_user
                        })

                # ---------- INVITE ----------
                elif t == "game_invite":
                    target = msg.get("target")
                    if target:
                        await manager.send_to_user(target, {
                            "type": "game_invite",
                            "from": current_user,
                            "game_name": msg.get("game_name", ""),
                            "room_id": msg.get("room_id", "")
                        })

                else:
                    await websocket.send_json({"type": "error", "message": f"Unknown type: {t}"})

            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})

    except WebSocketDisconnect:
        manager.disconnect(websocket)
        if current_user:
            await manager.broadcast({"type": "user_list", "users": list(online_users)})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

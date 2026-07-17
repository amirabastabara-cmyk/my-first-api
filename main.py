# main.py - VoidVision Server (Lightweight)
import os
import json
import hashlib
import time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

app = FastAPI()

# ================== دیتابیس ساده (در حافظه) ==================
users = {}           # username -> {"password": "hash", "id": "..."}
online_users = {}    # username -> websocket
rooms = {}           # room_id -> {"name": "", "host": "", "players": [], "password": ""}
friends = {}         # username -> [friend_username]
friend_requests = {} # username -> [from_user]

# ================== توابع کمکی ==================
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def generate_id() -> str:
    return hashlib.sha256(str(time.time()).encode()).hexdigest()[:8]

# ================== REST API ==================
@app.get("/")
async def root():
    return {"status": "ok", "message": "VoidVision Server"}

@app.post("/api/register")
async def register(request_data: dict):
    username = request_data.get("username", "").strip()
    password = request_data.get("password", "").strip()
    
    if not username or not password:
        return {"success": False, "message": "Username and password required"}
    
    if len(password) < 4:
        return {"success": False, "message": "Password must be at least 4 characters"}
    
    if username in users:
        return {"success": False, "message": "Username already exists"}
    
    users[username] = {
        "password": hash_password(password),
        "id": generate_id()
    }
    friends[username] = []
    friend_requests[username] = []
    
    return {"success": True, "message": "Registered successfully"}

@app.post("/api/login")
async def login(request_data: dict):
    username = request_data.get("username", "").strip()
    password = request_data.get("password", "").strip()
    
    if not username or not password:
        return {"success": False, "message": "Username and password required"}
    
    if username not in users:
        return {"success": False, "message": "User not found"}
    
    if users[username]["password"] != hash_password(password):
        return {"success": False, "message": "Invalid password"}
    
    return {
        "success": True,
        "username": username,
        "user_id": users[username]["id"]
    }

# ================== WebSocket ==================
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
                
                # ===== AUTH =====
                if msg_type == "auth":
                    username = msg.get("username", "").strip()
                    if username in online_users:
                        await websocket.send_text(json.dumps({
                            "type": "auth_response",
                            "success": False,
                            "message": "User already online"
                        }))
                        continue
                    if username in users:
                        online_users[username] = websocket
                        current_user = username
                        await websocket.send_text(json.dumps({
                            "type": "auth_response",
                            "success": True,
                            "username": username,
                            "user_id": users[username]["id"]
                        }))
                        await broadcast_user_list()
                    continue
                
                # ===== اگر احراز هویت نشده =====
                if not current_user:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Not authenticated"
                    }))
                    continue
                
                # ===== USER LIST =====
                if msg_type == "get_users":
                    await websocket.send_text(json.dumps({
                        "type": "user_list",
                        "users": list(online_users.keys())
                    }))
                
                # ===== CHAT =====
                elif msg_type == "chat_message":
                    text = msg.get("message", "")[:500]
                    if text:
                        await broadcast({
                            "type": "chat_message",
                            "sender": current_user,
                            "message": text
                        })
                
                # ===== FRIENDS =====
                elif msg_type == "friend_request":
                    target = msg.get("target", "").strip()
                    if target and target in users:
                        if target not in friend_requests.get(target, []):
                            friend_requests.setdefault(target, []).append(current_user)
                            if target in online_users:
                                await online_users[target].send_text(json.dumps({
                                    "type": "friend_request_received",
                                    "from": current_user
                                }))
                        await websocket.send_text(json.dumps({
                            "type": "friend_request_sent",
                            "to": target
                        }))
                
                elif msg_type == "friend_accept":
                    target = msg.get("target", "").strip()
                    if target:
                        if target in friend_requests.get(current_user, []):
                            friend_requests[current_user].remove(target)
                        if current_user not in friends.get(target, []):
                            friends.setdefault(target, []).append(current_user)
                        if target not in friends.get(current_user, []):
                            friends.setdefault(current_user, []).append(target)
                        if target in online_users:
                            await online_users[target].send_text(json.dumps({
                                "type": "friend_accepted",
                                "from": current_user
                            }))
                        await websocket.send_text(json.dumps({
                            "type": "friends_list",
                            "friends": friends.get(current_user, [])
                        }))
                
                elif msg_type == "get_friends":
                    await websocket.send_text(json.dumps({
                        "type": "friends_list",
                        "friends": friends.get(current_user, [])
                    }))
                
                elif msg_type == "get_friend_requests":
                    await websocket.send_text(json.dumps({
                        "type": "friend_requests_list",
                        "requests": friend_requests.get(current_user, [])
                    }))
                
                # ===== ROOMS =====
                elif msg_type == "get_room_list":
                    room_list = []
                    for rid, r in rooms.items():
                        room_list.append({
                            "room_id": rid,
                            "room_name": r["name"],
                            "has_password": bool(r.get("password")),
                            "count": len(r["players"]),
                            "max": r.get("max_players", 8)
                        })
                    await websocket.send_text(json.dumps({
                        "type": "room_list",
                        "rooms": room_list
                    }))
                
                elif msg_type == "create_room":
                    room_id = generate_id()
                    rooms[room_id] = {
                        "name": msg.get("room_name", "Room"),
                        "password": msg.get("password", ""),
                        "max_players": msg.get("max_players", 8),
                        "host": current_user,
                        "players": [current_user],
                        "room_key": generate_id(),
                        "ips": {current_user: "10.77.0.1"},
                        "next_ip": 2
                    }
                    await websocket.send_text(json.dumps({
                        "type": "room_created",
                        "room": {
                            "room_id": room_id,
                            "room_name": rooms[room_id]["name"],
                            "room_key": rooms[room_id]["room_key"],
                            "subnet": "10.77.0.0"
                        }
                    }))
                    await broadcast({"type": "room_list", "rooms": []})
                
                elif msg_type == "join_room":
                    room_id = msg.get("room_id")
                    pwd = msg.get("password", "")
                    if room_id not in rooms:
                        await websocket.send_text(json.dumps({
                            "type": "room_joined",
                            "success": False,
                            "message": "Room not found"
                        }))
                        continue
                    room = rooms[room_id]
                    if room.get("password") and room["password"] != pwd:
                        await websocket.send_text(json.dumps({
                            "type": "room_joined",
                            "success": False,
                            "message": "Wrong password"
                        }))
                        continue
                    if len(room["players"]) >= room["max_players"]:
                        await websocket.send_text(json.dumps({
                            "type": "room_joined",
                            "success": False,
                            "message": "Room full"
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
                            "room_name": room["name"],
                            "room_key": room["room_key"],
                            "subnet": "10.77.0.0",
                            "players": room["players"],
                            "ips": room["ips"],
                            "host": room["host"]
                        }
                    }))
                    await broadcast({
                        "type": "room_players",
                        "players": room["players"],
                        "ips": room["ips"],
                        "host": room["host"],
                        "subnet": "10.77.0.0",
                        "room_key": room["room_key"]
                    })
                
                elif msg_type == "leave_room":
                    for rid, room in list(rooms.items()):
                        if current_user in room["players"]:
                            room["players"].remove(current_user)
                            room["ips"].pop(current_user, None)
                            if room["host"] == current_user or not room["players"]:
                                del rooms[rid]
                                await broadcast({"type": "room_closed", "room_id": rid})
                            else:
                                await broadcast({
                                    "type": "room_players",
                                    "players": room["players"],
                                    "ips": room["ips"],
                                    "host": room["host"],
                                    "subnet": "10.77.0.0",
                                    "room_key": room["room_key"]
                                })
                            break
                    await websocket.send_text(json.dumps({
                        "type": "left_room",
                        "success": True
                    }))
                
                # ===== WEBRTC SIGNALING =====
                elif msg_type == "offer":
                    target = msg.get("target")
                    sdp = msg.get("sdp")
                    if target and sdp and target in online_users:
                        await online_users[target].send_text(json.dumps({
                            "type": "offer_received",
                            "from": current_user,
                            "sdp": sdp,
                            "game_name": msg.get("game_name", "")
                        }))
                
                elif msg_type == "answer":
                    target = msg.get("target")
                    sdp = msg.get("sdp")
                    if target and sdp and target in online_users:
                        await online_users[target].send_text(json.dumps({
                            "type": "answer_received",
                            "from": current_user,
                            "sdp": sdp
                        }))
                
                elif msg_type == "ice_candidate":
                    target = msg.get("target")
                    candidate = msg.get("candidate")
                    if target and candidate and target in online_users:
                        await online_users[target].send_text(json.dumps({
                            "type": "ice_candidate_received",
                            "from": current_user,
                            "candidate": candidate
                        }))
                
                # ===== READY & LAUNCH =====
                elif msg_type == "ready_to_launch":
                    await broadcast({
                        "type": "player_ready",
                        "player": current_user,
                        "room": msg.get("room_id")
                    })
                
                elif msg_type == "launch_game":
                    game = msg.get("game_name")
                    room_id = msg.get("room_id")
                    room = rooms.get(room_id)
                    if room and room["host"] == current_user:
                        await broadcast({
                            "type": "launch_game_command",
                            "game": game,
                            "room_id": room_id
                        })
                
                elif msg_type == "invite":
                    target = msg.get("target")
                    room_id = msg.get("room_id")
                    if target and room_id and target in online_users:
                        await online_users[target].send_text(json.dumps({
                            "type": "game_invite",
                            "from": current_user,
                            "room_id": room_id
                        }))
                
                else:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": f"Unknown type: {msg_type}"
                    }))
                    
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Invalid JSON"
                }))
                
    except WebSocketDisconnect:
        pass
    finally:
        if current_user and current_user in online_users:
            del online_users[current_user]
            await broadcast_user_list()
            print(f"❌ {current_user} disconnected")

# ================== Helper Functions ==================
async def broadcast(data: dict, exclude: str = None):
    for name, ws in list(online_users.items()):
        if name != exclude:
            try:
                await ws.send_text(json.dumps(data))
            except:
                pass

async def broadcast_user_list():
    await broadcast({
        "type": "user_list",
        "users": list(online_users.keys())
    })

# ================== Entry ==================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

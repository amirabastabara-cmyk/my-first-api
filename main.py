from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn
import os
import json

app = FastAPI()

# دیکشنری برای نگهداری کاربران آنلاین
connected_users = {}

@app.get("/")
async def root():
    return {"message": "VoidVision Server is running!"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    username = None
    
    try:
        # دریافت نام کاربری
        data = await websocket.receive_text()
        login_data = json.loads(data)
        
        if login_data.get("type") == "login":
            username = login_data.get("username", "").strip()
            
            if not username:
                await websocket.send_text(json.dumps({"type": "error", "message": "Invalid username!"}))
                await websocket.close()
                return
                
            if username in connected_users:
                await websocket.send_text(json.dumps({"type": "error", "message": "Username already taken!"}))
                await websocket.close()
                return
            
            connected_users[username] = websocket
            await websocket.send_text(json.dumps({"type": "login_response", "status": "success"}))
            print(f"✅ {username} connected")
            
            # ارسال لیست کاربران به همه
            await broadcast_user_list()
            
            # حلقه دریافت پیام‌ها
            while True:
                try:
                    data = await websocket.receive_text()
                    msg = json.loads(data)
                    msg_type = msg.get("type")
                    
                    if msg_type == "chat_message":
                        await broadcast({
                            "type": "chat_message",
                            "sender": username,
                            "message": msg.get("message", "")
                        }, exclude=username)
                        print(f"💬 [{username}]: {msg.get('message', '')}")
                        
                    elif msg_type == "game_invite":
                        target = msg.get("target")
                        if target and target in connected_users:
                            await connected_users[target].send_text(json.dumps({
                                "type": "game_invite",
                                "sender": username,
                                "game_name": msg.get("game_name", "Unknown Game")
                            }))
                            print(f"🎮 {username} invited {target}")
                            
                    elif msg_type == "get_users":
                        await send_user_list(username)
                        
                except WebSocketDisconnect:
                    break
                    
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"⚠️ Error: {e}")
    finally:
        if username and username in connected_users:
            del connected_users[username]
            await broadcast_user_list()
            print(f"❌ {username} disconnected")

async def broadcast(data, exclude=None):
    for name, ws in list(connected_users.items()):
        if name != exclude:
            try:
                await ws.send_text(json.dumps(data))
            except:
                pass

async def broadcast_user_list():
    users = list(connected_users.keys())
    await broadcast({"type": "user_list", "users": users})
    print(f"👥 Online users: {', '.join(users) if users else 'None'}")

async def send_user_list(username):
    if username in connected_users:
        try:
            await connected_users[username].send_text(json.dumps({
                "type": "user_list",
                "users": list(connected_users.keys())
            }))
        except:
            pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print("=" * 50)
    print("🚀 VOIDVISION SERVER (FastAPI + WebSocket)")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=port)

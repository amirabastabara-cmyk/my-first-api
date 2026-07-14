import asyncio
import json
import os
import websockets
from websockets.server import WebSocketServerProtocol

connected_users = {}  # {username: websocket}

async def handler(websocket: WebSocketServerProtocol, path: str):
    # ===== پاسخ به درخواست Health Check از Render =====
    if path == "/healthz":
        await websocket.send(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        await websocket.close()
        return

    username = None
    try:
        # ===== تنظیم تایم‌اوت برای دریافت اولین پیام (۵ ثانیه) =====
        try:
            data = await asyncio.wait_for(websocket.recv(), timeout=5.0)
        except asyncio.TimeoutError:
            print("⚠️ Client did not send login message within 5 seconds, closing connection")
            await websocket.close()
            return

        login_data = json.loads(data)

        if login_data.get("type") == "login":
            username = login_data.get("username", "").strip()
            if not username:
                await websocket.send(json.dumps({"type": "error", "message": "Invalid username!"}))
                await websocket.close()
                return

            if username in connected_users:
                await websocket.send(json.dumps({"type": "error", "message": "Username already taken!"}))
                await websocket.close()
                return

            connected_users[username] = websocket
            await websocket.send(json.dumps({"type": "login_response", "status": "success"}))
            print(f"✅ User '{username}' connected (Total: {len(connected_users)})")
            await broadcast_user_list()

            # ===== حلقه دریافت پیام‌ها با تایم‌اوت =====
            while True:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                    try:
                        msg = json.loads(message)
                        await process_message(username, msg)
                    except json.JSONDecodeError:
                        continue
                except asyncio.TimeoutError:
                    # ارسال PING برای حفظ اتصال
                    try:
                        await websocket.ping()
                    except:
                        break
                except websockets.exceptions.ConnectionClosed:
                    break
                except Exception as e:
                    print(f"⚠️ Error in message loop: {e}")
                    break

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"⚠️ Error handling client: {e}")
    finally:
        if username and username in connected_users:
            del connected_users[username]
            await broadcast_user_list()
            print(f"❌ User '{username}' disconnected (Total: {len(connected_users)})")
        try:
            await websocket.close()
        except:
            pass

async def process_message(sender, message):
    msg_type = message.get("type")
    if msg_type == "chat_message":
        await broadcast({
            "type": "chat_message",
            "sender": sender,
            "message": message.get("message", "")
        }, exclude=sender)
        print(f"💬 [{sender}]: {message.get('message', '')}")
    elif msg_type == "game_invite":
        target = message.get("target")
        if target and target in connected_users:
            try:
                await connected_users[target].send(json.dumps({
                    "type": "game_invite",
                    "sender": sender,
                    "game_name": message.get("game_name", "Unknown Game")
                }))
                print(f"🎮 {sender} invited {target} to play {message.get('game_name', 'Unknown Game')}")
            except:
                pass
    elif msg_type == "get_users":
        await send_user_list(sender)

async def broadcast(data, exclude=None):
    for username, ws in connected_users.items():
        if username != exclude:
            try:
                await ws.send(json.dumps(data))
            except:
                pass

async def broadcast_user_list():
    users = list(connected_users.keys())
    await broadcast({"type": "user_list", "users": users})
    print(f"👥 Online users: {', '.join(users) if users else 'None'}")

async def send_user_list(username):
    if username in connected_users:
        try:
            await connected_users[username].send(json.dumps({
                "type": "user_list",
                "users": list(connected_users.keys())
            }))
        except:
            pass

async def main():
    port = int(os.environ.get("PORT", 10000))
    print("=" * 50)
    print("🚀 VOIDVISION GAME SERVER (WebSocket + Health Check)")
    print("=" * 50)

    async with websockets.serve(handler, "", port):
        print(f"✅ WebSocket server started on port {port}")
        print("🟢 Waiting for connections...")
        await asyncio.Future()  # اجرای بی‌نهایت

if __name__ == "__main__":
    asyncio.run(main())

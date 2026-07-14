import asyncio
import json
import os
import websockets

connected_users = {}  # {username: websocket}

async def handler(websocket, path):
    username = None
    try:
        # دریافت اطلاعات لاگین
        data = await websocket.recv()
        login_data = json.loads(data)

        if login_data.get("type") == "login":
            username = login_data.get("username", "").strip()
            if not username:
                await websocket.send(json.dumps({"type": "error", "message": "Invalid username!"}))
                return

            if username in connected_users:
                await websocket.send(json.dumps({"type": "error", "message": "Username already taken!"}))
                return

            connected_users[username] = websocket
            await websocket.send(json.dumps({"type": "login_response", "status": "success"}))
            print(f"✅ User '{username}' connected (Total: {len(connected_users)})")
            await broadcast_user_list()

            # حلقه دریافت پیام‌ها
            async for message in websocket:
                try:
                    msg = json.loads(message)
                    await process_message(username, msg)
                except json.JSONDecodeError:
                    continue
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if username and username in connected_users:
            del connected_users[username]
            await broadcast_user_list()
            print(f"❌ User '{username}' disconnected (Total: {len(connected_users)})")

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

# ===== اضافه کردن یک سرور HTTP ساده برای Health Check =====
async def health_check_handler(reader, writer):
    try:
        data = await reader.read(1024)
        if data:
            # تشخیص درخواست HEAD یا GET
            if data.startswith(b'HEAD') or data.startswith(b'GET'):
                response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"
                writer.write(response)
                await writer.drain()
    except:
        pass
    finally:
        writer.close()
        await writer.wait_closed()

async def main():
    port = int(os.environ.get("PORT", 10000))
    print("=" * 50)
    print("🚀 VOIDVISION GAME SERVER (WebSocket + HTTP Health Check)")
    print("=" * 50)

    # راه‌اندازی سرور WebSocket
    ws_server = await websockets.serve(handler, "", port)

    # راه‌اندازی یک سرور HTTP ساده برای Health Check (روی همان پورت)
    http_server = await asyncio.start_server(health_check_handler, "", port)

    print(f"✅ WebSocket server started on port {port}")
    print(f"✅ HTTP health check server started on port {port}")
    print("🟢 Waiting for connections...")

    # اجرای هر دو سرور به صورت همزمان
    await asyncio.gather(ws_server.wait_closed(), http_server.wait_closed())

if __name__ == "__main__":
    asyncio.run(main())

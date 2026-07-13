import socket
import threading
import json
import time
import sys
import os

class GameServer:
    def __init__(self, host='0.0.0.0', port=5000):
        self.host = host
        self.port = port
        self.clients = {}  # {username: socket}
        self.lock = threading.Lock()
        
    def start(self):
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))
            server.listen(50)
            print(f"✅ Server started on {self.host}:{self.port}")
            print("🟢 Waiting for connections...")
            print("="*50)
            
            while True:
                try:
                    client_socket, address = server.accept()
                    print(f"📥 New connection from {address}")
                    threading.Thread(target=self.handle_client, args=(client_socket,), daemon=True).start()
                except Exception as e:
                    print(f"❌ Error accepting connection: {e}")
                    
        except KeyboardInterrupt:
            print("\n🛑 Server shutting down...")
            sys.exit(0)
        except Exception as e:
            print(f"❌ Server error: {e}")
            sys.exit(1)
    
    def handle_client(self, client_socket):
        username = None
        try:
            data = client_socket.recv(1024).decode()
            if not data:
                return
                
            login_data = json.loads(data)
            
            if login_data.get("type") == "login":
                username = login_data.get("username", "").strip()
                
                if not username:
                    client_socket.send(json.dumps({
                        "type": "error",
                        "message": "Invalid username!"
                    }).encode())
                    client_socket.close()
                    return
                
                with self.lock:
                    if username in self.clients:
                        client_socket.send(json.dumps({
                            "type": "error",
                            "message": "Username already taken!"
                        }).encode())
                        client_socket.close()
                        return
                    
                    self.clients[username] = client_socket
                
                client_socket.send(json.dumps({
                    "type": "login_response",
                    "status": "success"
                }).encode())
                
                print(f"✅ User '{username}' connected (Total: {len(self.clients)})")
                
                self.broadcast_user_list()
                
                while True:
                    try:
                        data = client_socket.recv(4096)
                        if not data:
                            break
                        
                        message = json.loads(data.decode())
                        self.process_message(username, message)
                        
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        print(f"⚠️ Error processing message from {username}: {e}")
                        break
                        
        except Exception as e:
            print(f"⚠️ Error handling client {username}: {e}")
            
        finally:
            if username:
                with self.lock:
                    if username in self.clients:
                        del self.clients[username]
                self.broadcast_user_list()
                print(f"❌ User '{username}' disconnected (Total: {len(self.clients)})")
            
            try:
                client_socket.close()
            except:
                pass
    
    def process_message(self, sender, message):
        msg_type = message.get("type")
        
        if msg_type == "chat_message":
            self.broadcast({
                "type": "chat_message",
                "sender": sender,
                "message": message.get("message", "")
            }, exclude=sender)
            print(f"💬 [{sender}]: {message.get('message', '')}")
            
        elif msg_type == "game_invite":
            target = message.get("target")
            if target and target in self.clients:
                try:
                    self.clients[target].send(json.dumps({
                        "type": "game_invite",
                        "sender": sender,
                        "game_name": message.get("game_name", "Unknown Game")
                    }).encode())
                    print(f"🎮 {sender} invited {target} to play {message.get('game_name', 'Unknown Game')}")
                except:
                    pass
                    
        elif msg_type == "get_users":
            self.send_user_list(sender)
    
    def broadcast(self, data, exclude=None):
        with self.lock:
            for username, client_socket in self.clients.items():
                if username != exclude:
                    try:
                        client_socket.send(json.dumps(data).encode())
                    except:
                        pass
    
    def broadcast_user_list(self):
        users = list(self.clients.keys())
        self.broadcast({
            "type": "user_list",
            "users": users
        })
        print(f"👥 Online users: {', '.join(users) if users else 'None'}")
    
    def send_user_list(self, username):
        if username in self.clients:
            try:
                self.clients[username].send(json.dumps({
                    "type": "user_list",
                    "users": list(self.clients.keys())
                }).encode())
            except:
                pass

if __name__ == "__main__":
    print("="*50)
    print("🚀 VOIDVISION GAME SERVER")
    print("="*50)
    
    port = 5000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except:
            pass
    
    server = GameServer(port=port)
    server.start()

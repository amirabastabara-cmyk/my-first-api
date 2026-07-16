import sys
import os
import json
import threading
import time
import subprocess
import hashlib
import struct
import asyncio
import logging
import ctypes
from typing import Optional, Dict, Any, List

import websocket
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
import wintun
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceCandidate,
    RTCConfiguration,
    RTCIceServer,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

# ===================== تنظیمات =====================
SERVER_IP = "my-first-api-1-bqnx.onrender.com"
SERVER_PORT = 10000
MTU = 1280
MAX_RETRY = 3

# ===================== TURN Servers =====================
TURN_SERVERS = [
    RTCIceServer(urls="stun:stun.l.google.com:19302"),
    RTCIceServer(urls="stun:stun.anyfirewall.com:3478"),
    RTCIceServer(urls="turn:openrelay.metered.ca:80", username="openrelayproject", credential="openrelayproject"),
    RTCIceServer(urls="turn:openrelay.metered.ca:443", username="openrelayproject", credential="openrelayproject"),
    RTCIceServer(urls="turn:openrelay.metered.ca:5349", username="openrelayproject", credential="openrelayproject"),
]

# ===================== لاگ =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("voidvision.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("voidvision")

# ===================== چک کردن Admin =====================
def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

# ===================== رمزنگاری با Nonce همراه Packet =====================
class CryptoManager:
    def __init__(self, key_hex: str):
        self.key = bytes.fromhex(key_hex)
        self.cipher = ChaCha20Poly1305(self.key)
        self.send_counter = 0
        self.recv_counter = 0

    def encrypt(self, data: bytes) -> bytes:
        nonce = self.send_counter.to_bytes(12, 'big')
        self.send_counter += 1
        encrypted = self.cipher.encrypt(nonce, data, None)
        return nonce + encrypted  # nonce (12 bytes) + encrypted data

    def decrypt(self, data: bytes) -> bytes:
        if len(data) < 12:
            raise ValueError("Data too short")
        nonce = data[:12]
        encrypted = data[12:]
        return self.cipher.decrypt(nonce, encrypted, None)

# ===================== Packet Fragment با Header =====================
PACKET_HEADER_FORMAT = "!BBH"  # packet_id, fragment_num, total_fragments
PACKET_HEADER_SIZE = struct.calcsize(PACKET_HEADER_FORMAT)
FRAGMENT_TIMEOUT = 5  # seconds

def fragment_packet(data: bytes, max_size: int = MTU - 100) -> List[tuple]:
    if len(data) <= max_size - PACKET_HEADER_SIZE:
        return [(0, 0, 1, data)]
    packet_id = os.urandom(1)[0]
    fragments = []
    chunk_size = max_size - PACKET_HEADER_SIZE
    total = (len(data) + chunk_size - 1) // chunk_size
    for i in range(total):
        start = i * chunk_size
        end = min(start + chunk_size, len(data))
        fragments.append((packet_id, i, total, data[start:end]))
    return fragments

def defragment_packet(fragments: List[tuple]) -> bytes:
    if not fragments:
        return b''
    # fragments: list of (packet_id, frag_num, total, data)
    sorted_frags = sorted(fragments, key=lambda x: x[1])
    return b''.join(f[3] for f in sorted_frags)

# ===================== توابع کمکی =====================
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def get_interface_index(interface_name: str) -> Optional[str]:
    try:
        result = subprocess.run(
            f'route print | findstr "{interface_name}"',
            shell=True,
            capture_output=True,
            text=True
        )
        for line in result.stdout.split('\n'):
            if interface_name in line:
                parts = line.split()
                if len(parts) >= 2:
                    return parts[0]
    except Exception as e:
        logger.error(f"Interface index error: {e}")
    return None

def enable_ip_forwarding(interface_name: str) -> bool:
    try:
        subprocess.run(
            f'netsh interface ipv4 set interface "{interface_name}" forwarding=enabled',
            shell=True,
            capture_output=True,
            check=True
        )
        logger.info(f"✅ IP Forwarding enabled for {interface_name}")
        return True
    except Exception as e:
        logger.warning(f"⚠️ IP Forwarding error: {e}")
        return False

def set_adapter_mtu(interface_name: str, mtu: int = 1280) -> bool:
    try:
        subprocess.run(
            f'netsh interface ipv4 set subinterface "{interface_name}" mtu={mtu} store=persistent',
            shell=True,
            capture_output=True,
            check=True
        )
        logger.info(f"✅ MTU set to {mtu} for {interface_name}")
        return True
    except Exception as e:
        logger.warning(f"⚠️ MTU error: {e}")
        return False

def add_route(virtual_ip: str, interface_name: str = "voidvision", gateway: str = "10.77.0.1") -> bool:
    if not virtual_ip:
        return False
    idx = get_interface_index(interface_name)
    if idx:
        try:
            subprocess.run(
                f'route add {virtual_ip} mask 255.255.255.255 {gateway} if {idx}',
                shell=True,
                capture_output=True,
                check=True
            )
            logger.info(f"✅ Route added: {virtual_ip} -> {gateway} via interface {idx}")
            return True
        except Exception as e:
            logger.warning(f"⚠️ Route error: {e}")
    try:
        subprocess.run(
            f'route add {virtual_ip} mask 255.255.255.255 {gateway}',
            shell=True,
            capture_output=True,
            check=True
        )
        logger.info(f"✅ Route added (fallback): {virtual_ip} -> {gateway}")
        return True
    except Exception as e:
        logger.warning(f"⚠️ Route error (fallback): {e}")
        return False

def remove_route(virtual_ip: str) -> bool:
    if not virtual_ip:
        return False
    try:
        subprocess.run(f'route delete {virtual_ip}', shell=True, capture_output=True, check=True)
        logger.info(f"🗑️ Route removed: {virtual_ip}")
        return True
    except Exception as e:
        logger.warning(f"⚠️ Route removal error: {e}")
        return False

def get_ping_ms(ip: str) -> Optional[int]:
    try:
        start = time.time()
        result = subprocess.run(f'ping -n 1 {ip}', shell=True, capture_output=True, text=True, timeout=2)
        if "Reply from" in result.stdout:
            import re
            match = re.search(r'time[=<](\d+)ms', result.stdout)
            if match:
                return int(match.group(1))
        return None
    except Exception:
        return None

def is_ipv4_packet(data: bytes) -> bool:
    if len(data) < 1:
        return False
    version = (data[0] >> 4) & 0xF
    return version == 4

def get_dest_ip(data: bytes) -> Optional[str]:
    if len(data) < 20:
        return None
    return f"{data[16]}.{data[17]}.{data[18]}.{data[19]}"

def get_src_ip(data: bytes) -> Optional[str]:
    if len(data) < 20:
        return None
    return f"{data[12]}.{data[13]}.{data[14]}.{data[15]}"

# ===================== Wintun Adapter =====================
class WintunAdapter:
    def __init__(self, name: str = "voidvision", ip: str = "10.77.0.1"):
        self.name = name
        self.ip = ip
        self.adapter = None
        self.session = None
        self.running = False
        self.read_callback = None
        self.lock = threading.Lock()

    def create(self) -> bool:
        try:
            # حذف adapter قبلی اگر وجود داشته باشد
            try:
                old = wintun.Adapter.open(self.name)
                if old:
                    old.close()
            except Exception:
                pass

            self.adapter = wintun.Adapter.create(self.name, "VoidVision Tunnel", None)
            if not self.adapter:
                logger.error("❌ Failed to create Wintun adapter")
                return False

            subprocess.run(
                f'netsh interface ip set address name="{self.name}" static {self.ip} 255.255.255.0',
                shell=True, capture_output=True, check=True
            )
            subprocess.run(
                f'netsh interface set interface name="{self.name}" admin=enabled',
                shell=True, capture_output=True, check=True
            )
            enable_ip_forwarding(self.name)
            set_adapter_mtu(self.name, MTU)
            self.session = self.adapter.start_session()
            self.running = True
            logger.info(f"✅ Wintun Adapter created: {self.name} -> {self.ip}")
            return True
        except Exception as e:
            logger.error(f"❌ Wintun error: {e}")
            return False

    def set_read_callback(self, callback):
        self.read_callback = callback

    def start_reading(self):
        def _read_loop():
            while self.running:
                try:
                    data = self.session.read(65535)
                    if data and self.read_callback:
                        with self.lock:
                            self.read_callback(data)
                except Exception:
                    if not self.running:
                        break
                    time.sleep(0.01)
                time.sleep(0.001)
        threading.Thread(target=_read_loop, daemon=True).start()

    def write(self, data: bytes) -> bool:
        if self.session and self.running:
            try:
                with self.lock:
                    return self.session.write(data)
            except Exception as e:
                logger.error(f"❌ Wintun write error: {e}")
                return False
        return False

    def delete(self):
        self.running = False
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass
        if self.adapter:
            try:
                self.adapter.close()
            except Exception:
                pass
        logger.info("🗑️ Wintun Adapter deleted")

# ===================== P2P Manager v8.2 =====================
class P2PManager:
    def __init__(self, network):
        self.network = network
        self.pc = None
        self.channel = None
        self.is_connected = False
        self.is_host = False
        self.loop = None
        self.loop_thread = None
        self.adapter = None
        self.room_id = None
        self.room_key = None  # اضافه شد
        self.peers = {}
        self.data_callback = None
        self.my_ip = None
        self.active = False
        self.gateway_ip = "10.77.0.1"
        self.crypto = None
        self.subnet = "10.77.0."
        self.fragment_buffer = {}
        self.fragment_timestamps = {}
        self.ping_result = None
        self.user_id = None
        self.pending_candidates = {}  # بافر کاندیدهای معلق

    def set_data_callback(self, callback):
        self.data_callback = callback

    def _start_event_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self.active = True
        while self.active:
            try:
                loop.run_forever()
            except Exception:
                time.sleep(0.1)

    def _run_coroutine(self, coro):
        if self.loop and self.active:
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        return None

    def _get_rtc_config(self):
        return RTCConfiguration(iceServers=TURN_SERVERS)

    def start_host(self, target_users: List[str], game_name: str, room_id: str, room_key: str, subnet: str):
        self.is_host = True
        self.room_id = room_id
        self.room_key = room_key
        self.subnet = subnet
        self.my_ip = subnet + "1"
        self.gateway_ip = self.my_ip
        self.crypto = CryptoManager(room_key)
        self.adapter = WintunAdapter(f"voidvision_{room_id[:6]}", self.my_ip)
        if not self.adapter.create():
            logger.error("❌ Adapter creation failed")
            return
        self.adapter.set_read_callback(self._on_adapter_read)
        self.adapter.start_reading()
        logger.info(f"✅ Host Adapter: {self.my_ip} (Subnet: {self.subnet})")

        self.loop_thread = threading.Thread(target=self._start_event_loop, daemon=True)
        self.loop_thread.start()
        time.sleep(0.5)

        # ذخیره IP هر کاربر برای reconnect
        for idx, user in enumerate(target_users, start=2):
            peer_ip = subnet + str(idx)
            add_route(peer_ip, f"voidvision_{room_id[:6]}", self.gateway_ip)
            # ذخیره IP اختصاصی برای هر کاربر
            threading.Thread(target=self._connect_to_peer, args=(user, peer_ip, game_name), daemon=True).start()

        self._start_ping_timer()

    def _connect_to_peer(self, target_user: str, peer_ip: str, game_name: str):
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            config = self._get_rtc_config()
            pc = RTCPeerConnection(configuration=config)
            channel = pc.createDataChannel("game_channel")

            @channel.on("message")
            def on_message(message):
                try:
                    if isinstance(message, str):
                        message = message.encode()
                    self._on_packet_received(message, target_user)
                except Exception as e:
                    logger.error(f"Message error: {e}")

            @pc.on("iceconnectionstatechange")
            def on_ice():
                if pc.iceConnectionState == "connected":
                    self.is_connected = True
                    logger.info(f"✅ Connected to {target_user}")
                    if self.data_callback:
                        self.data_callback(f"✅ Connected to {target_user}")
                elif pc.iceConnectionState == "failed":
                    self._reconnect_peer(target_user, game_name, peer_ip)  # ارسال IP قبلی

            @pc.on("icecandidate")
            def on_candidate(candidate):
                if candidate:
                    self.network.send_ice_candidate(target_user, {
                        "candidate": candidate.candidate,
                        "sdpMid": candidate.sdpMid or "0",
                        "sdpMLineIndex": candidate.sdpMLineIndex or 0,
                    })

            offer = loop.run_until_complete(pc.createOffer())
            loop.run_until_complete(pc.setLocalDescription(offer))
            # timeout برای ICE gathering
            start = time.time()
            while pc.iceGatheringState != "complete" and (time.time() - start) < 10:
                time.sleep(0.1)

            self.peers[target_user] = {
                "pc": pc,
                "channel": channel,
                "virtual_ip": peer_ip,
                "loop": loop,
                "username": target_user
            }
            # اگر کاندیدهای معلق برای این کاربر وجود داشت، اضافه کن
            if target_user in self.pending_candidates:
                for cand in self.pending_candidates[target_user]:
                    try:
                        future = self._run_coroutine(pc.addIceCandidate(cand))
                        if future:
                            future.result(timeout=5)
                    except Exception as e:
                        logger.error(f"Pending candidate error: {e}")
                del self.pending_candidates[target_user]

            self.network.send_offer(target_user, pc.localDescription.sdp, game_name)
            logger.info(f"📤 Offer sent to {target_user}")

        threading.Thread(target=_run, daemon=True).start()

    def _reconnect_peer(self, target_user: str, game_name: str, peer_ip: str = None):
        logger.info(f"🔄 Reconnecting to {target_user}...")
        if target_user in self.peers:
            try:
                self.peers[target_user]["pc"].close()
            except Exception:
                pass
            # حذف route قدیمی
            old_ip = self.peers[target_user].get("virtual_ip")
            if old_ip:
                remove_route(old_ip)
            del self.peers[target_user]

        # اگر IP جدید داده نشد، از subnet استفاده کن (اما بهتر است IP قبلی را داشته باشیم)
        if peer_ip is None:
            # پیدا کردن یک IP آزاد
            used_ips = [p.get("virtual_ip") for p in self.peers.values()]
            for i in range(2, 255):
                ip = self.subnet + str(i)
                if ip not in used_ips and ip != self.my_ip:
                    peer_ip = ip
                    break
            else:
                peer_ip = self.subnet + "2"  # fallback

        add_route(peer_ip, f"voidvision_{self.room_id[:6]}", self.gateway_ip)
        threading.Thread(target=self._connect_to_peer, args=(target_user, peer_ip, game_name), daemon=True).start()

    def start_client(self, from_user: str, offer_sdp: str, room_id: str, my_ip: str, room_key: str, subnet: str):
        self.room_id = room_id
        self.room_key = room_key
        self.is_host = False
        self.subnet = subnet
        self.my_ip = my_ip
        self.gateway_ip = subnet + "1"
        self.crypto = CryptoManager(room_key)
        self.adapter = WintunAdapter(f"voidvision_{room_id[:6]}", self.my_ip)
        if not self.adapter.create():
            logger.error("❌ Adapter creation failed")
            return
        self.adapter.set_read_callback(self._on_adapter_read)
        self.adapter.start_reading()
        logger.info(f"✅ Client Adapter: {self.my_ip} (Subnet: {self.subnet})")

        self.loop_thread = threading.Thread(target=self._start_event_loop, daemon=True)
        self.loop_thread.start()
        time.sleep(0.5)

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            config = self._get_rtc_config()
            pc = RTCPeerConnection(configuration=config)

            @pc.on("datachannel")
            def on_datachannel(channel):
                if from_user in self.peers:
                    self.peers[from_user]["channel"] = channel
                else:
                    self.peers[from_user] = {
                        "pc": pc,
                        "channel": channel,
                        "virtual_ip": self.my_ip,
                        "loop": loop,
                        "username": from_user
                    }

                @channel.on("message")
                def on_message(message):
                    try:
                        if isinstance(message, str):
                            message = message.encode()
                        self._on_packet_received(message, from_user)
                    except Exception as e:
                        logger.error(f"Message error: {e}")
                logger.info("✅ DataChannel established")

            @pc.on("iceconnectionstatechange")
            def on_ice():
                if pc.iceConnectionState == "connected":
                    self.is_connected = True
                    logger.info("✅ Connected to host")
                    if self.data_callback:
                        self.data_callback("✅ Connected to host")
                elif pc.iceConnectionState == "failed":
                    logger.warning("❌ Connection failed, retrying...")
                    self._reconnect_host(from_user, offer_sdp)

            @pc.on("icecandidate")
            def on_candidate(candidate):
                if candidate:
                    self.network.send_ice_candidate(from_user, {
                        "candidate": candidate.candidate,
                        "sdpMid": candidate.sdpMid or "0",
                        "sdpMLineIndex": candidate.sdpMLineIndex or 0,
                    })

            offer = RTCSessionDescription(sdp=offer_sdp, type="offer")
            loop.run_until_complete(pc.setRemoteDescription(offer))
            answer = loop.run_until_complete(pc.createAnswer())
            loop.run_until_complete(pc.setLocalDescription(answer))
            start = time.time()
            while pc.iceGatheringState != "complete" and (time.time() - start) < 10:
                time.sleep(0.1)

            if from_user not in self.peers:
                self.peers[from_user] = {
                    "pc": pc,
                    "channel": None,
                    "virtual_ip": self.my_ip,
                    "loop": loop,
                    "username": from_user
                }
            # اگر کاندیدهای معلق برای این کاربر وجود داشت، اضافه کن
            if from_user in self.pending_candidates:
                for cand in self.pending_candidates[from_user]:
                    try:
                        future = self._run_coroutine(pc.addIceCandidate(cand))
                        if future:
                            future.result(timeout=5)
                    except Exception as e:
                        logger.error(f"Pending candidate error: {e}")
                del self.pending_candidates[from_user]

            self.network.send_answer(from_user, pc.localDescription.sdp)
            logger.info("📤 Answer sent to host")

        threading.Thread(target=_run, daemon=True).start()

    def _reconnect_host(self, from_user: str, offer_sdp: str):
        if from_user in self.peers:
            try:
                self.peers[from_user]["pc"].close()
            except Exception:
                pass
            del self.peers[from_user]
        self.start_client(from_user, offer_sdp, self.room_id, self.my_ip, self.room_key, self.subnet)

    def _on_adapter_read(self, data: bytes):
        if not data:
            return

        if not is_ipv4_packet(data):
            return

        dest_ip = get_dest_ip(data)
        src_ip = get_src_ip(data)
        is_broadcast = False
        is_multicast = False

        if dest_ip:
            if dest_ip.endswith('.255') or dest_ip == '255.255.255.255':
                is_broadcast = True
            elif dest_ip.startswith('224.'):
                is_multicast = True

        encrypted = self.crypto.encrypt(data)
        fragments = fragment_packet(encrypted)

        for packet_id, frag_num, total, frag_data in fragments:
            header = struct.pack(PACKET_HEADER_FORMAT, packet_id, frag_num, total)
            packet = header + frag_data

            if is_broadcast or is_multicast:
                for username, peer in self.peers.items():
                    channel = peer.get("channel")
                    if channel and channel.readyState == "open":
                        try:
                            channel.send(packet)
                        except Exception:
                            pass
            else:
                sent = False
                for username, peer in self.peers.items():
                    if peer.get("virtual_ip") == dest_ip:
                        channel = peer.get("channel")
                        if channel and channel.readyState == "open":
                            try:
                                channel.send(packet)
                                sent = True
                            except Exception:
                                pass
                        break
                if not sent:
                    # fallback: ارسال به همه
                    for username, peer in self.peers.items():
                        channel = peer.get("channel")
                        if channel and channel.readyState == "open":
                            try:
                                channel.send(packet)
                            except Exception:
                                pass

    def _on_packet_received(self, data: bytes, from_user: str):
        try:
            # بررسی Ping/Heartbeat (اختیاری)
            if len(data) >= 4 and data[:4] == b'PING':
                channel = self.peers.get(from_user, {}).get("channel")
                if channel and channel.readyState == "open":
                    channel.send(b'PONG')
                return
            if len(data) >= 4 and data[:4] == b'PONG':
                self.ping_result = 10
                if self.data_callback:
                    self.data_callback("📶 Ping: ~10ms")
                return

            if len(data) < PACKET_HEADER_SIZE:
                return
            packet_id, frag_num, total = struct.unpack(PACKET_HEADER_FORMAT, data[:PACKET_HEADER_SIZE])
            frag_data = data[PACKET_HEADER_SIZE:]

            key = (from_user, packet_id)
            if key not in self.fragment_buffer:
                self.fragment_buffer[key] = [None] * total
                self.fragment_timestamps[key] = time.time()

            # حذف اگر timeout شده باشد
            if time.time() - self.fragment_timestamps.get(key, 0) > FRAGMENT_TIMEOUT:
                del self.fragment_buffer[key]
                del self.fragment_timestamps[key]
                return

            self.fragment_buffer[key][frag_num] = frag_data

            if all(x is not None for x in self.fragment_buffer[key]):
                # ساخت لیست کامل قطعات با فرمت صحیح
                fragments_list = [
                    (packet_id, i, total, self.fragment_buffer[key][i])
                    for i in range(total)
                ]
                full_data = defragment_packet(fragments_list)
                del self.fragment_buffer[key]
                del self.fragment_timestamps[key]
                try:
                    decrypted = self.crypto.decrypt(full_data)
                    if self.adapter:
                        self.adapter.write(decrypted)
                except Exception as e:
                    logger.error(f"Decrypt error: {e}")
        except Exception as e:
            logger.error(f"Packet receive error: {e}")

    def _start_ping_timer(self):
        def _ping_loop():
            while self.is_connected:
                target_ip = self.subnet + "2" if not self.is_host else self.subnet + "1"
                ping = get_ping_ms(target_ip)
                if ping is not None:
                    self.ping_result = ping
                    if self.data_callback:
                        self.data_callback(f"📶 Ping: {ping}ms")
                time.sleep(2)
        threading.Thread(target=_ping_loop, daemon=True).start()

    def handle_answer(self, from_user: str, answer_sdp: str):
        if from_user in self.peers:
            try:
                pc = self.peers[from_user]["pc"]
                answer = RTCSessionDescription(sdp=answer_sdp, type="answer")
                future = self._run_coroutine(pc.setRemoteDescription(answer))
                if future:
                    future.result(timeout=5)
                logger.info(f"✅ Answer applied for {from_user}")
            except Exception as e:
                logger.error(f"❌ Answer error: {e}")

    def handle_ice_candidate(self, from_user: str, candidate_dict: dict):
        # اگر peer وجود نداشته باشد، کاندید را بافر کن
        if from_user not in self.peers:
            if from_user not in self.pending_candidates:
                self.pending_candidates[from_user] = []
            # ایجاد شیء کاندید
            try:
                candidate = RTCIceCandidate(
                    candidate=candidate_dict.get("candidate", ""),
                    sdpMid=candidate_dict.get("sdpMid", "0"),
                    sdpMLineIndex=candidate_dict.get("sdpMLineIndex", 0)
                )
                self.pending_candidates[from_user].append(candidate)
                logger.info(f"🧊 Pending ICE candidate for {from_user}")
            except Exception as e:
                logger.error(f"Pending candidate creation error: {e}")
            return

        # اگر peer وجود دارد، مستقیماً اضافه کن
        try:
            pc = self.peers[from_user]["pc"]
            candidate = RTCIceCandidate(
                candidate=candidate_dict.get("candidate", ""),
                sdpMid=candidate_dict.get("sdpMid", "0"),
                sdpMLineIndex=candidate_dict.get("sdpMLineIndex", 0)
            )
            future = self._run_coroutine(pc.addIceCandidate(candidate))
            if future:
                future.result(timeout=5)
            logger.info(f"🧊 ICE added for {from_user}")
        except Exception as e:
            logger.error(f"❌ ICE error: {e}")

    def disconnect(self):
        self.active = False
        self.is_connected = False
        for username, peer in list(self.peers.items()):
            pc = peer.get("pc")
            if pc:
                try:
                    pc.close()
                except Exception:
                    pass
            remove_route(peer.get("virtual_ip", ""))
        self.peers.clear()
        self.pending_candidates.clear()
        if self.adapter:
            self.adapter.delete()
        if self.loop:
            try:
                self.loop.stop()
            except Exception:
                pass
        logger.info("🔴 Disconnected")
        if self.data_callback:
            self.data_callback("🔴 Disconnected")

    def get_ping(self) -> Optional[int]:
        return self.ping_result

# ===================== NetworkManager =====================
class NetworkManager(QObject):
    login_signal = pyqtSignal(dict)
    register_signal = pyqtSignal(dict)
    user_list_signal = pyqtSignal(list)
    chat_signal = pyqtSignal(str, str)
    room_list_signal = pyqtSignal(list)
    room_created_signal = pyqtSignal(dict)
    room_joined_signal = pyqtSignal(dict)
    room_players_signal = pyqtSignal(dict)
    offer_received_signal = pyqtSignal(str, str, str)
    answer_received_signal = pyqtSignal(str, str)
    ice_candidate_received_signal = pyqtSignal(str, dict)

    def __init__(self):
        super().__init__()
        self.ws = None
        self.connected = False
        self.logged_in = False
        self.username = ""
        self.user_id = ""
        self.online_users = []
        self.rooms = []
        self.current_room = None
        self.reconnect_count = 0
        self.max_reconnect = MAX_RETRY
        self.ws_lock = threading.Lock()
        self._password = ""  # ذخیره موقت برای reconnect

    def connect_to_server(self) -> bool:
        try:
            ws_url = f"wss://{SERVER_IP}/ws"
            self.ws = websocket.WebSocketApp(ws_url,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close
            )
            threading.Thread(target=self.ws.run_forever, daemon=True).start()
            time.sleep(1.5)
            return True
        except Exception as e:
            logger.error(f"Connect error: {e}")
            return False

    def on_open(self, ws):
        self.connected = True
        self.reconnect_count = 0
        logger.info("✅ WebSocket connected")

    def on_message(self, ws, message):
        # پردازش پیام در Thread جدا برای جلوگیری از blocking
        def process():
            try:
                data = json.loads(message)
                msg_type = data.get("type")

                if msg_type == "login_response":
                    if data.get("success"):
                        self.logged_in = True
                        self.username = data.get("username", "")
                        self.user_id = data.get("user_id", "")
                    self.login_signal.emit(data)

                elif msg_type == "register_response":
                    self.register_signal.emit(data)

                elif msg_type == "user_list":
                    self.online_users = data.get("users", [])
                    self.user_list_signal.emit(self.online_users)

                elif msg_type == "chat_message":
                    self.chat_signal.emit(data.get("sender"), data.get("message"))

                elif msg_type == "room_list":
                    self.rooms = data.get("rooms", [])
                    self.room_list_signal.emit(self.rooms)

                elif msg_type == "room_created":
                    self.current_room = data.get("room_id")
                    self.room_created_signal.emit(data.get("room", {}))

                elif msg_type == "room_joined":
                    self.current_room = data.get("room_id")
                    self.room_joined_signal.emit(data.get("room", {}))

                elif msg_type == "room_players":
                    self.room_players_signal.emit(data)

                elif msg_type == "offer_received":
                    self.offer_received_signal.emit(data.get("from"), data.get("sdp"), data.get("game_name"))

                elif msg_type == "answer_received":
                    self.answer_received_signal.emit(data.get("from"), data.get("sdp"))

                elif msg_type == "ice_candidate_received":
                    self.ice_candidate_received_signal.emit(data.get("from"), data.get("candidate"))

            except Exception as e:
                logger.error(f"WS message error: {e}")
        threading.Thread(target=process, daemon=True).start()

    def on_error(self, ws, error):
        self.connected = False
        logger.error(f"WS error: {error}")
        self._reconnect()

    def on_close(self, ws, close_status_code, close_msg):
        self.connected = False
        self.logged_in = False
        logger.warning("WS closed")
        self._reconnect()

    def _reconnect(self):
        if self.reconnect_count < self.max_reconnect:
            self.reconnect_count += 1
            logger.info(f"🔄 Reconnecting... ({self.reconnect_count}/{self.max_reconnect})")
            time.sleep(2)
            self.connect_to_server()
            if self.logged_in and self._password:
                self.login(self.username, self._password)

    def send(self, data: dict) -> bool:
        with self.ws_lock:
            if self.ws and self.connected:
                try:
                    self.ws.send(json.dumps(data))
                    return True
                except Exception as e:
                    logger.error(f"Send error: {e}")
            return False

    def login(self, username: str, password: str):
        self._password = password
        if self.connected:
            self.send({"type": "login", "username": username, "password": hash_password(password)})

    def register(self, username: str, password: str):
        if self.connected:
            self.send({"type": "register", "username": username, "password": hash_password(password)})

    def send_chat_message(self, message: str):
        if self.logged_in:
            self.send({"type": "chat_message", "message": message})

    def create_room(self, game_name: str):
        if self.logged_in:
            self.send({"type": "create_room", "game_name": game_name})

    def join_room(self, room_id: str):
        if self.logged_in:
            self.send({"type": "join_room", "room_id": room_id})

    def leave_room(self):
        if self.logged_in:
            self.send({"type": "leave_room"})

    def get_room_list(self):
        if self.logged_in:
            self.send({"type": "get_room_list"})

    def send_offer(self, target_user: str, sdp: str, game_name: str):
        if self.logged_in:
            self.send({"type": "offer", "target": target_user, "sdp": sdp, "game_name": game_name})

    def send_answer(self, target_user: str, sdp: str):
        if self.logged_in:
            self.send({"type": "answer", "target": target_user, "sdp": sdp})

    def send_ice_candidate(self, target_user: str, candidate: dict):
        if self.logged_in:
            self.send({"type": "ice_candidate", "target": target_user, "candidate": candidate})

    def disconnect(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

# ===================== پنجره اصلی =====================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        # چک کردن Admin
        if not is_admin():
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Warning)
            msg.setWindowTitle("Admin Required")
            msg.setText("VoidVision needs Administrator privileges to create virtual network adapter.")
            msg.setInformativeText("Please restart the application as Administrator.")
            msg.setStandardButtons(QMessageBox.Ok)
            msg.exec_()
            sys.exit(1)

        self.network = NetworkManager()
        self.p2p = P2PManager(self.network)
        self.p2p.set_data_callback(self.on_p2p_status)
        self.games = self.load_games()
        self.current_room = None
        self.my_ip = None
        self.room_key = None
        self.subnet = None
        self.setup_ui()
        self.connect_signals()
        self.load_games_ui()

    def setup_ui(self):
        self.setWindowTitle("VoidVision Launcher v8.2")
        self.setGeometry(100, 100, 1200, 750)
        self.setStyleSheet("background: #1a1a1a; color: white;")

        top_bar = QWidget()
        top_layout = QHBoxLayout(top_bar)
        title = QLabel("✦ VOIDVISION LAUNCHER v8.2")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #00ff88;")
        top_layout.addWidget(title)
        top_layout.addStretch()
        self.status_label = QLabel("🔴 Disconnected")
        top_layout.addWidget(self.status_label)
        self.ping_label = QLabel("")
        self.ping_label.setStyleSheet("color: #888; font-size: 12px;")
        top_layout.addWidget(self.ping_label)

        main = QWidget()
        main_layout = QHBoxLayout(main)

        left = QWidget()
        left.setFixedWidth(200)
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("🎮 GAMES"))
        self.game_combo = QComboBox()
        left_layout.addWidget(self.game_combo)
        self.add_game_btn = QPushButton("➕ Add Game")
        self.add_game_btn.clicked.connect(self.add_game)
        left_layout.addWidget(self.add_game_btn)
        self.launch_btn = QPushButton("🚀 LAUNCH GAME")
        self.launch_btn.setStyleSheet("background: #00ff88; color: black; font-weight: bold;")
        self.launch_btn.clicked.connect(self.launch_game)
        left_layout.addWidget(self.launch_btn)
        self.test_ping_btn = QPushButton("📡 Test Ping")
        self.test_ping_btn.clicked.connect(self.test_ping)
        left_layout.addWidget(self.test_ping_btn)
        self.ip_label = QLabel("IP: --")
        self.ip_label.setStyleSheet("color: #aaa; font-size: 12px;")
        left_layout.addWidget(self.ip_label)
        left_layout.addStretch()

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.addWidget(QLabel("👥 Online Users"))
        self.user_list = QListWidget()
        center_layout.addWidget(self.user_list)
        center_layout.addWidget(QLabel("💬 Chat"))
        self.chat_display = QTextEdit()
        self.chat_display.setReadOnly(True)
        center_layout.addWidget(self.chat_display)
        chat_input = QHBoxLayout()
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("Type a message...")
        self.chat_input.returnPressed.connect(self.send_message)
        chat_input.addWidget(self.chat_input)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send_message)
        chat_input.addWidget(self.send_btn)
        center_layout.addLayout(chat_input)

        room_btns = QHBoxLayout()
        self.create_room_btn = QPushButton("🏠 Create Room")
        self.create_room_btn.clicked.connect(self.create_room)
        room_btns.addWidget(self.create_room_btn)
        self.join_room_btn = QPushButton("🔗 Join Room")
        self.join_room_btn.clicked.connect(self.join_room)
        room_btns.addWidget(self.join_room_btn)
        self.leave_room_btn = QPushButton("🚪 Leave Room")
        self.leave_room_btn.clicked.connect(self.leave_room)
        room_btns.addWidget(self.leave_room_btn)
        center_layout.addLayout(room_btns)

        right = QWidget()
        right.setFixedWidth(250)
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("🏠 Rooms"))
        self.room_list = QListWidget()
        right_layout.addWidget(self.room_list)
        right_layout.addWidget(QLabel("👫 Friends"))
        self.friend_list = QListWidget()
        right_layout.addWidget(self.friend_list)
        right_layout.addWidget(QLabel("📩 Friend Requests"))
        self.request_list = QListWidget()
        right_layout.addWidget(self.request_list)
        friend_add = QHBoxLayout()
        self.friend_input = QLineEdit()
        self.friend_input.setPlaceholderText("Username")
        friend_add.addWidget(self.friend_input)
        self.add_friend_btn = QPushButton("➕")
        self.add_friend_btn.clicked.connect(self.add_friend)
        friend_add.addWidget(self.add_friend_btn)
        right_layout.addLayout(friend_add)
        self.accept_btn = QPushButton("✅ Accept")
        self.accept_btn.clicked.connect(self.accept_friend)
        right_layout.addWidget(self.accept_btn)

        main_layout.addWidget(left)
        main_layout.addWidget(center, stretch=2)
        main_layout.addWidget(right)

        self.login_widget = QWidget()
        login_layout = QVBoxLayout(self.login_widget)
        login_layout.setAlignment(Qt.AlignCenter)
        login_layout.addWidget(QLabel("✦ VOIDVISION v8.2", styleSheet="font-size: 48px; font-weight: bold; color: #00ff88;"))
        login_layout.addWidget(QLabel("Login / Register", styleSheet="font-size: 18px; color: #888;"))

        form = QWidget()
        form.setFixedWidth(300)
        form_layout = QVBoxLayout(form)
        self.login_username = QLineEdit()
        self.login_username.setPlaceholderText("Username")
        form_layout.addWidget(self.login_username)
        self.login_password = QLineEdit()
        self.login_password.setPlaceholderText("Password")
        self.login_password.setEchoMode(QLineEdit.Password)
        form_layout.addWidget(self.login_password)
        self.login_status = QLabel("")
        self.login_status.setStyleSheet("color: #ff6666;")
        form_layout.addWidget(self.login_status)

        btn_layout = QHBoxLayout()
        login_btn = QPushButton("Login")
        login_btn.clicked.connect(self.handle_login)
        login_btn.setStyleSheet("background: #00ff88; color: black; font-weight: bold;")
        register_btn = QPushButton("Register")
        register_btn.clicked.connect(self.handle_register)
        btn_layout.addWidget(login_btn)
        btn_layout.addWidget(register_btn)
        form_layout.addLayout(btn_layout)

        login_layout.addWidget(form)
        login_layout.addStretch()

        self.stack = QStackedWidget()
        self.stack.addWidget(self.login_widget)
        self.stack.addWidget(main)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.addWidget(top_bar)
        central_layout.addWidget(self.stack)
        self.setCentralWidget(central)

    def connect_signals(self):
        self.network.login_signal.connect(self.on_login_response)
        self.network.register_signal.connect(self.on_register_response)
        self.network.user_list_signal.connect(self.on_user_list)
        self.network.chat_signal.connect(self.on_chat_message)
        self.network.room_list_signal.connect(self.on_room_list)
        self.network.room_created_signal.connect(self.on_room_created)
        self.network.room_joined_signal.connect(self.on_room_joined)
        self.network.room_players_signal.connect(self.on_room_players)
        self.network.offer_received_signal.connect(self.on_offer_received)
        self.network.answer_received_signal.connect(self.on_answer_received)
        self.network.ice_candidate_received_signal.connect(self.on_ice_candidate_received)

    def load_games(self) -> dict:
        try:
            with open("games.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.error(f"Load games error: {e}")
            return {}

    def save_games(self):
        try:
            with open("games.json", "w", encoding="utf-8") as f:
                json.dump(self.games, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Save games error: {e}")

    def load_games_ui(self):
        self.game_combo.clear()
        self.game_combo.addItems(list(self.games.keys()))

    def add_game(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Game", "", "*.exe")
        if path:
            name = os.path.basename(path).replace(".exe", "")
            self.games[name] = path
            self.save_games()
            self.load_games_ui()

    def launch_game(self):
        if not self.p2p.is_connected:
            QMessageBox.warning(self, "Not Connected", "VPN is not ready. Please wait for connection.")
            return
        name = self.game_combo.currentText()
        if name in self.games:
            path = self.games[name]
            try:
                subprocess.Popen(path, cwd=os.path.dirname(path))
                logger.info(f"🚀 Launched {name}")
            except Exception as e:
                QMessageBox.critical(self, "Launch Error", f"Failed to launch game: {e}")

    def test_ping(self):
        if self.p2p.is_connected:
            ping = self.p2p.get_ping()
            if ping is not None:
                self.ping_label.setText(f"📶 Ping: {ping}ms")
                self.ping_label.setStyleSheet("color: #00ff88;")
            else:
                self.ping_label.setText("❌ Ping Failed")
                self.ping_label.setStyleSheet("color: #ff4444;")
        else:
            QMessageBox.warning(self, "Not Connected", "P2P connection not established yet!")

    def on_p2p_status(self, message: str):
        self.status_label.setText(f"🟢 {message}")

    def handle_login(self):
        username = self.login_username.text().strip()
        password = self.login_password.text().strip()
        if username and len(password) >= 4:
            self.network.connect_to_server()
            QTimer.singleShot(500, lambda: self.network.login(username, password))
        else:
            self.login_status.setText("Invalid username or password (min 4 chars)")

    def handle_register(self):
        username = self.login_username.text().strip()
        password = self.login_password.text().strip()
        if username and len(password) >= 4:
            self.network.connect_to_server()
            QTimer.singleShot(500, lambda: self.network.register(username, password))
        else:
            self.login_status.setText("Invalid username or password (min 4 chars)")

    def on_login_response(self, data):
        if data.get("success"):
            self.login_status.setText("")
            self.status_label.setText(f"🟢 {self.network.username}")
            self.stack.setCurrentIndex(1)
            self.network.get_room_list()
        else:
            self.login_status.setText(f"❌ {data.get('message')}")

    def on_register_response(self, data):
        if data.get("success"):
            self.login_status.setText("✅ Registration successful! Please login.")
        else:
            self.login_status.setText(f"❌ {data.get('message')}")

    def send_message(self):
        msg = self.chat_input.text().strip()
        if msg:
            self.network.send_chat_message(msg)
            self.chat_input.clear()

    def on_chat_message(self, sender, message):
        self.chat_display.append(f"[{sender}]: {message}")

    def on_user_list(self, users):
        self.user_list.clear()
        for user in users:
            if user != self.network.username:
                self.user_list.addItem(user)

    def on_room_list(self, rooms):
        self.room_list.clear()
        for room in rooms:
            item = f"{room['room_id']} ({room.get('game', 'Unknown')}) [{room.get('count', 0)}/{room.get('max', 8)}]"
            self.room_list.addItem(item)

    def create_room(self):
        game = self.game_combo.currentText()
        if game:
            self.network.create_room(game)

    def join_room(self):
        selected = self.room_list.currentItem()
        if selected:
            room_id = selected.text().split(" ")[0]
            self.network.join_room(room_id)

    def leave_room(self):
        self.network.leave_room()
        self.p2p.disconnect()

    def on_room_created(self, room):
        self.current_room = room.get("room_id")
        self.room_key = room.get("room_key", "")
        self.subnet = room.get("subnet", "10.77.0.")
        QMessageBox.information(self, "Room Created", f"Room {self.current_room} created!\nSubnet: {self.subnet}")

    def on_room_joined(self, room):
        self.current_room = room.get("room_id")
        self.room_key = room.get("room_key", "")
        self.subnet = room.get("subnet", "10.77.0.")
        QMessageBox.information(self, "Room Joined", f"You joined room {self.current_room}\nSubnet: {self.subnet}")

    def on_room_players(self, data):
        players = data.get("players", [])
        ip_map = data.get("ips", {})
        host = data.get("host")
        self.subnet = data.get("subnet", "10.77.0.")
        self.room_key = data.get("room_key", "")

        my_user_id = self.network.user_id
        if my_user_id in ip_map:
            self.my_ip = ip_map[my_user_id]
        else:
            self.my_ip = self.subnet + "1"
        self.ip_label.setText(f"IP: {self.my_ip}")

        if my_user_id == host:
            others = [p for p in players if p != my_user_id]
            self.p2p.start_host(others, self.game_combo.currentText(), self.current_room, self.room_key, self.subnet)
            QMessageBox.information(self, "Hosting", f"Hosting started!\nYour IP: {self.my_ip}\nSubnet: {self.subnet}\nPlayers: {len(players)}")
        else:
            pass

    def on_offer_received(self, from_user, sdp, game_name):
        reply = QMessageBox.question(self, "Game Invitation", f"{from_user} invites you to play {game_name}. Accept?", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.p2p.start_client(from_user, sdp, self.current_room, self.my_ip, self.room_key, self.subnet)
            QMessageBox.information(self, "Client", f"Connected to host!\nYour IP: {self.my_ip}\nSubnet: {self.subnet}")

    def on_answer_received(self, from_user, answer_sdp):
        self.p2p.handle_answer(from_user, answer_sdp)

    def on_ice_candidate_received(self, from_user, candidate):
        self.p2p.handle_ice_candidate(from_user, candidate)

    def add_friend(self):
        username = self.friend_input.text().strip()
        if username:
            self.network.send_chat_message(f"📨 Friend request to {username}")

    def accept_friend(self):
        selected = self.request_list.currentItem()
        if selected:
            friend_id = int(selected.text().split(" ")[0])
            self.network.send_chat_message(f"✅ Accept friend request (ID: {friend_id})")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())                                                            خب این اوکیه ؟ یا هنوز مشکل داره ؟

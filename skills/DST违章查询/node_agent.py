#!/usr/bin/env python3
"""node_agent.py — DST Skill 守护进程，替代 Electron tray app。

作为 systemd 服务运行在每台查询服务器上，负责：
- WebSocket 长连接中央控制台
- 接收任务指令 → spawn Claude Code 子进程
- 实时解析进度并上报
- 任务完成自动同步 SQLite → 中央 MySQL
- 定时上报保活状态
- 响应云控指令（触发扫码登录等）
- 启动时检查配置，未配置则提醒用户通过交互式对话完成

用法:
  python3 node_agent.py              # 启动守护进程
  python3 node_agent.py --setup      # 交互式配置
  python3 node_agent.py --health     # 健康检查
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Ensure lib/ is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.sync import read_node_config, write_node_config, check_cloud_connectivity, export_for_sync


# ── Configuration ──────────────────────────────────────────────

WEBSOCKET_RECONNECT_DELAY = 5   # seconds
HEARTBEAT_INTERVAL = 30          # seconds, 设备心跳上报
KEEPALIVE_REPORT_INTERVAL = 60   # seconds, 保活状态上报
SYNC_INTERVAL = 1800             # 30 minutes, 默认周期性同步（控制台可通过 register_ack 下发覆盖）
REGISTER_TIMEOUT = 15            # seconds, 等待 register_ack 超时
CLAUDE_PATH = "claude"


# ── Setup ───────────────────────────────────────────────────────

def run_setup():
    """Interactive or CLI-driven setup of node_config.json."""
    config = read_node_config()
    if config:
        print(f"当前配置:")
        print(f"  设备 ID: {config.get('node_id')}")
        print(f"  控制台地址: {config.get('cloud_ws_url')}")
        print(f"  配置时间: {config.get('setup_at')}")
        overwrite = input("是否覆盖已有配置? [y/N] ").strip().lower()
        if overwrite != 'y':
            print("保持现有配置。")
            return

    # Support CLI args for non-interactive setup
    args = sys.argv[2:] if len(sys.argv) > 2 else []
    node_id = ""
    cloud_ws_url = ""

    i = 0
    while i < len(args):
        if args[i] == "--node-id" and i + 1 < len(args):
            node_id = args[i + 1]; i += 2
        elif args[i] == "--cloud-ws-url" and i + 1 < len(args):
            cloud_ws_url = args[i + 1]; i += 2
        else:
            i += 1

    if not node_id:
        node_id = input("设备 ID (与控制台预注册的名称一致): ").strip()
    if not cloud_ws_url:
        cloud_ws_url = input("中央控制台 WebSocket 地址 (如 ws://10.0.1.5:3001/ws?client=node): ").strip()

    if not node_id or not cloud_ws_url:
        print("❌ 设备 ID 和控制台地址不能为空")
        sys.exit(1)

    write_node_config(node_id, cloud_ws_url)
    print(f"✅ 配置已保存")

    # Connectivity check
    ok, err = check_cloud_connectivity(cloud_ws_url)
    if ok:
        print(f"✅ 控制台连接正常")
    else:
        print(f"⚠️  警告: 无法连接控制台: {err}")
        print(f"   配置已保存，请确认控制台已启动后重新运行 node_agent")


def check_health():
    """Health check - check config and connectivity."""
    config = read_node_config()
    if not config:
        print("❌ 未配置。请运行: python3 node_agent.py --setup")
        sys.exit(1)

    print(f"✅ 设备 ID: {config['node_id']}")
    print(f"   控制台: {config['cloud_ws_url']}")

    ok, err = check_cloud_connectivity(config['cloud_ws_url'])
    if ok:
        print(f"✅ 控制台可达")
    else:
        print(f"❌ 控制台不可达: {err}")
        sys.exit(1)


def run_sync_now():
    """One-shot: export all local data and push to central console via WebSocket."""
    import asyncio, sqlite3, time
    from lib.db import _get_db_path, _init_db

    config = read_node_config()
    if not config:
        print("❌ 未配置，请先运行 --setup")
        sys.exit(1)

    node_id = config["node_id"]
    cloud_ws_url = config["cloud_ws_url"]

    # Check connectivity first
    ok, err = check_cloud_connectivity(cloud_ws_url)
    if not ok:
        print(f"❌ 控制台不可达: {err}")
        sys.exit(1)

    # Gather all company names from local DB
    _init_db()
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT DISTINCT name FROM companies")
    company_names = [r[0] for r in cur.fetchall()]
    conn.close()

    if not company_names:
        print("⚠️ 本地数据库无公司数据，跳过同步")
        sys.exit(0)

    print(f"📦 准备同步 {len(company_names)} 家公司...")

    async def _do_sync():
        # Parse URL
        url = cloud_ws_url.replace("ws://", "").replace("wss://", "").replace("http://", "").replace("https://", "")
        if "/" in url:
            host_part, path = url.split("/", 1)
            path = "/" + path
        else:
            host_part, path = url, "/"
        if ":" in host_part:
            host, port_str = host_part.rsplit(":", 1)
            port = int(port_str)
        else:
            host, port = host_part, 80

        reader, writer = await asyncio.open_connection(host, port)

        # WebSocket upgrade handshake
        ws_key = os.urandom(16)
        ws_key_b64 = __import__('base64').b64encode(ws_key).decode('ascii')
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key_b64}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        writer.write(request.encode())
        await writer.drain()

        # Read HTTP upgrade response
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = await reader.read(1024)
            if not chunk:
                print("❌ WebSocket 握手失败: 连接关闭")
                return
            response += chunk

        if b"101" not in response:
            first_line = response.split(b'\r\n')[0].decode()
            print(f"❌ WebSocket 握手失败: {first_line}")
            return
        print("✅ WebSocket 已连接")

        def _send_frame(w, payload_bytes):
            frame = bytearray()
            frame.append(0x81)
            length = len(payload_bytes)
            if length < 126:
                frame.append(length | 0x80)
            elif length < 65536:
                frame.append(126 | 0x80)
                frame.extend(length.to_bytes(2, "big"))
            else:
                frame.append(127 | 0x80)
                frame.extend(length.to_bytes(8, "big"))
            mask_key = os.urandom(4)
            frame.extend(mask_key)
            masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload_bytes))
            frame.extend(masked)
            w.write(bytes(frame))

        async def _recv_frame(r):
            header = await asyncio.wait_for(r.readexactly(2), timeout=10)
            opcode = header[0] & 0x0F
            masked = header[1] & 0x80
            length = header[1] & 0x7F
            if opcode == 0x08:
                return None
            if opcode == 0x09:
                return None
            if length == 126:
                length = int.from_bytes(await r.readexactly(2), "big")
            elif length == 127:
                length = int.from_bytes(await r.readexactly(8), "big")
            if masked:
                mask_key = await r.readexactly(4)
            payload = await r.readexactly(length)
            if masked:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            try:
                return json.loads(payload.decode("utf-8"))
            except Exception:
                return None

        # Send sync for each company
        total = {"companies": 0, "vehicles": 0, "violations": 0}
        for i, company_name in enumerate(company_names):
            result = export_for_sync(company_name=company_name, node_id=node_id)
            if not result.get("ok"):
                print(f"  ⚠️ {company_name}: 导出失败 - {result.get('error')}")
                continue

            n_v = len(result.get("vehicles", []))
            n_vi = len(result.get("violations", []))
            if n_v == 0 and n_vi == 0:
                print(f"  ⏭️ {company_name}: 无数据")
                continue

            msg = {
                "type": "sync_data",
                "company_name": company_name,
                "node_id": node_id,
                "hostname": socket.gethostname(),
                "companies": result["companies"],
                "vehicles": result["vehicles"],
                "violations": result["violations"],
            }
            _send_frame(writer, json.dumps(msg, ensure_ascii=False).encode("utf-8"))
            await writer.drain()
            print(f"  📤 {company_name}: {len(result['companies'])}司 / {n_v}车 / {n_vi}违章")

            total["companies"] += len(result["companies"])
            total["vehicles"] += n_v
            total["violations"] += n_vi

            # Wait for sync_ack
            try:
                ack = await _recv_frame(reader)
                if ack and ack.get("type") == "sync_ack":
                    status = "✅" if ack.get("ok") else "❌"
                    print(f"    {status} 控制台确认: {ack.get('stats', ack.get('message', ''))}")
            except Exception as e:
                print(f"    ⚠️ 未收到确认: {e}")

            if i < len(company_names) - 1:
                await asyncio.sleep(1)

        # Graceful close
        close_frame = bytearray([0x88, 0x80, 0, 0, 0, 0])  # opcode=0x08, masked, no payload
        writer.write(bytes(close_frame))
        await writer.drain()
        writer.close()
        # wait_closed() not available in Python < 3.8
        if hasattr(writer, 'wait_closed'):
            await writer.wait_closed()

        return total

    loop = asyncio.new_event_loop()
    try:
        totals = loop.run_until_complete(_do_sync())
        if totals:
            print(f"\n✅ 同步完成: {totals['companies']}司 / {totals['vehicles']}车 / {totals['violations']}违章")
    except Exception as e:
        print(f"❌ 同步失败: {e}")
        sys.exit(1)
    finally:
        loop.close()


def run_report_keepalive():
    """One-shot: gather keepalive status and push to central console via WebSocket."""
    import asyncio, sqlite3
    from lib.db import _get_db_path, _init_db
    from lib.core import _get_data_dir

    config = read_node_config()
    if not config:
        print("❌ 未配置，请先运行 --setup")
        sys.exit(1)

    node_id = config["node_id"]
    cloud_ws_url = config["cloud_ws_url"]

    ok, err = check_cloud_connectivity(cloud_ws_url)
    if not ok:
        print(f"❌ 控制台不可达: {err}")
        sys.exit(1)

    # Gather keepalive status
    _init_db()
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        print("⚠️ 数据库不存在")
        sys.exit(0)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT * FROM profiles")
    profiles = [dict(r) for r in cur.fetchall()]
    conn.close()

    if not profiles:
        print("⚠️ 无 Profile 数据")
        sys.exit(0)

    companies_status = []
    for p in profiles:
        status = {
            "company_name": p.get("company_name", ""),
            "profile_name": p.get("profile_name", ""),
            "is_logged_in": bool(p.get("is_logged_in", 0)),
        }

        # Check keepalive systemd service
        profile_name = p.get("profile_name", "")
        service_name = f"keepalive-{profile_name}"
        try:
            r = subprocess.run(
                ["systemctl", "--user", "is-active", service_name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', timeout=5
            )
            status["keepalive_alive"] = r.stdout.strip() == "active"
        except Exception:
            status["keepalive_alive"] = False

        # Read health file
        health_file = os.path.join(_get_data_dir(), f"keepalive_health_{p['company_name']}.json")
        if os.path.exists(health_file):
            try:
                with open(health_file, "r") as f:
                    health = json.load(f)
                status["last_cycle"] = health.get("last_check", "")
                status["cycle_count"] = health.get("cycle_count", 0)
                status["health_state"] = health.get("state", "unknown")
            except Exception:
                status["last_cycle"] = None
                status["health_state"] = "error"
        else:
            status["last_cycle"] = None
            status["health_state"] = "no_health_file"

        companies_status.append(status)

    print(f"📦 准备上报 {len(companies_status)} 个公司保活状态:")
    for s in companies_status:
        alive = "✅" if s["keepalive_alive"] else "❌"
        login = "✅" if s["is_logged_in"] else "❌"
        print(f"  {alive} {s['company_name']}: keepalive={'alive' if s['keepalive_alive'] else 'dead'} login={login}")

    async def _do_report():
        url = cloud_ws_url.replace("ws://", "").replace("wss://", "").replace("http://", "").replace("https://", "")
        if "/" in url:
            host_part, path = url.split("/", 1)
            path = "/" + path
        else:
            host_part, path = url, "/"
        if ":" in host_part:
            host, port_str = host_part.rsplit(":", 1)
            port = int(port_str)
        else:
            host, port = host_part, 80

        reader, writer = await asyncio.open_connection(host, port)

        ws_key = os.urandom(16)
        ws_key_b64 = __import__('base64').b64encode(ws_key).decode('ascii')
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key_b64}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        writer.write(request.encode())
        await writer.drain()

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = await reader.read(1024)
            if not chunk:
                print("❌ WebSocket 握手失败")
                return
            response += chunk

        if b"101" not in response:
            first_line = response.split(b'\r\n')[0].decode()
            print(f"❌ WebSocket 握手失败: {first_line}")
            return
        print("✅ WebSocket 已连接")

        # Build and send keepalive_status messages — one per company
        hostname = socket.gethostname()
        for cs in companies_status:
            msg = {
                "type": "keepalive_status",
                "company_name": cs["company_name"],
                "node_id": node_id,
                "hostname": hostname,
                "is_logged_in": cs["is_logged_in"],
                "keepalive_alive": cs["keepalive_alive"],
                "last_cycle": cs.get("last_cycle"),
                "health_state": cs.get("health_state"),
            }
            payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")

            frame = bytearray()
            frame.append(0x81)
            length = len(payload)
            if length < 126:
                frame.append(length | 0x80)
            elif length < 65536:
                frame.append(126 | 0x80)
                frame.extend(length.to_bytes(2, "big"))
            else:
                frame.append(127 | 0x80)
                frame.extend(length.to_bytes(8, "big"))
            mask_key = os.urandom(4)
            frame.extend(mask_key)
            masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            frame.extend(masked)
            writer.write(bytes(frame))
            await writer.drain()

            print(f"  📤 保活状态已上报: {cs['company_name']}")

        # Graceful close
        close_frame = bytearray([0x88, 0x80, 0, 0, 0, 0])
        writer.write(bytes(close_frame))
        await writer.drain()
        writer.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_do_report())
        print("✅ 上报完成")
    except Exception as e:
        print(f"❌ 上报失败: {e}")
        sys.exit(1)
    finally:
        loop.close()


# ── WebSocket Client ────────────────────────────────────────────

class NodeAgent:
    """Main agent that connects to cloud server and manages everything."""

    def __init__(self, config):
        self.node_id = config["node_id"]
        self.cloud_ws_url = config["cloud_ws_url"]
        self.ws = None
        self._running = False
        self._tasks = {}       # task_id → process info
        self._heartbeat_task = None
        self._keepalive_task = None
        self._sync_task = None
        self._sync_interval = SYNC_INTERVAL  # 可由控制台 register_ack 下发覆盖
        self._company_sessions = set()  # company IDs with active sessions
        self.hostname = socket.gethostname()  # 设备主机名，上报时携带
        # 心跳数据采集缓存
        self._device_config = None          # 设备配置（静态，首次采集后缓存）
        self._prev_cpu_stat = None          # 上次 /proc/stat 读数，用于计算 CPU%
        self._prev_proc_cpu = {}            # {pid: (utime+stime, timestamp)} 进程 CPU 基线

    async def start(self):
        """Connect to cloud server and run main loop."""
        self._running = True
        print(f"[node_agent] {self.node_id} 启动, 控制台: {self.cloud_ws_url}")

        # Start periodic tasks
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())
        self._keepalive_task = asyncio.ensure_future(self._keepalive_report_loop())
        self._sync_task = asyncio.ensure_future(self._periodic_sync_loop())

        # Main WebSocket connection loop
        while self._running:
            try:
                await self._connect_ws()
            except Exception as e:
                print(f"[node_agent] WebSocket 错误: {e}, {WEBSOCKET_RECONNECT_DELAY}s 后重连...")
                await asyncio.sleep(WEBSOCKET_RECONNECT_DELAY)

    async def stop(self):
        """Graceful shutdown."""
        self._running = False
        for task in [self._heartbeat_task, self._keepalive_task, self._sync_task]:
            if task:
                task.cancel()
        # Kill all running Claude processes
        for task_id, proc in list(self._tasks.items()):
            try:
                proc["process"].terminate()
            except Exception:
                pass
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        print("[node_agent] 已停止")

    async def _connect_ws(self):
        """Establish WebSocket connection using native TCP sockets (zero deps)."""
        await self._connect_ws_native()

    async def _connect_ws_native(self):
        """Native asyncio WebSocket fallback."""
        # Parse URL
        url = self.cloud_ws_url.replace("ws://", "").replace("wss://", "").replace("http://", "").replace("https://", "")
        if "/" in url:
            host_part, path = url.split("/", 1)
            path = "/" + path
        else:
            host_part = url
            path = "/"

        if ":" in host_part:
            host, port = host_part.rsplit(":", 1)
            port = int(port)
        else:
            host = host_part
            port = 80

        reader, writer = await asyncio.open_connection(host, port)

        # HTTP Upgrade handshake (proper base64 WS key)
        import base64 as b64
        ws_key_bytes = os.urandom(16)
        ws_key = b64.b64encode(ws_key_bytes).decode('ascii')
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        writer.write(request.encode())
        await writer.drain()

        # Read response
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = await reader.read(4096)
            if not chunk:
                break
            response += chunk

        if b"101" not in response:
            print(f"[node_agent] WS 握手失败: {response[:200]}")
            writer.close()
            return

        print(f"[node_agent] WS 已连接 (native)")

        # Send register with device specs
        device = self._gather_device_config()
        await self._ws_send_native(writer, {
            "type": "register",
            "node_id": self.node_id,
            "hostname": self.hostname,
            "max_concurrency": 15,
            "cpu_model": device.get("cpu_model", "unknown"),
            "cpu_cores": device.get("cpu_cores", 1),
            "memory_total_gb": device.get("memory_total_gb", 0),
        })

        # ── 等待 register_ack（必须收到控制台回复才算连接成功）──
        try:
            while True:
                msg = await asyncio.wait_for(
                    self._ws_recv_native(reader), timeout=REGISTER_TIMEOUT)
                if msg is None:
                    print("[node_agent] WS 连接在等待注册确认时关闭")
                    writer.close()
                    return
                if msg.get("type") == "register_ack":
                    # 解析控制台下发的同步频率
                    sync_interval = msg.get("sync_interval")
                    if sync_interval and isinstance(sync_interval, (int, float)) and sync_interval > 0:
                        self._sync_interval = int(sync_interval)
                        print(f"[node_agent] 控制台下发同步频率: {self._sync_interval}s")
                    print(f"[node_agent] 注册确认: {msg.get('message', 'OK')}")
                    break
                else:
                    print(f"[node_agent] 注册期间收到非预期消息: {msg.get('type')}")
        except asyncio.TimeoutError:
            print(f"[node_agent] 注册超时({REGISTER_TIMEOUT}s)，未收到 register_ack")
            writer.close()
            return

        # Read loop
        while self._running:
            try:
                msg = await self._ws_recv_native(reader)
                if msg is None:
                    break
                await self._handle_message(msg)
            except Exception as e:
                print(f"[node_agent] 读取错误: {e}")
                break

        writer.close()

    async def _read_exactly(self, reader, n):
        """Read exactly n bytes from reader (Python 3.6 compat)."""
        data = b""
        while len(data) < n:
            chunk = await reader.read(n - len(data))
            if not chunk:
                raise ConnectionError("Connection closed")
            data += chunk
        return data

    async def _ws_send_native(self, writer, msg):
        """Send a WebSocket text frame (client → server, MUST set MASK)."""
        payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        frame = bytearray()
        frame.append(0x81)  # FIN + text opcode
        length = len(payload)
        # Set MASK bit (bit 7) per spec: client → server frames MUST be masked
        if length < 126:
            frame.append(length | 0x80)
        elif length < 65536:
            frame.append(126 | 0x80)
            frame.extend(length.to_bytes(2, "big"))
        else:
            frame.append(127 | 0x80)
            frame.extend(length.to_bytes(8, "big"))
        # Generate 4-byte mask key
        mask_key = os.urandom(4)
        frame.extend(mask_key)
        # XOR payload with mask key
        masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        frame.extend(masked_payload)
        writer.write(bytes(frame))
        await writer.drain()

    async def _ws_recv_native(self, reader):
        """Receive a WebSocket frame, return parsed JSON or None."""
        header = await self._read_exactly(reader, 2)
        opcode = header[0] & 0x0F
        masked = header[1] & 0x80
        length = header[1] & 0x7F

        if opcode == 0x08:  # Close
            return None
        if opcode == 0x09:  # Ping
            # Send pong (we don't have the writer in this context, just skip)
            return None

        if length == 126:
            length = int.from_bytes(await self._read_exactly(reader, 2), "big")
        elif length == 127:
            length = int.from_bytes(await self._read_exactly(reader, 8), "big")

        if masked:
            mask_key = await self._read_exactly(reader, 4)

        payload = await self._read_exactly(reader, length)
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        try:
            return json.loads(payload.decode("utf-8"))
        except Exception:
            return None

    def _ws_send(self, msg):
        """Send a message via WebSocket (thread-safe)."""
        if self.ws and hasattr(self.ws, 'send'):
            try:
                self.ws.send(json.dumps(msg, ensure_ascii=False))
            except Exception as e:
                print(f"[node_agent] 发送失败: {e}")

    # ── Message Handler ─────────────────────────────────────

    async def _handle_message(self, msg):
        """Handle incoming messages from cloud server."""
        msg_type = msg.get("type", "")
        print(f"[node_agent] 收到: {msg_type}")

        if msg_type == "register_ack":
            sync_interval = msg.get("sync_interval")
            if sync_interval and isinstance(sync_interval, (int, float)) and sync_interval > 0:
                self._sync_interval = int(sync_interval)
                print(f"[node_agent] 控制台下发同步频率: {self._sync_interval}s")
            print(f"[node_agent] 注册确认: {msg.get('message', 'OK')}")

        elif msg_type == "assign_task":
            await self._handle_assign_task(msg)

        elif msg_type == "pause_task":
            self._handle_pause_task(msg)

        elif msg_type == "resume_task":
            self._handle_resume_task(msg)

        elif msg_type == "terminate_task":
            self._handle_terminate_task(msg)

        elif msg_type == "trigger_login":
            await self._handle_trigger_login(msg)

        elif msg_type == "trigger_sync":
            asyncio.ensure_future(self._run_sync())

        elif msg_type == "sync_ack":
            status = msg.get("ok") and "成功" or "失败"
            print(f"[node_agent] 同步{status}: {msg.get('stats', '')}")

    async def _handle_assign_task(self, msg):
        """Spawn a Claude Code subprocess to run the query."""
        task_id = msg.get("task_id")
        company_id = msg.get("company_id")
        company_name = msg.get("company_name", "")
        province = msg.get("province", "")
        session_id = msg.get("session_id", f"sess-{int(time.time())}")

        print(f"[node_agent] 任务 {task_id}: {company_name} ({province})")

        prompt = f"查询{company_name}的车辆违章，省份{province}"
        try:
            proc = subprocess.Popen(
                [CLAUDE_PATH, "--session", session_id, "--prompt", prompt],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            self._ws_send({
                "type": "task_failed", "task_id": task_id,
                "error": f"Claude CLI 未找到, 请确认 {CLAUDE_PATH} 已安装"
            })
            return

        self._tasks[task_id] = {
            "process": proc,
            "task_id": task_id,
            "company_id": company_id,
            "company_name": company_name,
            "session_id": session_id,
            "started_at": datetime.now().isoformat(),
        }
        self._company_sessions.add(company_id)

        self._ws_send({
            "type": "status_ack", "task_id": task_id,
            "status": "进行中",
            "message": f"Claude Code 已启动, pid={proc.pid}, session={session_id}"
        })

        # Read stdout/stderr in background
        asyncio.ensure_future(self._read_process_output(task_id, proc))

    def _handle_pause_task(self, msg):
        task_id = msg.get("task_id")
        task = self._tasks.get(task_id)
        if task:
            try:
                task["process"].send_signal(signal.SIGSTOP)
                self._ws_send({
                    "type": "status_ack", "task_id": task_id, "status": "暂停",
                    "message": f"SIGSTOP sent to pid={task['process'].pid}"
                })
            except Exception as e:
                self._ws_send({
                    "type": "status_ack", "task_id": task_id, "status": "暂停指令下发",
                    "message": f"Pause failed: {e}"
                })

    def _handle_resume_task(self, msg):
        task_id = msg.get("task_id")
        task = self._tasks.get(task_id)
        if task:
            try:
                task["process"].send_signal(signal.SIGCONT)
                self._ws_send({
                    "type": "status_ack", "task_id": task_id, "status": "进行中",
                    "message": f"SIGCONT sent to pid={task['process'].pid}"
                })
            except Exception as e:
                self._ws_send({
                    "type": "status_ack", "task_id": task_id,
                    "message": f"Resume failed: {e}"
                })

    def _handle_terminate_task(self, msg):
        task_id = msg.get("task_id")
        task = self._tasks.get(task_id)
        if task:
            try:
                task["process"].terminate()
                # Schedule kill after 10s
                pid = task["process"].pid
                asyncio.get_event_loop().call_later(10,
                    lambda: task["process"].kill() if task["process"].poll() is None else None
                )
                self._ws_send({
                    "type": "status_ack", "task_id": task_id, "status": "终止指令下发",
                    "message": f"SIGTERM sent to pid={pid}"
                })
            except Exception as e:
                self._ws_send({
                    "type": "status_ack", "task_id": task_id,
                    "message": f"Terminate failed: {e}"
                })

    async def _handle_trigger_login(self, msg):
        """Handle trigger_login command from cloud server.
        Spawn Claude Code to open 12123 login page and capture QR code.
        """
        company_name = msg.get("company_name", "")
        province_url = msg.get("province_url", "")

        print(f"[node_agent] 触发扫码登录: {company_name}")

        # Start a Claude session to get QR code
        session_id = f"login-{company_name}-{int(time.time())}"
        prompt = (
            f"登录{company_name}的12123账号。"
            f"请导航到 provincial 12123 登录页面，截图二维码保存到 "
            f"violation_query/screenshots/qr_{company_name}.png，"
            f"然后输出 __QR_READY__:qr_{company_name}.png"
        )

        try:
            proc = subprocess.Popen(
                [CLAUDE_PATH, "--session", session_id, "--prompt", prompt],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            self._ws_send({
                "type": "login_failed", "company_name": company_name,
                "reason": "Claude CLI 未找到"
            })
            return

        # Monitor output for QR ready marker
        import threading
        def _monitor_qr():
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if "__QR_READY__" in line:
                        # Extract filename
                        qr_file = line.split("__QR_READY__:")[-1].strip()
                        # Read and base64
                        import base64
                        from lib.core import _find_project_root
                        root = _find_project_root()
                        qr_path = os.path.join(root, qr_file) if not os.path.isabs(qr_file) else qr_file
                        if os.path.exists(qr_path):
                            with open(qr_path, "rb") as f:
                                img_b64 = base64.b64encode(f.read()).decode("utf-8")
                            self._ws_send({
                                "type": "qr_code",
                                "company_name": company_name,
                                "image_base64": img_b64,
                                "qr_expires_at": datetime.now().isoformat(),
                            })
                    elif "__LOGIN_OK__" in line:
                        self._ws_send({
                            "type": "login_ok",
                            "company_name": company_name,
                        })
                    elif "__LOGIN_FAILED__" in line:
                        reason = line.split("__LOGIN_FAILED__:")[-1].strip() if ":" in line else "unknown"
                        self._ws_send({
                            "type": "login_failed",
                            "company_name": company_name,
                            "reason": reason,
                        })
            except Exception:
                pass

        t = threading.Thread(target=_monitor_qr, daemon=True)
        t.start()

    # ── Process Output ────────────────────────────────────────

    async def _read_process_output(self, task_id, proc):
        """Read stdout/stderr from a Claude Code process and relay to cloud."""
        seq = 0
        loop = asyncio.get_event_loop()

        def _read_stream(stream_name, pipe):
            nonlocal seq
            try:
                for line in pipe:
                    line = line.strip()
                    if not line:
                        continue
                    seq += 1
                    msg = {
                        "type": "stream_output",
                        "task_id": task_id,
                        "stream": stream_name,
                        "line": line,
                        "seq": seq,
                    }

                    # Parse progress
                    if "入口导航" in line:
                        msg["progress"] = "入口导航"
                    elif "登录中" in line:
                        msg["progress"] = "登录中"
                    elif "查询准备" in line:
                        msg["progress"] = "查询准备"
                    elif "查询中" in line:
                        msg["progress"] = "查询中"
                    elif "已完成" in line:
                        msg["progress"] = "已完成"

                    self._ws_send(msg)
            except Exception:
                pass

        # Read both streams in threads
        import threading
        t1 = threading.Thread(target=_read_stream, args=("stdout", proc.stdout), daemon=True)
        t2 = threading.Thread(target=_read_stream, args=("stderr", proc.stderr), daemon=True)
        t1.start()
        t2.start()

        # Wait for process completion
        def _wait_exit():
            proc.wait()
            asyncio.run_coroutine_threadsafe(self._on_task_exit(task_id, proc.returncode), loop)  # type: ignore

        t3 = threading.Thread(target=_wait_exit, daemon=True)
        t3.start()

    async def _on_task_exit(self, task_id, exit_code):
        """Handle task process exit."""
        task = self._tasks.pop(task_id, None)
        if not task:
            return

        company_name = task["company_name"]
        self._company_sessions.discard(task["company_id"])

        if exit_code == 0:
            self._ws_send({
                "type": "task_completed",
                "task_id": task_id,
            })
            self._ws_send({
                "type": "log", "task_id": task_id,
                "level": "INFO", "category": "task",
                "message": f"任务完成, exit code={exit_code}",
            })
            # Trigger sync after task completion
            await asyncio.sleep(3)  # wait for SQLite writes
            await self._sync_company(company_name, task_id)
        else:
            self._ws_send({
                "type": "task_failed", "task_id": task_id,
                "error": f"进程退出, exit code={exit_code}",
            })

        print(f"[node_agent] 任务 {task_id} 退出, code={exit_code}")

    # ── Sync ──────────────────────────────────────────────────

    async def _sync_company(self, company_name, task_id=None):
        """Export data for a company and send to cloud server."""
        print(f"[node_agent] 同步: {company_name}")
        result = export_for_sync(company_name=company_name, node_id=self.node_id)

        if not result.get("ok"):
            print(f"[node_agent] 同步导出失败: {result.get('error')}")
            return

        if not result.get("vehicles") and not result.get("violations"):
            print(f"[node_agent] 无数据需同步: {company_name}")
            return

        self._ws_send({
            "type": "sync_data",
            "company_name": company_name,
            "node_id": self.node_id,
            "hostname": self.hostname,
            "task_id": task_id,
            "companies": result["companies"],
            "vehicles": result["vehicles"],
            "violations": result["violations"],
        })

    async def _run_sync(self):
        """Periodic full sync — sync all companies in local DB."""
        import sqlite3
        from lib.db import _get_db_path, _init_db
        _init_db()
        db_path = _get_db_path()
        if not os.path.exists(db_path):
            return

        conn = sqlite3.connect(db_path)
        cur = conn.execute("SELECT DISTINCT name FROM companies")
        company_names = [r[0] for r in cur.fetchall()]
        conn.close()

        for name in company_names:
            await self._sync_company(name)
            await asyncio.sleep(5)  # throttle

    # ── Heartbeat Data Gather ─────────────────────────────────

    def _gather_device_config(self):
        """Gather static device configuration (cached after first call)."""
        if self._device_config is not None:
            return self._device_config
        config = {}
        try:
            config["cpu_cores"] = os.cpu_count() or 1
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if line.startswith("model name"):
                        config["cpu_model"] = line.split(":")[1].strip()
                        break
            if "cpu_model" not in config:
                config["cpu_model"] = "unknown"
        except Exception:
            config["cpu_model"] = "unknown"
            config["cpu_cores"] = os.cpu_count() or 1
        try:
            mem_total_kb = 0
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_total_kb = int(line.split()[1])
                        break
            config["memory_total_gb"] = round(mem_total_kb / (1024 * 1024), 2)
        except Exception:
            config["memory_total_gb"] = 0
        self._device_config = config
        return config

    def _read_cpu_stat(self):
        """Read /proc/stat first line, return dict of cpu times."""
        try:
            with open("/proc/stat", "r") as f:
                parts = f.readline().split()
            if parts[0] != "cpu":
                return None
            return {
                "user": int(parts[1]), "nice": int(parts[2]),
                "system": int(parts[3]), "idle": int(parts[4]),
                "iowait": int(parts[5]) if len(parts) > 5 else 0,
                "irq": int(parts[6]) if len(parts) > 6 else 0,
                "softirq": int(parts[7]) if len(parts) > 7 else 0,
                "steal": int(parts[8]) if len(parts) > 8 else 0,
            }
        except Exception:
            return None

    def _gather_system_usage(self):
        """Gather total system CPU% and memory usage."""
        result = {"cpu_percent": 0.0, "memory_percent": 0.0,
                   "memory_used_gb": 0.0, "memory_total_gb": 0.0}
        # CPU
        curr = self._read_cpu_stat()
        if curr and self._prev_cpu_stat:
            prev_total = sum(self._prev_cpu_stat.values())
            curr_total = sum(curr.values())
            total_delta = curr_total - prev_total
            if total_delta > 0:
                idle_delta = curr["idle"] - self._prev_cpu_stat["idle"]
                result["cpu_percent"] = round(
                    100.0 * (total_delta - idle_delta) / total_delta, 1)
        if curr:
            self._prev_cpu_stat = curr
        # Memory
        try:
            mem_total = 0; mem_avail = 0
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_total = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        mem_avail = int(line.split()[1])
                    if mem_total and mem_avail:
                        break
            if mem_total > 0:
                result["memory_total_gb"] = round(mem_total / (1024 * 1024), 2)
                result["memory_used_gb"] = round((mem_total - mem_avail) / (1024 * 1024), 2)
                result["memory_percent"] = round(
                    100.0 * (mem_total - mem_avail) / mem_total, 1)
        except Exception:
            pass
        return result

    def _get_proc_cpu_mem(self, pid):
        """Get CPU% (since last call) and RSS (MB) for a process. Returns (cpu_pct, mem_mb)."""
        cpu_pct = 0.0
        mem_mb = 0.0
        try:
            with open(f"/proc/{pid}/stat", "r") as f:
                stat_parts = f.read().split()
            # field 14=utime, 15=stime (1-indexed)
            utime = int(stat_parts[13])
            stime = int(stat_parts[14])
            now = time.time()
            key = str(pid)
            if key in self._prev_proc_cpu:
                prev_total, prev_ts = self._prev_proc_cpu[key]
                delta_t = now - prev_ts
                if delta_t > 0:
                    cpu_pct = round(100.0 * (utime + stime - prev_total) /
                                    (delta_t * os.sysconf('SC_CLK_TCK')), 1)
            self._prev_proc_cpu[key] = (utime + stime, now)
        except Exception:
            pass
        try:
            with open(f"/proc/{pid}/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        mem_mb = round(int(line.split()[1]) / 1024, 1)
                        break
        except Exception:
            pass
        return (cpu_pct, mem_mb)

    def _gather_browser_usage(self):
        """Gather Chrome browser instances, tabs, CPU, and memory usage."""
        instances = []
        seen_ports = set()
        try:
            # Find chromium processes and extract debug ports
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/cmdline", "rb") as f:
                        cmdline = f.read().replace(b'\0', b' ').decode('utf-8', errors='replace')
                except Exception:
                    continue
                if "chromium-browser" not in cmdline and "chrome" not in cmdline.lower():
                    continue
                # Extract --remote-debugging-port
                port = None
                profile = "unknown"
                parts = cmdline.split()
                for i, p in enumerate(parts):
                    if p.startswith("--remote-debugging-port="):
                        port = p.split("=")[1]
                    if p.startswith("--user-data-dir="):
                        profile = p.split("=")[1].split("/")[-1]
                if not port or port in seen_ports:
                    continue
                seen_ports.add(port)
                pid = int(entry)
                cpu_pct, mem_mb = self._get_proc_cpu_mem(pid)
                # Get tabs via Chrome DevTools Protocol /json endpoint
                tabs = []
                tab_count = 0
                try:
                    import urllib.request
                    req = urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json", timeout=5)
                    tab_list = json.loads(req.read().decode("utf-8"))
                    tab_count = len(tab_list)
                    for t in tab_list:
                        tabs.append({
                            "id": t.get("id", "")[:16],
                            "title": (t.get("title", "") or "")[:60],
                            "url": (t.get("url", "") or "")[:120],
                        })
                except Exception:
                    pass
                instances.append({
                    "profile": profile,
                    "debug_port": int(port),
                    "cpu_percent": cpu_pct,
                    "memory_mb": mem_mb,
                    "tab_count": tab_count,
                    "tabs": tabs,
                })
        except Exception:
            pass
        total_cpu = round(sum(i["cpu_percent"] for i in instances), 1)
        total_mem = round(sum(i["memory_mb"] for i in instances), 1)
        return {"instances": instances, "total_cpu_percent": total_cpu,
                "total_memory_mb": total_mem}

    def _gather_service_status(self):
        """Gather all skill services with status and resource usage.

        设计原则：
        - 固定清单（FIXED_SERVICES）：已知的 systemd 服务，无论是否运行都必须上报
        - 动态发现（DYNAMIC）：运行时扫描 /proc 发现的临时任务进程

        当前技能全部进程/服务清单：
        ┌─────────────────────────────────────────────────────┐
        │ 类型        │ 服务/脚本                  │ 上报方式 │
        ├─────────────────────────────────────────────────────┤
        │ 通信服务    │ dst-node-agent.service     │ 固定     │
        │ 浏览器服务  │ pinchtab.service           │ 固定     │
        │ 保活看门狗  │ keepalive-watchdog-*.timer │ 固定     │
        │ 保活服务    │ keepalive-*.service        │ 固定     │
        │ 清理服务    │ cleanup-dst.timer          │ 固定     │
        │ 查询服务    │ scan_vehicles.py           │ 动态     │
        │ 查询服务    │ collect_violations.py      │ 动态     │
        └─────────────────────────────────────────────────────┘

        辅助脚本（不独立运行，无需上报）：
        - cookie_persist.py → pinchtab drop-in 触发
        - session_manager.py / tab_session.py → 库模块
        - pinchtab_client.py → 库模块
        - violation_helper.py → CLI 调度入口
        - _split_helper.py → 一次性工具
        """
        services = []

        # ── 辅助函数 ──

        def _read_proc_cmdline(pid):
            """Read /proc/<pid>/cmdline, return clean arg list (no truncation)."""
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    raw = f.read()
                return [a.decode("utf-8", errors="replace") for a in raw.split(b'\0') if a]
            except Exception:
                return []

        def _extract_company(args):
            """Extract --company value from cmdline args, clean quotes/escapes."""
            for i, a in enumerate(args):
                if a == "--company" and i + 1 < len(args):
                    return args[i + 1].strip().strip('"').strip("'").strip()[:60]
            return ""

        def _systemctl(unit, user=True):
            """Run systemctl is-active, return status string."""
            try:
                cmd = ["systemctl", "--user", "is-active", unit, "--no-pager"] if user else \
                      ["systemctl", "is-active", unit, "--no-pager"]
                r = subprocess.run(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, encoding='utf-8', timeout=5)
                return r.stdout.strip()
            except Exception:
                return "unknown"

        def _make_svc(name, unit, pid=None, company=None):
            """Build a service entry dict."""
            cpu_pct, mem_mb = 0.0, 0.0
            if pid:
                cpu_pct, mem_mb = self._get_proc_cpu_mem(pid)
            svc = {"name": name, "unit": unit,
                   "status": _systemctl(unit, user=(unit != "dst-node-agent.service")),
                   "pid": pid, "cpu_percent": cpu_pct, "memory_mb": mem_mb}
            if company:
                svc["company"] = company
            return svc

        def _find_pid_by_cmdline(match_fn):
            """Find PID by scanning /proc/*/cmdline with a match function."""
            try:
                for entry in os.listdir("/proc"):
                    if not entry.isdigit():
                        continue
                    args = _read_proc_cmdline(entry)
                    if args and match_fn(args):
                        return int(entry), args
            except Exception:
                pass
            return None, []

        # ── 构建全量 unit 列表（一次 systemctl 查询 service + timer）──
        all_units = {}  # unit_name → line
        for args in [
            ["systemctl", "--user", "list-units", "--type=service",
             "--no-pager", "--no-legend"],
            ["systemctl", "--user", "list-units", "--type=timer",
             "--no-pager", "--no-legend"],
            ["systemctl", "list-units", "--type=service",
             "--no-pager", "--no-legend"],
        ]:
            try:
                r = subprocess.run(args, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, encoding='utf-8', timeout=5)
                for line in r.stdout.split('\n'):
                    if not line.strip():
                        continue
                    unit = line.split()[0]
                    if unit not in all_units:
                        all_units[unit] = line
            except Exception:
                pass

        # ── 固定清单 ──

        # 1. 通信服务 — dst-node-agent (system service)
        pid, _ = _find_pid_by_cmdline(
            lambda args: any("node_agent.py" in a for a in args))
        services.append(_make_svc("通信服务", "dst-node-agent.service", pid))

        # 2. 浏览器服务 — pinchtab
        chrome_pid = None
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            args = _read_proc_cmdline(entry)
            cmd0 = args[0] if args else ""
            if ("chrome" in cmd0.lower() or "chromium" in cmd0.lower()) and not any(
                    a.startswith("--type=") for a in args):
                chrome_pid = int(entry)
                break
        services.append(_make_svc("浏览器服务", "pinchtab.service", chrome_pid))

        # 3. 保活看门狗 — keepalive-watchdog-* (oneshot timer, per company)
        #    看门狗是 oneshot service + timer，状态以 timer 为准
        #    同时查 service 和 timer（可能只安装了其中一种）
        wd_timers = sorted(
            [u for u in all_units if u.startswith("keepalive-watchdog-") and u.endswith(".timer")])
        wd_services = sorted(
            [u for u in all_units if u.startswith("keepalive-watchdog-") and u.endswith(".service")])
        wd_units = wd_timers if wd_timers else wd_services

        if not wd_units:
            services.append({
                "name": "保活看门狗", "unit": "keepalive-watchdog-*.timer",
                "status": "not-installed",
                "pid": None, "cpu_percent": 0.0, "memory_mb": 0.0,
            })
        else:
            for unit in wd_units:
                # 从 unit description 或看门狗脚本参数中提取公司名
                company = ""
                unit_prefix = unit.replace(".timer", "").replace(".service", "")
                for entry in os.listdir("/proc"):
                    if not entry.isdigit():
                        continue
                    args = _read_proc_cmdline(entry)
                    if args and any("keepalive_watchdog.sh" in a for a in args) \
                       and any(unit_prefix in a for a in args):
                        company = _extract_company(args) or ""
                        break
                svc = _make_svc("保活看门狗", unit, company=company)
                services.append(svc)

        # 4. 保活服务 — keepalive-* (daemon, per company, exclude watchdog)
        kp_units = sorted(
            [u for u in all_units
             if u.startswith("keepalive-") and not u.startswith("keepalive-watchdog-")])
        kp_pids = sorted(
            [int(e) for e in os.listdir("/proc") if e.isdigit()
             and any("keepalive_daemon.py" in a
                     for a in _read_proc_cmdline(e))])
        if not kp_units:
            services.append({
                "name": "保活服务", "unit": "keepalive-*.service",
                "status": "not-installed",
                "pid": None, "cpu_percent": 0.0, "memory_mb": 0.0,
            })
        else:
            for i, unit in enumerate(kp_units):
                pid = kp_pids[i] if i < len(kp_pids) else None
                company = _extract_company(_read_proc_cmdline(pid)) if pid else ""
                services.append(_make_svc("保活服务", unit, pid, company))

        # 5. 清理服务 — cleanup-dst (oneshot timer)
        pid, _ = _find_pid_by_cmdline(
            lambda args: any("cleanup_daemon.py" in a for a in args))
        svc = _make_svc("清理服务", "cleanup-dst.timer", pid)
        if "cleanup-dst.timer" in all_units or "cleanup-dst.service" in all_units:
            svc["status"] = _systemctl("cleanup-dst.timer", user=True)
        services.append(svc)

        # ── 动态发现：扫描所有 /proc 进程，识别 skill 相关但不在固定清单中的进程 ──

        seen_pids = {s["pid"] for s in services if s.get("pid")}

        # 6. 查询服务 — scan_vehicles / collect_violations
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid in seen_pids:
                continue
            args = _read_proc_cmdline(entry)
            if not args:
                continue
            is_scan = any("scan_vehicles.py" in a for a in args)
            is_collect = any("collect_violations.py" in a for a in args)
            if not is_scan and not is_collect:
                continue
            qtype = "scan" if is_scan else "collect"
            company = _extract_company(args)
            cpu_pct, mem_mb = self._get_proc_cpu_mem(pid)
            services.append({
                "name": f"查询服务({qtype})", "unit": None,
                "status": "running", "pid": pid,
                "cpu_percent": cpu_pct, "memory_mb": mem_mb,
                "company": company,
            })
            seen_pids.add(pid)

        return services

    def _gather_agent_usage(self):
        """Gather Claude AI agent processes grouped by session (TTY).
        Only reports active (running) sessions.
        """
        sessions = []
        try:
            r = subprocess.run(["ps", "aux"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, encoding='utf-8', timeout=5)
            for line in r.stdout.split('\n'):
                if "grep" in line:
                    continue
                # Match claude main process (not subprocesses like bash/shell)
                is_claude = False
                parts = line.split(None, 10)
                if len(parts) < 11:
                    continue
                cmd = parts[10]
                # Match 'claude' as the binary name (not path arguments)
                cmd_base = parts[10].split()[0] if parts[10].strip() else ""
                if cmd_base.endswith("/claude") or cmd_base == "claude":
                    is_claude = True
                if not is_claude:
                    # Also match claude in arguments like 'node ... claude ...'
                    if " claude " not in f" {cmd} " and not cmd.startswith("claude "):
                        continue
                    # But exclude: node_agent, keepalive, pinchtab, scan, collect
                    if any(x in cmd for x in ["node_agent", "keepalive_daemon",
                                               "scan_vehicles", "collect_violations",
                                               "cleanup_daemon", "pinchtab"]):
                        continue
                try:
                    pid = int(parts[1])
                except ValueError:
                    continue
                tty = parts[6] if parts[6] != "?" else "bg"
                cpu_pct_str = parts[2]
                mem_pct_str = parts[3]
                try:
                    cpu_pct_val = float(cpu_pct_str)
                except ValueError:
                    cpu_pct_val = 0.0
                cpu_pct, mem_mb = self._get_proc_cpu_mem(pid)
                sessions.append({
                    "session_id": tty,
                    "pid": pid,
                    "cpu_percent": cpu_pct,
                    "memory_mb": mem_mb,
                })
        except Exception:
            pass
        total_cpu = round(sum(s["cpu_percent"] for s in sessions), 1)
        total_mem = round(sum(s["memory_mb"] for s in sessions), 1)
        return {"sessions": sessions, "total_cpu_percent": total_cpu,
                "total_memory_mb": total_mem}

    def _gather_heartbeat_data(self):
        """Orchestrate all heartbeat data gathering and compute 'other' usage."""
        device = self._gather_device_config()
        system = self._gather_system_usage()
        browser = self._gather_browser_usage()
        services = self._gather_service_status()
        agent = self._gather_agent_usage()

        # Calculate "other" = system total - sum of tracked categories
        svc_total_cpu = sum(s.get("cpu_percent", 0) for s in services)
        svc_total_mem = sum(s.get("memory_mb", 0) for s in services)
        tracked_cpu = browser.get("total_cpu_percent", 0) + svc_total_cpu + agent.get("total_cpu_percent", 0)
        tracked_mem = browser.get("total_memory_mb", 0) + svc_total_mem + agent.get("total_memory_mb", 0)
        sys_mem_mb = system.get("memory_used_gb", 0) * 1024
        other_cpu = round(max(0, system.get("cpu_percent", 0) - tracked_cpu), 1)
        other_mem = round(max(0, sys_mem_mb - tracked_mem), 1)

        return {
            "device_config": device,
            "system_usage": system,
            "browser_usage": browser,
            "services": services,
            "agent_usage": agent,
            "other_usage": {"cpu_percent": other_cpu, "memory_mb": other_mem},
        }

    # ── Periodic Loops ────────────────────────────────────────

    async def _heartbeat_loop(self):
        """Send detailed heartbeat every HEARTBEAT_INTERVAL seconds (30s)."""
        while self._running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            data = self._gather_heartbeat_data()
            self._ws_send({
                "type": "heartbeat",
                "node_id": self.node_id,
                "hostname": self.hostname,
                "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "active_sessions": len(self._tasks),
                **data,
            })

    async def _keepalive_report_loop(self):
        """Report keepalive status per company every KEEPALIVE_REPORT_INTERVAL (60s).
        Each message carries company_name as primary field + device info for verification.
        """
        while self._running:
            await asyncio.sleep(KEEPALIVE_REPORT_INTERVAL)
            companies_status = self._gather_keepalive_status()
            for company in companies_status:
                self._ws_send({
                    "type": "keepalive_status",
                    "company_name": company["company_name"],
                    "node_id": self.node_id,
                    "hostname": self.hostname,
                    "is_logged_in": company["is_logged_in"],
                    "keepalive_alive": company["keepalive_alive"],
                    "last_cycle": company.get("last_cycle"),
                    "health_state": company.get("health_state"),
                })

    def _gather_keepalive_status(self):
        """Read local keepalive state for all profiles."""
        import sqlite3
        from lib.db import _get_db_path, _init_db
        _init_db()
        db_path = _get_db_path()
        if not os.path.exists(db_path):
            return []

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM profiles")
        profiles = [dict(r) for r in cur.fetchall()]
        conn.close()

        result = []
        for p in profiles:
            status = {
                "company_name": p["company_name"],
                "profile_name": p.get("profile_name", ""),
                "is_logged_in": bool(p.get("is_logged_in", 0)),
            }

            # Check keepalive systemd service
            profile_name = p.get("profile_name", "")
            service_name = f"keepalive-{profile_name}"
            try:
                r = subprocess.run(
                    ["systemctl", "--user", "is-active", service_name],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', timeout=5
                )
                status["keepalive_alive"] = r.stdout.strip() == "active"
            except Exception:
                status["keepalive_alive"] = False

            # Read health file
            from lib.core import _get_data_dir
            health_file = os.path.join(_get_data_dir(), f"keepalive_health_{p['company_name']}.json")
            if os.path.exists(health_file):
                try:
                    with open(health_file, "r") as f:
                        health = json.load(f)
                    status["last_cycle"] = health.get("last_check", "")
                except Exception:
                    status["last_cycle"] = None
            else:
                status["last_cycle"] = None

            result.append(status)

        return result

    async def _periodic_sync_loop(self):
        """Periodic full sync fallback. Interval defaults to SYNC_INTERVAL (1800s),
        but can be overridden by register_ack.sync_interval from the control console."""
        while self._running:
            await asyncio.sleep(self._sync_interval)
            print(f"[node_agent] 定时全量同步 (间隔={self._sync_interval}s)...")
            await self._run_sync()


# ── Main ────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "--setup":
            run_setup()
            return
        elif cmd == "--health":
            check_health()
            return
        elif cmd == "--help" or cmd == "-h":
            print(__doc__)
            return
        elif cmd == "--sync-now":
            run_sync_now()
            return
        elif cmd == "--report-keepalive":
            run_report_keepalive()
            return

    # Start daemon
    config = read_node_config()
    if not config:
        print("=" * 60)
        print("❌ 未检测到设备配置")
        print("   请先执行交互式配置: python3 node_agent.py --setup")
        print("   或在对话中说: '配置设备连接'")
        print("=" * 60)
        sys.exit(1)

    if not config.get("node_id") or not config.get("cloud_ws_url"):
        print("=" * 60)
        print("❌ 设备配置不完整")
        print(f"   缺失字段: node_id={config.get('node_id')!r}, cloud_ws_url={config.get('cloud_ws_url')!r}")
        print("   请重新配置: python3 node_agent.py --setup")
        print("=" * 60)
        sys.exit(1)

    # Connectivity check before starting
    ok, err = check_cloud_connectivity(config["cloud_ws_url"])
    if not ok:
        print("=" * 60)
        print(f"⚠️  警告: 无法连接中央控制台 ({config['cloud_ws_url']})")
        print(f"   原因: {err}")
        print("   将尝试启动并持续重连...")
        print("=" * 60)

    agent = NodeAgent(config)

    # Handle signals
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(agent.stop()))
        except NotImplementedError:
            pass  # Windows

    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(agent.start())
    except KeyboardInterrupt:
        print("[node_agent] 收到中断信号")


if __name__ == "__main__":
    main()

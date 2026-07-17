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
import base64
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Ensure lib/ is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.sync import read_node_config, write_node_config, check_cloud_connectivity, export_for_sync
from session_bridge import SessionBridge


# ── Configuration ──────────────────────────────────────────────

WEBSOCKET_RECONNECT_DELAY = 5   # seconds
HEARTBEAT_INTERVAL = 10          # seconds
KEEPALIVE_REPORT_INTERVAL = 300  # 5 minutes
SYNC_INTERVAL = 1800             # 30 minutes periodic sync fallback
CLAUDE_PATH = "/home/openclaw/.npm-global/bin/claude"


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
        self._company_sessions = set()  # company IDs with active sessions
        self._register_event = asyncio.Event()  # 建联确认
        self._writer = None  # WS writer for sending
        self.bridge = None   # SessionBridge — initialized after register_ack

    async def start(self):
        """Connect to cloud server and run main loop."""
        self._running = True
        print(f"[node_agent] {self.node_id} 启动, 控制台: {self.cloud_ws_url}")

        # Periodic tasks will be started AFTER register_ack is received

        # Main WebSocket connection loop
        while self._running:
            try:
                await self._connect_ws()
            except Exception as e:
                print(f"[node_agent] WebSocket 错误: {e}, {WEBSOCKET_RECONNECT_DELAY}s 后重连...")
                self._register_event.clear()  # reset for reconnection
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
        url = self.cloud_ws_url.replace("ws://", "").replace("wss://", "")
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

        # Save writer for sending
        self._writer = writer

        # ── Gather keepalive services ──
        keepalive_services = self._detect_keepalive_services()

        # Send register
        hostname = socket.gethostname()
        await self._ws_send_native(writer, {
            "type": "register",
            "node_id": self.node_id,
            "node_name": self.node_id,
            "hostname": hostname,
            "max_concurrency": 15,
            "memory_total_gb": 4,
            "cpu_cores": os.cpu_count() or 4,
            "keepalive_services": keepalive_services,
        })

        # Start read loop first (so we can receive register_ack)
        print(f"[node_agent] 等待建联确认 (register_ack)...")

        async def _read_and_handle():
            """Read loop: receive messages and dispatch."""
            while self._running:
                try:
                    msg = await self._ws_recv_native(reader)
                    if msg is None:
                        break
                    # register_ack is handled inline to set the event
                    if msg.get("type") == "register_ack":
                        print(f"[node_agent] 注册确认: {msg.get('message', 'OK')}")
                        if not self._register_event.is_set():
                            self._register_event.set()
                            self._start_periodic_loops()
                    else:
                        try:
                            await self._handle_message(msg)
                        except Exception as e:
                            print(f"[node_agent] 消息处理异常 ({msg.get('type')}): {e}")
                except Exception as e:
                    print(f"[node_agent] 读取错误: {e}")
                    break

        # Wait for register_ack with timeout
        read_task = asyncio.ensure_future(_read_and_handle())
        try:
            await asyncio.wait_for(self._register_event.wait(), timeout=30)
            print(f"[node_agent] 建联确认成功")
        except asyncio.TimeoutError:
            print(f"[node_agent] 建联确认超时 (30s), 将重连...")
            read_task.cancel()
            writer.close()
            return

        # Main loop: just wait for read task
        try:
            await read_task
        except asyncio.CancelledError:
            pass

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
        try:
            if self._writer is not None:
                loop = asyncio.get_event_loop()
                # call_soon_threadsafe works from any thread
                loop.call_soon_threadsafe(
                    lambda m=msg: asyncio.ensure_future(
                        self._ws_send_native(self._writer, m)
                    )
                )
        except RuntimeError:
            # No event loop in this thread — fall back to direct send
            try:
                if self._running and self._writer is not None:
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
                    frame.extend(bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload)))
                    self._writer.write(bytes(frame))
            except Exception as e:
                print(f"[node_agent] 发送失败: {e}")
        except Exception as e:
            print(f"[node_agent] 发送失败: {e}")

    def _detect_keepalive_services(self):
        """Detect which keepalive services are installed on this device."""
        import glob
        services = []
        # Check systemd user services
        service_dir = os.path.expanduser("~/.config/systemd/user")
        if os.path.isdir(service_dir):
            for path in glob.glob(os.path.join(service_dir, "keepalive-watchdog-profile_*.service")):
                svc = os.path.basename(path).replace(".service", "")
                services.append(svc)
        return services

    def _start_periodic_loops(self):
        """Start heartbeat, keepalive, and sync loops after register_ack."""
        if not self._heartbeat_task or self._heartbeat_task.cancelled():
            self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())
        if not self._keepalive_task or self._keepalive_task.cancelled():
            self._keepalive_task = asyncio.ensure_future(self._keepalive_report_loop())
        if not self._sync_task or self._sync_task.cancelled():
            self._sync_task = asyncio.ensure_future(self._periodic_sync_loop())

        # Initialize SessionBridge
        self.bridge = SessionBridge(ws_send=self._ws_send, claude_path=CLAUDE_PATH)

        # Start reporting schedule checker
        import threading
        t = threading.Thread(target=self._reporting_schedule_check_loop, daemon=True)
        t.start()

    # ── Message Handler ─────────────────────────────────────

    async def _handle_message(self, msg):
        """Handle incoming messages from cloud server."""
        msg_type = msg.get("type", "")
        print(f"[node_agent] 收到: {msg_type}")

        if msg_type == "register_ack":
            print(f"[node_agent] 注册确认: {msg.get('message', 'OK')}")
            self._register_event.set()

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

        elif msg_type == "session_message":
            self._handle_session_message(msg)

        elif msg_type == "start_keepalive_login":
            await self._handle_start_keepalive_login(msg)

        elif msg_type == "trigger_sync":
            asyncio.ensure_future(self._run_sync())

        elif msg_type == "reporting_schedule_config":
            self._handle_reporting_schedule_config(msg)

        elif msg_type == "sync_ack":
            status = msg.get("ok") and "成功" or "失败"
            print(f"[node_agent] 同步{status}: {msg.get('stats', '')}")

        # ── Session bridge messages ──
        elif msg_type == "session_create":
            if self.bridge:
                await self.bridge.create(
                    session_id=msg.get("session_id", ""),
                    prompt=msg.get("prompt", ""),
                    filter_mode=msg.get("filter_mode", "text_only"),
                    markers=msg.get("markers"),
                )

        elif msg_type == "session_message":
            if self.bridge:
                await self.bridge.send(
                    session_id=msg.get("session_id", ""),
                    text=msg.get("text", ""),
                )

        elif msg_type == "session_cancel":
            if self.bridge:
                await self.bridge.cancel(msg.get("session_id", ""))

        elif msg_type == "session_list":
            if self.bridge:
                self.bridge.list_sessions()

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
                [CLAUDE_PATH, "-p", prompt],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
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
        Routes to keepalive (fast, PinchTab) or session (interactive, Claude) path.
        """
        # 按 mode 判定路由：keepalive(保活) 或 session(Claude交互)
        _USE_KEEPALIVE = False

        if _USE_KEEPALIVE:
            await self._trigger_keepalive_login(msg)
            return

        # ── session / Claude 路径（保留，暂不使用）──
        company_name = msg.get("company_name", "")
        company_id = str(msg.get("company_id", ""))
        prompt = msg.get("prompt", "")
        mode = msg.get("mode", "keepalive")

        if not prompt:
            self._ws_send({
                "type": "login_failed", "company_name": company_name,
                "reason": "未收到登录提示词"
            })
            return

        print(f"[node_agent] 触发扫码登录: {company_name} (mode={mode})")

        # Build Claude command based on mode
        if mode == "session":
            # Interactive session: no -p, stream-json I/O
            cmd = [CLAUDE_PATH,
                   "--input-format", "stream-json",
                   "--output-format", "stream-json",
                   "--verbose",
                   "--include-partial-messages",
                   "--permission-mode", "auto"]
            stdin_pipe = subprocess.PIPE
        else:
            # keepalive: one-shot print mode
            cmd = [CLAUDE_PATH, "-p", prompt,
                   "--output-format", "stream-json",
                   "--verbose",
                   "--include-partial-messages",
                   "--permission-mode", "auto"]
            stdin_pipe = None

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=stdin_pipe,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1,
            )
            print(f"[node_agent] Claude 子进程已启动: PID={proc.pid} mode={mode}")
        except FileNotFoundError:
            self._ws_send({"type": "login_failed", "company_name": company_name, "reason": "Claude CLI 未找到"})
            return
        except Exception as e:
            print(f"[node_agent] Popen 异常: {e}")
            self._ws_send({"type": "login_failed", "company_name": company_name, "reason": f"启动 Claude 失败: {e}"})
            return

        # For session mode, send the prompt as first user message
        if mode == "session" and proc.stdin:
            import json as _json
            first_msg = _json.dumps({
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}
            }) + "\n"
            proc.stdin.write(first_msg)
            proc.stdin.flush()
            # Track session for message forwarding
            self._claude_sessions = getattr(self, '_claude_sessions', {})
            self._claude_sessions[company_id] = proc
            print(f"[node_agent] Session started for {company_name} ({company_id})")

        # Monitor stream-json output
        import threading, json as _json
        def _monitor_qr():
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = _json.loads(line)
                    except Exception:
                        continue
                    etype = event.get("type", "")

                    # For session mode: forward all events as session_chunk
                    if mode == "session":
                        self._ws_send({
                            "type": "session_chunk",
                            "company_name": company_name,
                            "event": event,
                        })

                    # Track assistant text for markers
                    if etype == "assistant":
                        content = event.get("message", {}).get("content", [])
                        for block in content:
                            text = block.get("text", "") if isinstance(block, dict) else ""
                            if "__QR_READY__" in text:
                                qr_file = text.split("__QR_READY__:")[-1].strip().split("\n")[0].strip()
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
                                    print(f"[node_agent] QR sent for {company_name}")
                            if "__LOGIN_OK__" in text:
                                self._ws_send({"type": "login_ok", "company_name": company_name})
                            if "__LOGIN_FAILED__" in text:
                                reason = text.split("__LOGIN_FAILED__:")[-1].strip().split("\n")[0].strip() if ":" in text else "unknown"
                                self._ws_send({"type": "login_failed", "company_name": company_name, "reason": reason})
                    # Progress from tool results
                    elif etype == "user" and event.get("message", {}).get("content", []):
                        for block in event["message"]["content"]:
                            if block.get("type") == "tool_result":
                                tool_id = block.get("tool_use_id", "")
                                tool_content = str(block.get("content", ""))[:200]
                                print(f"[node_agent] claude tool_result {tool_id[:20]}: {tool_content[:100]}")
                                progress_text = tool_content[:120]
                                self._ws_send({
                                    "type": "keepalive_login_progress",
                                    "company_name": company_name,
                                    "progress": progress_text,
                                })
                # stdout exhausted
                exit_code = proc.wait()
                stderr_out = proc.stderr.read()
                print(f"[node_agent] Claude 子进程退出: exit={exit_code} stderr={stderr_out[:500] if stderr_out else '(empty)'}")
                # Clean up session tracking
                if mode == "session":
                    self._ws_send({"type": "session_done", "company_name": company_name, "reason": f"exit={exit_code}"})
                    self._claude_sessions = getattr(self, '_claude_sessions', {})
                    self._claude_sessions.pop(company_id, None)
            except Exception as e:
                print(f"[node_agent] _monitor_qr 异常: {e}")

        t = threading.Thread(target=_monitor_qr, daemon=True)
        t.start()

    async def _trigger_keepalive_login(self, msg):
        """保活登录路径：启动 keepalive daemon（auto-recover），监控 QR 截图和登录进度。

        流程：
        1. 确保 profile 存在且 is_logged_in=0
        2. 启动 keepalive_daemon --auto-recover
        3. 监控 health file → keepalive_login_progress
        4. 监控 QR 文件 → qr_code (base64)
        5. 登录成功 → login_ok
        """
        company_name = msg.get("company_name", "")
        company_id = str(msg.get("company_id", ""))
        province = msg.get("province", "")
        contact_name = msg.get("contact_name", "")
        contact_phone = msg.get("contact_phone", "")
        notify_chat = msg.get("notify_chat_name", "")

        print(f"[node_agent] 保活登录触发: {company_name}")

        # ── 检查是否已有保活程序在运行 ──
        from lib.core import _get_data_dir
        data_dir = _get_data_dir()
        safe_name = company_name  # keepalive uses raw company name
        pid_file = os.path.join(data_dir, f"keepalive_{safe_name}.pid")
        health_file = os.path.join(data_dir, f"keepalive_health_{safe_name}.json")

        keepalive_alive = False
        if os.path.exists(pid_file):
            try:
                with open(pid_file, "r") as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, 0)  # 检查进程是否存在
                keepalive_alive = True
                print(f"[node_agent] 保活程序已在运行 PID={old_pid}，复用")
            except (OSError, ValueError):
                pass

        if keepalive_alive:
            # 发送已有 QR（如有）
            import glob as _glob
            qr_pattern = os.path.join(data_dir, f"recovery_qr_{safe_name}_*.png")
            qr_files = sorted(_glob.glob(qr_pattern))
            if qr_files:
                try:
                    with open(qr_files[-1], "rb") as f:
                        img_b64 = base64.b64encode(f.read()).decode("utf-8")
                    self._ws_send({
                        "type": "qr_code", "company_name": company_name,
                        "image_base64": img_b64,
                        "qr_expires_at": (datetime.now() + timedelta(minutes=5)).isoformat(),
                    })
                    print(f"[node_agent] 复用已有 QR for {company_name}")
                except Exception as e:
                    print(f"[node_agent] 复用 QR 失败: {e}")

            # 发送当前进度
            if os.path.exists(health_file):
                try:
                    with open(health_file, "r") as f:
                        h = json.load(f)
                    progress = h.get("progress", h.get("state", "保活程序运行中"))
                    self._ws_send({
                        "type": "keepalive_login_progress",
                        "company_name": company_name,
                        "progress": progress,
                    })
                except Exception:
                    pass

            # 启动监控（如果还没在监控）
            asyncio.ensure_future(self._monitor_existing_keepalive(
                company_name, health_file, data_dir, safe_name))
            return

        skill_dir = os.path.dirname(os.path.abspath(__file__))
        daemon_path = os.path.join(skill_dir, "keepalive_daemon.py")
        if not os.path.exists(daemon_path):
            self._ws_send({"type": "login_failed", "company_name": company_name,
                           "reason": "keepalive_daemon.py 未找到"})
            return

        # Step 1: 确保 profile 存在并已登出
        try:
            # profile-lookup
            lookup = subprocess.run(
                [sys.executable, os.path.join(skill_dir, "violation_helper.py"),
                 "profile-lookup", "--company", company_name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=15
            )
            profile_data = json.loads(lookup.stdout) if lookup.stdout.strip() else {}
        except Exception as e:
            print(f"[node_agent] profile-lookup 失败: {e}")
            profile_data = {}

        if not profile_data.get("found"):
            # 创建 profile
            print(f"[node_agent] 为 {company_name} 创建新 profile...")
            try:
                create = subprocess.run(
                    [sys.executable, os.path.join(skill_dir, "session_manager.py"),
                     "profile-create"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=15
                )
                create_data = json.loads(create.stdout) if create.stdout.strip() else {}
                profile_name = create_data.get("profile_name", "")
                instance_port = create_data.get("instance_port")
            except Exception as e:
                self._ws_send({"type": "login_failed", "company_name": company_name,
                               "reason": f"profile-create 失败: {e}"})
                return

            # 注册 profile
            platform_url = _province_to_url(province)
            try:
                subprocess.run(
                    [sys.executable, os.path.join(skill_dir, "violation_helper.py"),
                     "profile-register", "--company", company_name,
                     "--profile-name", profile_name,
                     "--platform-url", platform_url],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=15
                )
            except Exception as e:
                print(f"[node_agent] profile-register 失败: {e}")
        else:
            profile_name = profile_data.get("profile_name", "")
            instance_port = profile_data.get("instance_port")

        # 确保 is_logged_in=0
        try:
            import sqlite3
            db_path = os.path.join(
                subprocess.run(
                    [sys.executable, os.path.join(skill_dir, "violation_helper.py"),
                     "get-data-dir"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10
                ).stdout.strip(),
                "violations.db"
            )
            conn = sqlite3.connect(db_path)
            conn.execute("UPDATE profiles SET is_logged_in=0 WHERE company_name=?", (company_name,))
            conn.commit()
            conn.close()
            print(f"[node_agent] 已设置 {company_name} is_logged_in=0")
        except Exception as e:
            print(f"[node_agent] 设置 is_logged_in 失败: {e}")

        # Step 2: 启动 keepalive daemon
        cmd = [sys.executable, daemon_path,
               "--company", company_name,
               "--project-root", "/home/openclaw",
               "--auto-recover"]
        # 通知目标
        if notify_chat:
            cmd += ["--notify-chat", notify_chat]
        elif contact_name:
            cmd += ["--notify-user", contact_name]

        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"[node_agent] keepalive daemon 已启动: {company_name}")
        except Exception as e:
            self._ws_send({"type": "login_failed", "company_name": company_name,
                           "reason": f"启动 keepalive daemon 失败: {e}"})
            return

        self._ws_send({
            "type": "keepalive_login_progress",
            "company_name": company_name,
            "progress": "保活登录程序已启动，正在打开 12123 登录页...",
        })

        # Step 3: 异步监控 QR 文件和登录进度
        _company = company_name
        _profile = profile_name
        _iport = instance_port
        _ws_send = self._ws_send

        async def _monitor_keepalive():
            from lib.core import _get_data_dir
            data_dir = _get_data_dir()
            health_file = os.path.join(data_dir, f"keepalive_health_{_company}.json")

            qr_sent = False
            last_progress = ""
            deadline = time.time() + 900  # 15 分钟超时

            while time.time() < deadline:
                await asyncio.sleep(3)

                # A. 检查 health file 获取进度
                if os.path.exists(health_file):
                    try:
                        with open(health_file, "r") as f:
                            h = json.load(f)
                        progress = h.get("progress", h.get("state", ""))
                        if progress and progress != last_progress:
                            last_progress = progress
                            _ws_send({
                                "type": "keepalive_login_progress",
                                "company_name": _company,
                                "progress": progress,
                            })
                        if h.get("state") == "logged_in":
                            _ws_send({"type": "login_ok", "company_name": _company})
                            print(f"[node_agent] 保活登录成功: {_company}")
                            return
                    except Exception:
                        pass

                # B. 检查 QR 截图文件
                if not qr_sent:
                    import glob as _glob
                    qr_pattern = os.path.join(data_dir, f"recovery_qr_{_company}_*.png")
                    qr_files = sorted(_glob.glob(qr_pattern))
                    if qr_files:
                        qr_path = qr_files[-1]  # 取最新的
                        try:
                            with open(qr_path, "rb") as f:
                                img_b64 = base64.b64encode(f.read()).decode("utf-8")
                            _ws_send({
                                "type": "qr_code",
                                "company_name": _company,
                                "image_base64": img_b64,
                                "qr_expires_at": (datetime.now() + timedelta(minutes=5)).isoformat(),
                            })
                            qr_sent = True
                            print(f"[node_agent] QR sent for {_company} ({len(img_b64)} bytes)")
                        except Exception as e:
                            print(f"[node_agent] QR read error: {e}")

            # 超时
            if not qr_sent:
                _ws_send({"type": "login_failed", "company_name": _company,
                          "reason": "保活登录超时（15分钟）"})

        asyncio.ensure_future(_monitor_keepalive())

    async def _monitor_existing_keepalive(self, company_name, health_file, data_dir, safe_name):
        """监控已在运行的保活程序，跟踪 QR 更新和登录结果。"""
        import glob as _glob
        last_qr = ""
        last_progress = ""
        deadline = time.time() + 900

        while time.time() < deadline:
            await asyncio.sleep(5)

            # 检查 QR 更新
            qr_pattern = os.path.join(data_dir, f"recovery_qr_{safe_name}_*.png")
            qr_files = sorted(_glob.glob(qr_pattern))
            if qr_files:
                latest = qr_files[-1]
                if latest != last_qr:
                    last_qr = latest
                    try:
                        with open(latest, "rb") as f:
                            img_b64 = base64.b64encode(f.read()).decode("utf-8")
                        self._ws_send({
                            "type": "qr_code", "company_name": company_name,
                            "image_base64": img_b64,
                            "qr_expires_at": (datetime.now() + timedelta(minutes=5)).isoformat(),
                        })
                    except Exception:
                        pass

            # 检查进度
            if os.path.exists(health_file):
                try:
                    with open(health_file, "r") as f:
                        h = json.load(f)
                    progress = h.get("progress", h.get("state", ""))
                    if progress and progress != last_progress:
                        last_progress = progress
                        self._ws_send({
                            "type": "keepalive_login_progress",
                            "company_name": company_name,
                            "progress": progress,
                        })
                    if h.get("state") == "logged_in":
                        self._ws_send({"type": "login_ok", "company_name": company_name})
                        return
                except Exception:
                    pass

            # 检查保活是否还活着
            pid_file = os.path.join(data_dir, f"keepalive_{safe_name}.pid")
            if os.path.exists(pid_file):
                try:
                    with open(pid_file, "r") as f:
                        pid = int(f.read().strip())
                    os.kill(pid, 0)
                except (OSError, ValueError):
                    break  # 进程已死

    def _handle_session_message(self, msg):
        """Forward a chat message from frontend to the active Claude session."""
        company_id = str(msg.get("company_id", ""))
        text = msg.get("text", "")
        self._claude_sessions = getattr(self, '_claude_sessions', {})
        proc = self._claude_sessions.get(company_id)
        if not proc or not proc.stdin:
            print(f"[node_agent] session_message: no active session for {company_id}")
            return
        try:
            import json as _json
            user_msg = _json.dumps({
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}
            }) + "\n"
            proc.stdin.write(user_msg)
            proc.stdin.flush()
            print(f"[node_agent] session_message sent to {company_id}: {text[:60]}")
        except Exception as e:
            print(f"[node_agent] session_message write error: {e}")

    async def _handle_start_keepalive_login(self, msg):
        """Handle start_keepalive_login — 激活保活服务登录（路径A）。"""
        company_name = msg.get("company_name", "")
        profile_name = msg.get("profile_name", "")
        instance_port = msg.get("instance_port")

        print(f"[node_agent] 保活登录: {company_name} (profile={profile_name})")

        # Determine service name
        svc_name = None
        if profile_name:
            svc_name = profile_name.replace("profile_", "keepalive-watchdog-profile_")

        # Try systemctl first
        started = False
        if svc_name:
            try:
                r = subprocess.run(
                    ["systemctl", "--user", "restart", svc_name],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=15
                )
                if r.returncode == 0:
                    started = True
                    print(f"[node_agent] systemctl restart {svc_name} 成功")
            except Exception as e:
                print(f"[node_agent] systemctl 失败: {e}")

        # Fallback: spawn directly
        if not started:
            skill_dir = os.path.dirname(os.path.abspath(__file__))
            daemon_path = os.path.join(skill_dir, "keepalive_daemon.py")
            if os.path.exists(daemon_path):
                try:
                    cmd = [sys.executable, daemon_path, company_name,
                           "--auto-recover", "--no-daemon"]
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                    started = True
                    print(f"[node_agent] 直接 spawn keepalive_daemon: {company_name}")
                except Exception as e:
                    print(f"[node_agent] spawn 失败: {e}")

        if not started:
            self._ws_send({
                "type": "keepalive_login_result",
                "company_name": company_name,
                "ok": False,
                "reason": "service_not_found",
            })
            return

        # Start polling health file in background
        asyncio.ensure_future(self._poll_keepalive_progress(company_name))

    async def _poll_keepalive_progress(self, company_name):
        """轮询 health file，上报保活登录进度。"""
        from lib.core import _get_data_dir
        health_file = os.path.join(_get_data_dir(),
                                   f"keepalive_health_{company_name}.json")

        last_progress = None
        timeout = 600  # 10 分钟超时
        start = time.time()

        while time.time() - start < timeout:
            await asyncio.sleep(3)

            if not os.path.exists(health_file):
                continue

            try:
                with open(health_file, "r") as f:
                    health = json.load(f)
            except Exception:
                continue

            state = health.get("state", "")
            progress = health.get("progress", state)

            if progress != last_progress:
                last_progress = progress
                self._ws_send({
                    "type": "keepalive_login_progress",
                    "company_name": company_name,
                    "progress": progress,
                    "state": state,
                    "last_check": health.get("last_check", ""),
                })

            # Check terminal states
            if state == "ok" and progress == "logged_in":
                self._ws_send({
                    "type": "keepalive_login_result",
                    "company_name": company_name,
                    "ok": True,
                })
                return

            if state == "login_expired" or state == "exited":
                self._ws_send({
                    "type": "keepalive_login_result",
                    "company_name": company_name,
                    "ok": False,
                    "reason": state,
                })
                return

        # Timeout
        self._ws_send({
            "type": "keepalive_login_result",
            "company_name": company_name,
            "ok": False,
            "reason": "timeout",
        })

    def _handle_reporting_schedule_config(self, msg):
        """Handle reporting schedule config from cloud server."""
        schedules = msg.get("schedules", [])
        self._reporting_schedules = schedules
        print(f"[node_agent] 收到定时上报配置: {len(schedules)} 个计划")

    def _reporting_schedule_check_loop(self):
        """Check every 30s if any schedule time matches current time."""
        while self._running:
            time.sleep(30)
            schedules = getattr(self, '_reporting_schedules', [])
            if not schedules:
                continue

            now = datetime.now()
            current_time = now.strftime("%H:%M")

            for sched in schedules:
                if not sched.get("enabled"):
                    continue
                times = sched.get("times", [])
                if isinstance(times, str):
                    try:
                        times = json.loads(times)
                    except Exception:
                        continue
                if current_time in times:
                    print(f"[node_agent] 定时上报触发: {current_time}")
                    asyncio.ensure_future(self._run_sync())

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
            "task_id": task_id,
            "node_id": self.node_id,
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

    # ── Periodic Loops ────────────────────────────────────────

    async def _heartbeat_loop(self):
        """Send heartbeat every HEARTBEAT_INTERVAL seconds."""
        while self._running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            metrics = self._get_system_metrics()
            proc_count = len(metrics.get("important_processes", []))
            print(f"[node_agent] heartbeat: cpu={metrics.get('cpu_percent')}% mem={metrics.get('memory_percent')}% processes={metrics.get('process_count')} important={proc_count}")
            self._ws_send({
                "type": "heartbeat",
                "node_id": self.node_id,
                "active_sessions": len(self._tasks),
                **metrics,
            })

    def _get_system_metrics(self):
        """Collect system resource metrics. Uses psutil if available, falls back to /proc."""
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=1)
            cpu_count = psutil.cpu_count()
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            net = psutil.net_io_counters()
            uptime = int(time.time() - psutil.boot_time())
            proc_count = len(psutil.pids())
            return {
                "cpu_percent": round(cpu, 2),
                "cpu_count": cpu_count,
                "memory_total_gb": round(mem.total / (1024**3), 2),
                "memory_used_gb": round(mem.used / (1024**3), 2),
                "memory_percent": round(mem.percent, 2),
                "disk_total_gb": round(disk.total / (1024**3), 2),
                "disk_used_gb": round(disk.used / (1024**3), 2),
                "disk_percent": round(disk.percent, 2),
                "net_bytes_sent_mb": round(net.bytes_sent / (1024**2), 2),
                "net_bytes_recv_mb": round(net.bytes_recv / (1024**2), 2),
                "uptime_seconds": uptime,
                "process_count": proc_count,
                "processes": self._get_important_processes(),
            }
        except ImportError:
            # Fallback: /proc filesystem
            metrics = {"cpu_percent": 0, "cpu_count": os.cpu_count() or 1,
                       "memory_total_gb": 0, "memory_used_gb": 0, "memory_percent": 0,
                       "disk_total_gb": 0, "disk_used_gb": 0, "disk_percent": 0,
                       "net_bytes_sent_mb": 0, "net_bytes_recv_mb": 0,
                       "uptime_seconds": 0, "process_count": 0,
                       "important_processes": self._get_important_processes()}
            try:
                with open('/proc/meminfo') as f:
                    meminfo = {}
                    for line in f:
                        parts = line.split(':')
                        if len(parts) == 2:
                            meminfo[parts[0].strip()] = int(parts[1].strip().split()[0])
                total = meminfo.get('MemTotal', 0)
                avail = meminfo.get('MemAvailable', 0)
                if total > 0:
                    metrics['memory_total_gb'] = round(total / (1024**2), 2)
                    metrics['memory_used_gb'] = round((total - avail) / (1024**2), 2)
                    metrics['memory_percent'] = round((total - avail) / total * 100, 2)
            except Exception:
                pass
            try:
                with open('/proc/loadavg') as f:
                    metrics['cpu_percent'] = round(float(f.read().split()[0]) * 100 / metrics['cpu_count'], 2)
            except Exception:
                pass
            try:
                metrics['uptime_seconds'] = int(float(open('/proc/uptime').read().split()[0]))
            except Exception:
                pass
            try:
                metrics['process_count'] = len([d for d in os.listdir('/proc') if d.isdigit()])
            except Exception:
                pass
            return metrics

    def _get_important_processes(self):
        """Collect all running processes with real name and description.
        Returns a flat list sorted by memory usage (highest first).
        """
        processes = []
        # Process name → description mapping
        NAME_MAP = {
            "node_agent.py": ("节点代理", "WebSocket 守护进程，负责与中央控制台通信"),
            "keepalive_daemon.py": ("保活服务", "保持 12123 登录态，自动恢复二维码登录"),
            "claude": ("Claude 会话", "AI 查询引擎，执行违章查询/登录任务"),
            "pinchtab server": ("PinchTab 服务", "浏览器实例管理主进程"),
            "pinchtab bridge": ("PinchTab 桥接", "浏览器标签页桥接进程"),
            "chrome": ("Chrome 浏览器", "PinchTab 管理的浏览器进程"),
            "session_bridge.py": ("会话桥接", "通用 Claude 会话桥接模块"),
        }
        try:
            r = subprocess.run(
                ["ps", "aux", "--sort=-%mem"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                universal_newlines=True, timeout=5
            )
            lines = r.stdout.strip().split('\n')
            for line in lines[1:]:
                parts = line.split(None, 10)
                if len(parts) < 11:
                    continue
                cmd = parts[10]
                cpu = float(parts[2])
                mem = float(parts[3])
                rss_mb = round(int(parts[5]) / 1024, 1)

                # Determine process name and description
                proc_name = cmd.strip().split()[0].split('/')[-1] if cmd.strip() else "unknown"
                # Try to find a better name from the command
                desc = ""
                for pattern, (display_name, display_desc) in NAME_MAP.items():
                    if pattern in cmd:
                        proc_name = display_name
                        desc = display_desc
                        break

                # For Chrome renderer/GPU/utility processes, unify as "Chrome 浏览器"
                if "chrome" in proc_name.lower() or "chromium" in proc_name.lower():
                    proc_name = "Chrome 浏览器"
                    if not desc:
                        desc = "PinchTab 管理的浏览器进程"

                processes.append({
                    "name": proc_name,
                    "description": desc,
                    "pid": int(parts[1]),
                    "cpu_percent": cpu,
                    "mem_percent": mem,
                    "rss_mb": rss_mb,
                    "command": cmd[:200],
                })
        except Exception as e:
            print("[node_agent] 进程采集失败: {}".format(e))
        return processes

    async def _keepalive_report_loop(self):
        """Report keepalive status for all companies every KEEPALIVE_REPORT_INTERVAL."""
        while self._running:
            await asyncio.sleep(KEEPALIVE_REPORT_INTERVAL)
            companies_status = self._gather_keepalive_status()
            if companies_status:
                self._ws_send({
                    "type": "keepalive_status",
                    "node_id": self.node_id,
                    "companies": companies_status,
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
            status = {"name": p["company_name"], "is_logged_in": bool(p.get("is_logged_in", 0))}

            # Check keepalive systemd service
            profile_name = p.get("profile_name", "").replace("profile_", "")
            service_name = f"keepalive-watchdog-profile_{profile_name}"
            try:
                r = subprocess.run(
                    ["systemctl", "is-active", service_name],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=5
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
        """Periodic full sync fallback every SYNC_INTERVAL seconds."""
        while self._running:
            await asyncio.sleep(SYNC_INTERVAL)
            print("[node_agent] 定时全量同步...")
            await self._run_sync()


def _province_to_url(province):
    """省份名 → 12123 URL。"""
    m = {
        "四川": "https://sc.122.gov.cn", "广东": "https://gd.122.gov.cn",
        "福建": "https://fj.122.gov.cn", "北京": "https://bj.122.gov.cn",
        "上海": "https://sh.122.gov.cn", "重庆": "https://cq.122.gov.cn",
        "浙江": "https://zj.122.gov.cn", "江苏": "https://js.122.gov.cn",
        "湖北": "https://hb.122.gov.cn", "湖南": "https://hn.122.gov.cn",
        "山东": "https://sd.122.gov.cn", "河南": "https://ha.122.gov.cn",
        "河北": "https://he.122.gov.cn", "安徽": "https://ah.122.gov.cn",
        "江西": "https://jx.122.gov.cn", "陕西": "https://sn.122.gov.cn",
    }
    return m.get(province, "https://sc.122.gov.cn")


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

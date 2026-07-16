#!/usr/bin/env python3
"""
session_bridge.py — 通用 Claude 会话桥接器

独立于违章查询业务的通用模块。桥接 WebSocket ↔ Claude Code 会话，支持
多会话管理、输出过滤、结构化 marker 检测。

可用于任何需要通过 WebSocket 与 Claude Code 交互的场景：
  - 扫码登录对话
  - 查询任务交互
  - 故障排查会话
  - ... 任何 Claude 会话

用法:
  from session_bridge import SessionBridge
  bridge = SessionBridge(ws_send=my_send_func)
  await bridge.create("my-session", "Hello Claude", filter_mode="text_only")
"""

import asyncio
import base64
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime

# ── Configuration defaults ─────────────────────────────────────

DEFAULT_CLAUDE_PATH = "claude"
DEFAULT_MAX_SESSIONS = 3
DEFAULT_IDLE_TIMEOUT = 600       # 10 分钟
DEFAULT_MAX_TURN_TIME = 1800     # 30 分钟
CLEANUP_INTERVAL = 30            # 清理检查间隔

# ── Output filter patterns ────────────────────────────────────

# Tool call tree characters (tree-drawing box characters used by Claude Code)
TOOL_TREE_PATTERN = re.compile(r'^[│├└╭╰\s]*[🔧●⚙️]')
TOOL_TREE_CHARS = set('│├└╭╰')

# Lines that indicate thinking/processing
THINKING_PATTERNS = [
    re.compile(r'●.*思考'),
    re.compile(r'●.*Thinking'),
    re.compile(r'╭──.*工具调用'),
    re.compile(r'╭──.*Tool'),
]

# Lines that look like JSON blocks (tool arguments)
JSON_BLOCK_START = re.compile(r'^\s*[{[]\s*$')


# ── SessionHandle ──────────────────────────────────────────────

class SessionHandle:
    """Represents one active Claude session."""
    def __init__(self, session_id: str, proc: subprocess.Popen,
                 filter_mode: str, markers: list, created_at: float):
        self.session_id = session_id
        self.proc = proc
        self.filter_mode = filter_mode
        self.markers = markers or []
        self.created_at = created_at
        self.last_activity = created_at
        self._stdin = proc.stdin


# ── SessionBridge ──────────────────────────────────────────────

class SessionBridge:
    """通用 Claude 会话桥接器。

    ws_send:      发送 WebSocket 消息的回调函数 callable(msg_dict)
    claude_path:  claude CLI 路径
    max_sessions: 最大并发会话数
    idle_timeout: 会话空闲超时秒数（0 禁用）
    max_turn_time: 单次 turn 最大秒数（0 禁用）
    """

    def __init__(self, ws_send, claude_path=None, max_sessions=None,
                 idle_timeout=None, max_turn_time=None):
        self._ws_send = ws_send
        self._claude_path = claude_path or DEFAULT_CLAUDE_PATH
        self._max_sessions = max_sessions if max_sessions is not None else DEFAULT_MAX_SESSIONS
        self._idle_timeout = idle_timeout if idle_timeout is not None else DEFAULT_IDLE_TIMEOUT
        self._max_turn_time = max_turn_time if max_turn_time is not None else DEFAULT_MAX_TURN_TIME

        self._sessions: dict[str, SessionHandle] = {}
        self._running = True

        # Start cleanup loop
        self._cleanup_task = None

    # ── Public API ─────────────────────────────────────────────

    async def create(self, session_id: str, prompt: str,
                     filter_mode: str = "text_only",
                     markers: list = None):
        """新建或续接 Claude 会话。

        session_id:  唯一会话标识（由调用方生成）
        prompt:      初始提示词
        filter_mode: "text_only" | "keep_thinking" | "full"
        markers:     可选标记列表，如 ["QR_READY", "LOGIN_OK"]
        """
        # Check concurrent limit
        if len(self._sessions) >= self._max_sessions:
            self._emit("session_error", session_id=session_id,
                       error=(f"当前有 {len(self._sessions)} 个会话正在进行"
                              f"（上限 {self._max_sessions}），"
                              f"请稍后重试或取消不用的会话"))
            return

        # Check if session already exists (resume)
        if session_id in self._sessions:
            self._emit("session_error", session_id=session_id,
                       error=f"会话 {session_id} 已存在")
            return

        markers = markers or []

        # Claude CLI: `claude -p <prompt>` = non-interactive, print and exit
        # For continued conversations: `claude -p --continue <prompt>`
        is_resume = False
        if session_id and len(session_id) > 30:  # Looks like a UUID
            is_resume = True

        cmd = [self._claude_path, "-p"]
        if is_resume:
            cmd.append("--continue")
        cmd.append(prompt)

        print(f"[session_bridge] 创建会话: {session_id} (mode={filter_mode}, markers={markers}, resume={is_resume})")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
            )
        except FileNotFoundError:
            self._emit("session_error", session_id=session_id,
                       error=f"Claude CLI 未找到 ({self._claude_path})")
            return

        # Create handle
        now = time.time()
        handle = SessionHandle(
            session_id=session_id,
            proc=proc,
            filter_mode=filter_mode,
            markers=markers,
            created_at=now,
        )
        self._sessions[session_id] = handle

        # Start cleanup if first session
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.ensure_future(self._cleanup_loop())

        self._emit("session_created", session_id=session_id)

        # Start reading stdout in background thread
        t = threading.Thread(
            target=self._read_loop,
            args=(handle,),
            daemon=True,
        )
        t.start()

    async def send(self, session_id: str, text: str):
        """向 Claude 会话发送消息（写入 stdin）。"""
        handle = self._sessions.get(session_id)
        if handle is None:
            self._emit("session_error", session_id=session_id,
                       error=f"会话 {session_id} 不存在")
            return
        if handle.proc.poll() is not None:
            self._emit("session_error", session_id=session_id,
                       error="Claude 进程已退出")
            return

        try:
            handle._stdin.write(text + "\n")
            handle._stdin.flush()
            handle.last_activity = time.time()
        except (BrokenPipeError, OSError) as e:
            self._emit("session_error", session_id=session_id,
                       error=f"写入失败: {e}")
            await self._cleanup_one(session_id, reason="broken_pipe")

    async def cancel(self, session_id: str):
        """终止会话（kill 进程）。"""
        handle = self._sessions.get(session_id)
        if handle is None:
            return

        try:
            handle.proc.terminate()
            # Schedule kill after 10s
            pid = handle.proc.pid
            def _force_kill():
                try:
                    if handle.proc.poll() is None:
                        handle.proc.kill()
                except Exception:
                    pass
            threading.Timer(10, _force_kill).start()
        except Exception:
            pass

        self._emit("session_done", session_id=session_id, reason="cancelled")
        self._sessions.pop(session_id, None)

    def list_sessions(self):
        """列出活跃会话 → session_list_result。"""
        sessions = []
        for sid, h in self._sessions.items():
            sessions.append({
                "session_id": sid,
                "created_at": datetime.fromtimestamp(h.created_at).isoformat(),
                "status": "running" if h.proc.poll() is None else "exited",
                "filter_mode": h.filter_mode,
            })
        self._emit("session_list_result", sessions=sessions)

    def shutdown(self):
        """关闭桥接器，终止所有会话。"""
        self._running = False
        for sid in list(self._sessions.keys()):
            try:
                self._sessions[sid].proc.terminate()
            except Exception:
                pass
        self._sessions.clear()

    # ── Internal ───────────────────────────────────────────────

    def _emit(self, msg_type: str, **kwargs):
        """发送 WebSocket 消息。"""
        msg = {"type": msg_type}
        msg.update(kwargs)
        try:
            self._ws_send(msg)
        except Exception as e:
            print(f"[session_bridge] 发送失败: {e}")

    def _read_loop(self, handle: SessionHandle):
        """后台线程：逐行读取 Claude stdout，过滤后发送到 WS。"""
        session_id = handle.session_id
        proc = handle.proc
        filter_mode = handle.filter_mode
        markers = handle.markers

        # Track JSON block state for text_only / keep_thinking modes
        in_json_block = False

        try:
            for line in proc.stdout:
                line = line.rstrip('\n')
                if not line:
                    continue

                handle.last_activity = time.time()

                # ── Marker detection (independent of filter_mode) ──
                if markers:
                    for marker in markers:
                        pattern = f"__{marker}__"
                        if pattern in line:
                            # Extract payload if present: __MARKER__:payload
                            payload = None
                            parts = line.split(f"__{marker}__", 1)
                            if len(parts) > 1 and parts[1].startswith(":"):
                                payload = parts[1][1:].strip()

                            marker_data = {
                                "session_id": session_id,
                                "marker": marker,
                                "payload": payload,
                            }

                            # ── QR_READY: read image file and base64-encode ──
                            if marker == "QR_READY" and payload:
                                try:
                                    qr_path = payload
                                    if not os.path.isabs(qr_path):
                                        # Try relative to cwd
                                        qr_path = os.path.join(os.getcwd(), payload)
                                    if os.path.exists(qr_path):
                                        with open(qr_path, "rb") as f:
                                            img_b64 = base64.b64encode(f.read()).decode("utf-8")
                                        marker_data["image_base64"] = img_b64
                                        print(f"[session_bridge] QR loaded: {qr_path} ({len(img_b64)} bytes b64)")
                                    else:
                                        print(f"[session_bridge] QR file not found: {qr_path}")
                                except Exception as e:
                                    print(f"[session_bridge] QR read error: {e}")

                            self._emit("session_marker", **marker_data)
                            # Don't skip — text still goes through

                # ── Filter output ──
                if filter_mode == "full":
                    # Pass through everything
                    self._emit("session_chunk", session_id=session_id, text=line)
                    continue

                # Detect JSON blocks (tool arguments — skip in text_only and keep_thinking)
                if JSON_BLOCK_START.match(line):
                    in_json_block = True
                    continue
                if in_json_block:
                    if re.match(r'^\s*[}\]]\s*$', line):
                        in_json_block = False
                    continue

                # Check tool call tree lines
                if line and line[0] in TOOL_TREE_CHARS:
                    continue

                # text_only: also skip thinking lines
                if filter_mode == "text_only":
                    if any(p.search(line) for p in THINKING_PATTERNS):
                        continue

                # Skip empty/whitespace-only after filtering
                stripped = line.strip()
                if not stripped:
                    continue

                # Emit chunk
                self._emit("session_chunk", session_id=session_id, text=line)

        except Exception as e:
            if self._running:
                self._emit("session_error", session_id=session_id,
                           error=f"读取异常: {e}")

        # Process exited
        exit_code = proc.poll()
        if exit_code is None:
            # Still running somehow — shouldn't happen
            return

        if exit_code == 0:
            self._emit("session_done", session_id=session_id, reason="completed")
        else:
            self._emit("session_error", session_id=session_id,
                       error=f"进程异常退出 (exit_code={exit_code})")

        self._sessions.pop(session_id, None)

    async def _cleanup_one(self, session_id: str, reason: str):
        """Clean up a single session."""
        handle = self._sessions.pop(session_id, None)
        if handle is None:
            return
        try:
            if handle.proc.poll() is None:
                handle.proc.terminate()
        except Exception:
            pass
        self._emit("session_done", session_id=session_id, reason=reason)

    async def _cleanup_loop(self):
        """后台清理：检查空闲超时和 turn 超时。"""
        while self._running:
            await asyncio.sleep(CLEANUP_INTERVAL)
            now = time.time()

            for sid in list(self._sessions.keys()):
                handle = self._sessions.get(sid)
                if handle is None:
                    continue

                # Check idle timeout
                if self._idle_timeout > 0:
                    idle = now - handle.last_activity
                    if idle > self._idle_timeout:
                        await self._cleanup_one(sid, reason="idle_timeout")
                        continue

                # Check max turn time
                if self._max_turn_time > 0:
                    elapsed = now - handle.created_at
                    if elapsed > self._max_turn_time:
                        await self._cleanup_one(sid, reason="max_turn_time")
                        continue

                # Check if process still alive
                if handle.proc.poll() is not None:
                    # Process exited but _read_loop hasn't cleaned up yet
                    # _read_loop handles this normally, skip here
                    pass

            # Stop cleanup if no sessions
            if not self._sessions:
                self._cleanup_task = None
                break

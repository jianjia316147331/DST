#!/usr/bin/env python3
"""lib/core.py — shared infrastructure for DST violation query tool.
Constants, path resolution, subprocess runner, output control."""
import json, os, re, io, subprocess, sys, time
from datetime import datetime

#!/usr/bin/env python3
"""
DST违章查询 — 12123 车辆违章查询辅助工具。
跨平台（Windows/Linux）：通过 Python 直接调用，中文参数通过脚本文件或 stdin 传入。
所有 bash 调用使用 `python /path/to/violation_helper.py <subcommand>`。

全局选项（所有子命令共享，必须在子命令之前）：
  --output FILE, -o FILE  将标准输出同时写入文件（UTF-8），绕过终端编码问题
"""

import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time
import re
import io
import urllib.request
from datetime import datetime

# ============================================================
# Global: output file (set before subcommand dispatch)
_OUTPUT_FILE = None
# ============================================================
def _fix_encoding():
    """Reconfigure stdout/stderr to UTF-8. Set PYTHONIOENCODING/PYTHONUTF8 for subprocess.
    Uses multiple strategies: reconfigure() + TextIOWrapper fallback + PYTHONUTF8=1.

    Triple guarantee (Issue #2 fix):
      1. Force-set PYTHONUTF8=1 + PYTHONIOENCODING=utf-8 env vars (override, not setdefault)
      2. TextIOWrapper first (wraps raw buffer, bypasses any console encoding)
      3. reconfigure() as fallback (for streams that support it)
    """
    # Force-override env vars (not setdefault — must override any inherited values)
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"

    # Strategy: wrap raw buffer first (most reliable — bypasses console codec entirely)
    for stream, name in [(sys.stdout, "stdout"), (sys.stderr, "stderr")]:
        try:
            if hasattr(stream, 'buffer'):
                wrapper = io.TextIOWrapper(stream.buffer, encoding='utf-8', errors='replace', line_buffering=True)
                if name == "stdout":
                    sys.stdout = wrapper
                else:
                    sys.stderr = wrapper
        except Exception:
            pass

    # Fallback: reconfigure if TextIOWrapper didn't work (e.g., redirected streams)
    for stream, name in [(sys.stdout, "stdout"), (sys.stderr, "stderr")]:
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Also wrap stdin for reading piped UTF-8 input
    try:
        if hasattr(sys.stdin, 'buffer') and sys.stdin.encoding and sys.stdin.encoding.lower() not in ('utf-8', 'utf8'):
            sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

# NOTE: _fix_encoding() is NOT called at module level.
# It is called by the dispatcher (violation_helper.py) BEFORE any output.
# Running it inside an imported module breaks stdout/stderr wrapping
# because a second call wraps the already-wrapped TextIOWrapper,
# causing the original wrapper to be GC'd, closing the underlying fd.
class _TeeWriter:
    """Write to both original stdout and an output file."""
    def __init__(self, original, filepath):
        self.original = original
        self.file = open(filepath, "w", encoding="utf-8")
    def write(self, s):
        self.original.write(s)
        self.file.write(s)
        self.flush()
    def flush(self):
        self.original.flush()
        self.file.flush()
    def close(self):
        self.file.close()
    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

def _setup_output_file():
    """Parse --output/-o from sys.argv before subcommand dispatch."""
    global _OUTPUT_FILE
    for i in range(1, len(sys.argv)):
        arg = sys.argv[i]
        if arg in ("--output", "-o") and i + 1 < len(sys.argv):
            _OUTPUT_FILE = sys.argv[i + 1]
            # Remove -o and its value: pop higher index first
            sys.argv.pop(i + 1)  # remove value
            sys.argv.pop(i)      # remove flag
            # Use current sys.stdout (may already be UTF-8-wrapped by _fix_encoding)
            # NOT sys.__stdout__ which bypasses the encoding fix
            sys.stdout = _TeeWriter(sys.stdout, _OUTPUT_FILE)
            return
# ============================================================
# Constants: Province / License Plate Mappings
# ============================================================

# 所有省份单位用户扫码登录入口统一为公安部平台
UNIT_LOGIN_URL = "https://gab.122.gov.cn/m/login?t=2"

# 各省份 12123 首页 URL（用于登录后导航、车辆列表等操作）
PROVINCE_URL = {
    "广东": "https://gd.122.gov.cn", "北京": "https://bj.122.gov.cn",
    "上海": "https://sh.122.gov.cn", "重庆": "https://cq.122.gov.cn",
    "浙江": "https://zj.122.gov.cn", "江苏": "https://js.122.gov.cn",
    "湖北": "https://hb.122.gov.cn", "湖南": "https://hn.122.gov.cn",
    "山东": "https://sd.122.gov.cn", "福建": "https://fj.122.gov.cn",
    "天津": "https://tj.122.gov.cn", "河北": "https://he.122.gov.cn",
    "山西": "https://sx.122.gov.cn", "辽宁": "https://ln.122.gov.cn",
    "吉林": "https://jl.122.gov.cn", "黑龙江": "https://hl.122.gov.cn",
    "安徽": "https://ah.122.gov.cn", "江西": "https://jx.122.gov.cn",
    "河南": "https://ha.122.gov.cn", "广西": "https://gx.122.gov.cn",
    "海南": "https://hi.122.gov.cn", "贵州": "https://gz.122.gov.cn",
    "云南": "https://yn.122.gov.cn", "西藏": "https://xz.122.gov.cn",
    "陕西": "https://sn.122.gov.cn", "甘肃": "https://gs.122.gov.cn",
    "青海": "https://qh.122.gov.cn", "宁夏": "https://nx.122.gov.cn",
    "新疆": "https://xj.122.gov.cn", "内蒙古": "https://nm.122.gov.cn",
    "四川": "https://sc.122.gov.cn",
}

LICENSE_TO_PROVINCE = {
    "京": "北京", "津": "天津", "沪": "上海", "渝": "重庆",
    "冀": "河北", "晋": "山西", "辽": "辽宁", "吉": "吉林",
    "黑": "黑龙江", "苏": "江苏", "浙": "浙江", "皖": "安徽",
    "闽": "福建", "赣": "江西", "鲁": "山东", "豫": "河南",
    "鄂": "湖北", "湘": "湖南", "粤": "广东", "桂": "广西",
    "琼": "海南", "川": "四川", "贵": "贵州", "云": "云南",
    "藏": "西藏", "陕": "陕西", "甘": "甘肃", "青": "青海",
    "宁": "宁夏", "新": "新疆", "蒙": "内蒙古",
}

LICENSE_TO_URL = {k: PROVINCE_URL[v] for k, v in LICENSE_TO_PROVINCE.items()}
def _find_project_root():
    """Walk up from cwd to find the project root (directory containing .claude/)."""
    d = os.getcwd()
    for _ in range(6):
        if os.path.isdir(os.path.join(d, '.claude')):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    # Fallback: assume cwd is the project root
    return os.getcwd()

def _get_query_dir():
    return os.path.join(_find_project_root(), "violation_query")

def _get_screenshot_dir():
    d = os.path.join(_get_query_dir(), "screenshots")
    os.makedirs(d, exist_ok=True)
    return d

def _get_report_dir():
    d = os.path.join(_get_query_dir(), "reports")
    os.makedirs(d, exist_ok=True)
    return d

def _get_data_dir():
    d = os.path.join(_get_query_dir(), "data")
    os.makedirs(d, exist_ok=True)
    return d

def _ensure_subdirs():
    _get_screenshot_dir()
    _get_report_dir()
    _get_data_dir()


LOGIN_KEYWORDS = ["已登录", "登录成功", "好了", "ok", "OK", "好的"]

# ── Unified login state detection constants ────────────────────
# Two-tier approach:
#   Tier 1 (URL+DOM): initial "are we logged in?" check — reliable, no false positives
#   Tier 2 (keyword): QR scan poll detection — "did the user scan?" transition check
#
# Post-login business indicators (Tier 2, also used as confirmation in Tier 1).
# Ordered by reliability: "退出" is the strongest signal.
POST_LOGIN_KEYWORDS = [
    # Only keywords that are ONLY visible when logged in.
    # Public nav items like "业务办理"/"违法查询"/"首页" are visible to
    # everyone and must NOT be here — they cause false positives.
    "退出",
    "我的主页",
    "公司列表", "公司名称", "选择单位", "请选择单位",
    "车辆管理", "租赁车辆",
]

# Login page indicators — signals that we're NOT logged in (or session expired).
# Note: "单位用户登录"/"个人用户登录" may appear on logged-in pages as
# account-switch links in nav bars. They are only treated as login-page signals
# when NO post-login indicators are present.
LOGIN_PAGE_KEYWORDS = [
    "单位用户登录", "个人用户登录", "扫码登录",
    "请使用交管12123", "请打开交管12123",
]

# Legacy alias for backward compatibility with poll-login
LOGIN_INDICATORS = POST_LOGIN_KEYWORDS

# URL pattern for login page detection (Tier 1)
LOGIN_PAGE_URL_PATTERN = "gab.122.gov.cn/m/login"

QR_EXPIRED_KEYWORDS = ["过期", "失效", "重新"]
def _lark_cli_path():
    """Detect lark-cli path, compatible with Windows/MINGW64."""
    path = os.environ.get("LARK_CLI", "")
    if path and os.path.exists(path):
        return path
    # Check npm global install locations
    npm_root = _run_silent(["npm", "root", "-g"]).stdout.strip()
    if npm_root:
        candidates = [
            os.path.join(npm_root, "..", "lark-cli.cmd"),
            os.path.join(npm_root, "..", "lark-cli"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
    # shutil.which
    found = shutil.which("lark-cli")
    if found: return found
    for name in ["lark-cli.cmd", "lark-cli.exe"]:
        found = shutil.which(name)
        if found: return found
    return "lark-cli"

def _pinchtab_path():
    """Detect pinchtab path, compatible with Windows/MINGW64."""
    path = os.environ.get("PINCHTAB", "")
    if path and os.path.exists(path):
        return path
    candidates = [
        os.path.join(os.environ.get("APPDATA", ""), "npm", "pinchtab.cmd"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "pinchtab"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    found = shutil.which("pinchtab")
    if found: return found
    for name in ["pinchtab.cmd", "pinchtab.exe"]:
        found = shutil.which(name)
        if found: return found
    return "pinchtab"

def _node_path():
    """Find node.exe. Multi-strategy search (Issue #8 fix).

    Priority:
      1. NODE env var
      2. Same dir as lark-cli.cmd (most reliable — npm global prefix)
      3. shutil.which("node") / shutil.which("node.exe")
      4. Common install locations (Program Files, nvm, fnm)
      5. npm prefix -g (slow, timeout 3s)

    Returns absolute path to node.exe or "node" as last resort.
    """
    # Fast path: explicit env var
    path = os.environ.get("NODE", "")
    if path and os.path.exists(path):
        return os.path.abspath(path)

    # Check same dir as lark-cli (npm global prefix — most likely location)
    lark = _lark_cli_path()
    if lark and os.path.exists(lark):
        npm_dir = os.path.dirname(lark)
        node_exe = os.path.join(npm_dir, "node.exe")
        if os.path.exists(node_exe):
            return node_exe

    # Check PATH (fast)
    for name in ["node", "node.exe"]:
        found = shutil.which(name)
        if found and os.path.exists(found):
            return found

    # Check common install locations
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs", "node.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "nodejs", "node.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "fnm", "node.exe"),
        os.path.join(os.environ.get("APPDATA", ""), "nvm"),
    ]
    # Also check nvm subdirs for current version
    nvm_dir = candidates[-1]
    if os.path.isdir(nvm_dir):
        for d in sorted(os.listdir(nvm_dir), reverse=True):
            node = os.path.join(nvm_dir, d, "node.exe")
            if os.path.exists(node):
                candidates.append(node)
                break
    for c in candidates:
        if c and os.path.exists(c):
            return c

    # Slow path: npm prefix -g (with strict timeout)
    try:
        npm_root = _run_silent(["npm", "root", "-g"], timeout=3).stdout.strip()
        if npm_root:
            npm_dir = os.path.dirname(npm_root)
            node_exe = os.path.join(npm_dir, "node.exe")
            if os.path.exists(node_exe):
                return node_exe
    except Exception:
        pass

    return "node"

def _lark_cli_base_cmd():
    """Return base command list to invoke lark-cli via node.exe directly.

    Bypasses lark-cli.cmd (which goes through cmd.exe), preventing argument
    corruption when passing --content JSON with Chinese/emoji characters.

    Issue #8 fix: Never silently fall back to .cmd. If node.exe + run.js can't
    be resolved, emit a loud warning and try multiple locations for run.js.
    Falls back to lark-cli.cmd only as absolute last resort.
    """
    lark_cmd = _lark_cli_path()
    if not lark_cmd:
        return ["lark-cli"]

    # If it's not a .cmd file, use directly
    if not lark_cmd.endswith(".cmd"):
        return [lark_cmd]

    npm_dir = os.path.dirname(lark_cmd)
    node_exe = _node_path()

    # Search for run.js in multiple locations
    run_js_candidates = [
        os.path.join(npm_dir, "node_modules", "@larksuite", "cli", "scripts", "run.js"),
        os.path.join(npm_dir, "node_modules", "lark-cli", "scripts", "run.js"),
        os.path.join(npm_dir, "..", "node_modules", "@larksuite", "cli", "scripts", "run.js"),
        # If npm_dir is the project local node_modules/.bin
        os.path.join(npm_dir, "..", "@larksuite", "cli", "scripts", "run.js"),
    ]

    run_js = None
    for candidate in run_js_candidates:
        if os.path.exists(candidate):
            run_js = candidate
            break

    if run_js and node_exe and os.path.exists(node_exe):
        return [node_exe, run_js]

    # Still not found — try resolving lark-cli through npm to find the real script
    if not run_js:
        try:
            # Try to find the actual lark-cli package root
            ls_result = _run_silent(["npm", "ls", "-g", "@larksuite/cli", "--depth=0", "--json"], timeout=3)
            npm_data = json.loads(ls_result.stdout)
            deps = npm_data.get("dependencies", {})
            cli_info = deps.get("@larksuite/cli", {})
            cli_path = cli_info.get("resolved") or cli_info.get("path") or ""
            if cli_path:
                # cli_path may be like file:../../../path
                if cli_path.startswith("file:"):
                    cli_path = cli_path[5:]
                cli_dir = os.path.dirname(cli_path) if not os.path.isdir(cli_path) else cli_path
                run_js = os.path.join(cli_dir, "scripts", "run.js")
                if os.path.exists(run_js):
                    return [node_exe, run_js]
        except Exception:
            pass

    # Last resort: warn and fall back to .cmd (may corrupt arguments with Chinese/emoji)
    if not run_js:
        print(f"WARNING: lark-cli run.js not found, falling back to .cmd (Chinese/emoji may corrupt)", file=sys.stderr)
        if not node_exe or not os.path.exists(str(node_exe)):
            print(f"WARNING: node.exe not resolved, using .cmd PATH resolution", file=sys.stderr)
    elif not node_exe or not os.path.exists(str(node_exe)):
        print(f"WARNING: node.exe not found, falling back to .cmd", file=sys.stderr)

    return [lark_cmd]

def _pinchtab_base_cmd():
    """Return base command list to invoke pinchtab. Resolves to full path.
    PinchTab is a Go binary (.exe), no cmd.exe wrapper needed."""
    return [_pinchtab_path()]

# Lazy-cached paths to avoid expensive npm lookups on every _run call
def _run(args, **kwargs):
    """Run a subprocess, return CompletedProcess. UTF-8 safe.
    Automatically resolves pinchtab/lark-cli paths:
    - Sentinel strings "pinchtab"/"lark-cli" → resolved
    - Pre-resolved .cmd paths → replaced with direct node invocation (bypasses cmd.exe)
    Sets PYTHONIOENCODING=utf-8 and PYTHONUTF8=1 for subprocess.

    Tab isolation: if VIOLATION_TAB_ID env var is set, automatically injects
    --tab <id> into pinchtab commands that support it (nav, eval, click, snap,
    text, find, wait, screenshot, reload, back).  Commands like 'tab' and
    daemon-level commands are excluded.  This allows multiple sessions to
    operate on different tabs within the same browser instance without
    interfering with each other."""
    # Only check sentinel strings - cached path getters cause infinite recursion
    # when called during initialization (lark_cli_path -> run_silent -> run -> get_lark_cached...)
    if args and args[0] == "pinchtab":
        args = _pinchtab_base_cmd() + list(args[1:])
    if args and args[0] == "lark-cli":
        args = _lark_cli_base_cmd() + list(args[1:])

    # ── Instance isolation: inject --server <url> for pinchtab commands ──
    instance_port = os.environ.get("VIOLATION_INSTANCE_PORT", "")
    _is_pt = args and args[0] and ("pinchtab" in os.path.basename(str(args[0])).lower())
    if instance_port and _is_pt and len(args) > 1:
        args = list(args)
        args.insert(1, "--server")
        args.insert(2, f"http://127.0.0.1:{instance_port}")

    # ── Tab isolation: inject --tab <id> for tab-aware pinchtab commands ──
    _TAB_AWARE = frozenset({
        "nav", "eval", "click", "dblclick", "snap", "text", "find", "wait",
        "screenshot", "reload", "back", "attr", "box", "capture", "check",
        "checked", "console", "count", "enabled", "fill", "hover",
    })
    tab_id = os.environ.get("VIOLATION_TAB_ID", "")
    if tab_id and _is_pt and len(args) > 1:
        # Find subcommand position: scan for the first arg matching a known
        # subcommand name (skipping global flags like --server and their values).
        cmd_idx = None
        for i in range(1, len(args)):
            if args[i] in _TAB_AWARE:
                cmd_idx = i
                break
        if cmd_idx is not None:
            args.insert(cmd_idx + 1, "--tab")
            args.insert(cmd_idx + 2, tab_id)

    env = kwargs.pop("env", os.environ.copy())
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    timeout = kwargs.pop("timeout", 30)
    p = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, timeout=timeout, **kwargs
    )
    # Python 3.6 compat: decode manually
    p.stdout = p.stdout.decode("utf-8", errors="replace") if isinstance(p.stdout, bytes) else p.stdout
    p.stderr = p.stderr.decode("utf-8", errors="replace") if isinstance(p.stderr, bytes) else p.stderr
    return p

def _parse_tab_ids(tab_output):
    """Extract tab IDs from pinchtab tab JSON output.
    Returns list of hex tab IDs (strings)."""
    try:
        data = json.loads(tab_output)
        return [t["id"] for t in data.get("tabs", []) if "id" in t]
    except (json.JSONDecodeError, KeyError):
        return []
def _run_silent(args, **kwargs):
    """Run and return CompletedProcess, suppressing errors."""
    try:
        return _run(args, **kwargs)
    except Exception:
        return subprocess.CompletedProcess(args, -1, stdout="", stderr="")

def _read_stdin_text():
    """Read stdin as UTF-8 text. Returns empty string on TTY or when no data available.
    Non-blocking: uses select to check for available data, avoiding hang on pipe stdin."""
    if sys.stdin.isatty():
        return ""
    try:
        # Use select to check if data is available (non-blocking)
        import select
        if select.select([sys.stdin.buffer], [], [], 0.0)[0]:
            return sys.stdin.buffer.read().decode("utf-8")
        return ""
    except Exception:
        try:
            return sys.stdin.buffer.read().decode("utf-8")
        except Exception:
            return ""

def _read_stdin_json(defaults):
    """Read JSON from stdin and update defaults dict."""
    text = _read_stdin_text()
    if text:
        try:
            defaults.update(json.loads(text))
        except (json.JSONDecodeError, ValueError):
            pass

def cmd_get_dir():
    """Output the base directory path for violation query files. Creates dir and subdirs."""
    target = _get_query_dir()
    os.makedirs(target, exist_ok=True)
    _ensure_subdirs()
    print(target)

def cmd_license_lookup():
    """Read a license plate char from stdin or args, output JSON {province, url}."""
    text = _read_stdin_text()
    data = json.loads(text) if text else {}
    char = data.get("char", "")
    if not char and len(sys.argv) > 2:
        char = sys.argv[2]
    if not char:
        print(json.dumps({"error": "missing char"}))
        sys.exit(1)
    province = LICENSE_TO_PROVINCE.get(char, "")
    url = LICENSE_TO_URL.get(char, "")
    print(json.dumps({"province": province, "url": url, "char": char}))

def cmd_province_url():
    """Read province name, output homepage URL."""
    text = _read_stdin_text()
    data = json.loads(text) if text else {}
    province = data.get("province", "")
    if not province and len(sys.argv) > 2:
        province = sys.argv[2]
    url = PROVINCE_URL.get(province, "")
    print(url)

def cmd_province_login_url():
    """Read province name, output province homepage URL.
    Note: actual login uses UNIT_LOGIN_URL (gab.122.gov.cn). This returns
    the province-specific URL for post-login operations like vehlist navigation.
    Usage: echo '{"province":"四川"}' | python3 violation_helper.py province-login-url
    """
    text = _read_stdin_text()
    data = json.loads(text) if text else {}
    province = data.get("province", "")
    if not province and len(sys.argv) > 2:
        province = sys.argv[2]
    url = PROVINCE_URL.get(province, "")
    print(url)

def cmd_get_screenshot_dir():
    """Output the screenshots subdirectory path."""
    print(_get_screenshot_dir())

def cmd_get_report_dir():
    """Output the reports subdirectory path."""
    print(_get_report_dir())

def cmd_get_data_dir():
    """Output the data subdirectory path."""
    print(_get_data_dir())

def cmd_pt_find():
    """Run pinchtab find (Chinese args passed via subprocess list, no shell)."""
    args = sys.argv[2:]
    result = _run(["pinchtab", "find"] + args)
    print(result.stdout, end="")

def cmd_pt_wait():
    """Run pinchtab wait (Chinese args passed via subprocess list, no shell)."""
    args = sys.argv[2:]
    result = _run(["pinchtab", "wait"] + args)
    print(result.stdout, end="")

def cmd_prepare_dir():
    """Create the query output directory + subdirs and print its path."""
    target = _get_query_dir()
    os.makedirs(target, exist_ok=True)
    _ensure_subdirs()
    print(target)

def cmd_init():
    """Initialize environment for violation query.
    1. Deploy helper + lib/ to TEMP/violation_query_helper/ (bypass Chinese-in-path issues)
    2. Detect lark-cli and pinchtab paths, write to temp files
    3. Create output directory (violation_query/)
    4. Detect Python path
    5. --clean: remove stale files (7d+ QR, non-today progress, 7d+ tab entries)
    Output: JSON with all paths.
    """
    # Parse optional --clean flag
    do_clean = "--clean" in sys.argv

    temp_dir = os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"

    # 1. Deploy helper + lib/ as a directory tree to temp
    #    source dir = dir containing this script (the skill directory)
    #    target dir = /tmp/violation_query_helper/
    src_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    deploy_dir = os.path.join(temp_dir, "violation_query_helper")

    # Remove old deployment if any, then recreate
    if os.path.exists(deploy_dir):
        shutil.rmtree(deploy_dir)
    os.makedirs(deploy_dir, exist_ok=True)

    # Copy violation_helper.py
    src_helper = os.path.join(src_dir, "violation_helper.py")
    dst_helper = os.path.join(deploy_dir, "violation_helper.py")
    if os.path.exists(src_helper):
        shutil.copy2(src_helper, dst_helper)

    # Copy entire lib/ directory
    src_lib = os.path.join(src_dir, "lib")
    dst_lib = os.path.join(deploy_dir, "lib")
    if os.path.isdir(src_lib):
        shutil.copytree(src_lib, dst_lib)

    # 2. Detect and persist tool paths
    lark = _lark_cli_path()
    with open(os.path.join(temp_dir, "lark_cli_path.txt"), "w", encoding="utf-8") as f:
        f.write(lark)

    pt = _pinchtab_path()
    with open(os.path.join(temp_dir, "pinchtab_path.txt"), "w", encoding="utf-8") as f:
        f.write(pt)

    # 3. Create output dir + subdirs (screenshots/, reports/, data/)
    query_dir = _get_query_dir()
    os.makedirs(query_dir, exist_ok=True)
    _ensure_subdirs()
    # Also write query_dir to a temp file for external scripts to read without encoding issues
    with open(os.path.join(temp_dir, "query_dir.txt"), "w", encoding="utf-8") as f:
        f.write(query_dir)

    # 4. Detect python path
    py_path = sys.executable

    # 5. --clean: remove stale files
    cleaned = {}
    if do_clean:
        cleaned = _clean_stale_files(query_dir)

    result = {
        "helper": dst_helper,
        "deploy_dir": deploy_dir,
        "lark_cli": lark,
        "pinchtab": pt,
        "query_dir": query_dir,
        "python": py_path,
        "cleaned": cleaned,
    }
    print(json.dumps(result, ensure_ascii=False))


def _clean_stale_files(query_dir):
    """Remove stale files older than 7 days. Returns counts of what was cleaned.
    Targets:
      - screenshots/*.png older than 7d (stale QR codes)
      - data/details_progress_* not matching today's date
      - data/tab_registry.json entries older than 7d
      - data/keepalive_health_* not matching today's date (NOT current health file)
    """
    import glob
    now = time.time()
    cutoff_7d = now - 7 * 86400
    today = time.strftime("%Y-%m-%d")
    counts = {"qr_files": 0, "progress_files": 0, "tab_entries": 0, "health_files": 0}

    data_dir = os.path.join(query_dir, "data")
    screenshot_dir = os.path.join(query_dir, "screenshots")

    # Clean old QR screenshots
    if os.path.isdir(screenshot_dir):
        for f in glob.glob(os.path.join(screenshot_dir, "*.png")):
            try:
                if os.path.getmtime(f) < cutoff_7d:
                    os.remove(f)
                    counts["qr_files"] += 1
            except OSError:
                pass

    if os.path.isdir(data_dir):
        # Clean non-today progress files
        for f in glob.glob(os.path.join(data_dir, "details_progress_*.json")):
            try:
                # Keep today's progress files
                basename = os.path.basename(f)
                if today not in basename:
                    os.remove(f)
                    counts["progress_files"] += 1
            except OSError:
                pass

        # Clean non-today health files
        for f in glob.glob(os.path.join(data_dir, "keepalive_health_*.json")):
            try:
                basename = os.path.basename(f)
                if today not in basename:
                    os.remove(f)
                    counts["health_files"] += 1
            except OSError:
                pass

        # Clean 7d+ entries from tab_registry.json
        reg_path = os.path.join(data_dir, "tab_registry.json")
        if os.path.exists(reg_path):
            try:
                with open(reg_path, "r", encoding="utf-8") as f:
                    registry = json.load(f)
                entries = registry.get("entries", [])
                fresh = []
                removed = 0
                for entry in entries:
                    created = entry.get("created_at", "")
                    if created:
                        try:
                            ts = datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
                            if ts < cutoff_7d:
                                removed += 1
                                continue
                        except (ValueError, OSError):
                            pass
                    fresh.append(entry)
                if removed > 0:
                    registry["entries"] = fresh
                    registry["cleaned_at"] = datetime.now().isoformat()
                    with open(reg_path, "w", encoding="utf-8") as f:
                        json.dump(registry, f, ensure_ascii=False, indent=2)
                counts["tab_entries"] = removed
            except (json.JSONDecodeError, OSError):
                pass

    return counts
def cmd_run_js():
    """Execute JavaScript from a file via pinchtab eval.
    Args: --file /path/to/script.js
    """
    p = {"file": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--file" and i + 1 < len(args):
            p["file"] = args[i + 1]; i += 2
        else:
            i += 1

    if not p["file"] or not os.path.exists(p["file"]):
        print("ERROR: --file required and must exist", file=sys.stderr)
        sys.exit(1)

    with open(p["file"], "r", encoding="utf-8") as f:
        js_code = f.read().strip()

    if not js_code:
        print("ERROR: empty JS file", file=sys.stderr)
        sys.exit(1)

    result = _run(["pinchtab", "eval", js_code])
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.returncode != 0:
        sys.exit(result.returncode)

def cmd_pinchtab_path():
    """Output the full path to pinchtab executable."""
    print(_pinchtab_path())

def cmd_lark_cli_path():
    """Output the full path to lark-cli executable."""
    print(_lark_cli_path())

def cmd_get_login_url():
    """Output the national unit login URL."""
    print(UNIT_LOGIN_URL)


def cmd_release_tab():
    """Close current session tab and clean up tab_registry.json.

    Reads VIOLATION_TAB_ID from env, finds its label in tab_registry.json,
    then delegates to session_manager.py release for the actual close + cleanup.

    Outputs JSON: {ok, tab_id, label, delegated}
    """
    tab_id = os.environ.get("VIOLATION_TAB_ID", "").strip()

    if not tab_id:
        print(json.dumps({"ok": False, "error": "VIOLATION_TAB_ID not set"},
                         ensure_ascii=False))
        return

    # Find label for this tab_id in the registry
    label = None
    try:
        registry_path = os.path.join(
            os.getcwd(), "violation_query", "data", "tab_registry.json"
        )
        if os.path.exists(registry_path):
            with open(registry_path, "r", encoding="utf-8") as f:
                registry = json.load(f)
            for lbl, info in registry.items():
                if info.get("tab_id") == tab_id:
                    label = lbl
                    break
    except Exception:
        pass

    if not label:
        # Tab not in registry — might have been cleaned already.
        # Still try to close the tab via pinchtab directly.
        closed = False
        try:
            result = _run(["pinchtab", "close", tab_id], timeout=10)
            closed = result.returncode == 0
        except Exception:
            pass
        print(json.dumps({
            "ok": True, "tab_id": tab_id,
            "closed": closed, "registry_cleaned": False,
            "note": "tab not found in registry, closed directly",
        }, ensure_ascii=False))
        return

    # Delegate to session_manager.py release
    session_mgr = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "session_manager.py"
    )
    result = _run(
        [sys.executable or "python3", session_mgr, "release", "--label", label],
        timeout=15
    )
    print(json.dumps({
        "ok": True,
        "tab_id": tab_id,
        "label": label,
        "delegated": True,
    }, ensure_ascii=False))


def cmd_mark_task_done():
    """Write a lightweight completion marker file for the current task.

    Reads VIOLATION_TAB_ID from env.  Writes .task_done_<tab_id>.json
    to violation_query/data/ with completion metadata.

    Options (all optional, for audit):
      --company <name>
      --query-type single|batch
      --vehicles-queried <n>
      --new-violations <n>
      --changed-violations <n>
      --new-vehicles <n>
      --new-points <n>
      --new-fine <n>
      --failed-vehicles <n>

    Outputs JSON: {ok, marker_file, tab_id}
    """
    tab_id = os.environ.get("VIOLATION_TAB_ID", "").strip()
    if not tab_id:
        print(json.dumps({"ok": False, "error": "VIOLATION_TAB_ID not set"},
                         ensure_ascii=False))
        return

    # Parse optional flags
    company = ""
    query_type = ""
    vehicles_queried = 0
    new_violations = 0
    changed_violations = 0
    new_vehicles = 0
    new_points = 0
    new_fine = 0
    failed_vehicles = 0
    missing_vehicles = 0
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            company = args[i + 1]; i += 2
        elif args[i] == "--query-type" and i + 1 < len(args):
            query_type = args[i + 1]; i += 2
        elif args[i] == "--vehicles-queried" and i + 1 < len(args):
            vehicles_queried = int(args[i + 1]); i += 2
        elif args[i] == "--new-violations" and i + 1 < len(args):
            new_violations = int(args[i + 1]); i += 2
        elif args[i] == "--changed-violations" and i + 1 < len(args):
            changed_violations = int(args[i + 1]); i += 2
        elif args[i] == "--new-vehicles" and i + 1 < len(args):
            new_vehicles = int(args[i + 1]); i += 2
        elif args[i] == "--new-points" and i + 1 < len(args):
            new_points = int(args[i + 1]); i += 2
        elif args[i] == "--new-fine" and i + 1 < len(args):
            new_fine = int(args[i + 1]); i += 2
        elif args[i] == "--failed-vehicles" and i + 1 < len(args):
            failed_vehicles = int(args[i + 1]); i += 2
        elif args[i] == "--missing-vehicles" and i + 1 < len(args):
            missing_vehicles = int(args[i + 1]); i += 2
        else:
            i += 1

    data_dir = os.path.join(os.getcwd(), "violation_query", "data")
    os.makedirs(data_dir, exist_ok=True)
    marker_file = os.path.join(data_dir, f".task_done_{tab_id}.json")

    marker = {
        "tab_id": tab_id,
        "company": company,
        "query_type": query_type,
        "completed_at": datetime.now().isoformat(),
    }
    if vehicles_queried:
        marker["vehicles_queried"] = vehicles_queried
    if new_violations:
        marker["new_violations"] = new_violations
    if changed_violations:
        marker["changed_violations"] = changed_violations
    if new_vehicles:
        marker["new_vehicles"] = new_vehicles
    if new_points:
        marker["new_points"] = new_points
    if new_fine:
        marker["new_fine"] = new_fine
    if failed_vehicles:
        marker["failed_vehicles"] = failed_vehicles
    if missing_vehicles:
        marker["missing_vehicles"] = missing_vehicles

    try:
        with open(marker_file, "w", encoding="utf-8") as f:
            json.dump(marker, f, ensure_ascii=False, indent=2)
        print(json.dumps({"ok": True, "marker_file": marker_file, "tab_id": tab_id},
                         ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))


def cmd_check_task_done():
    """Check whether a completion marker exists for a tab.

    --tab-id <id>   check specific tab
    (if omitted, reads VIOLATION_TAB_ID from env)

    Outputs JSON: {done: true/false, marker_file: path or null, marker: {...}}
    """
    tab_id = ""
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--tab-id" and i + 1 < len(args):
            tab_id = args[i + 1]; i += 2
        else:
            i += 1

    if not tab_id:
        tab_id = os.environ.get("VIOLATION_TAB_ID", "").strip()

    if not tab_id:
        print(json.dumps({"done": False, "error": "no tab_id specified or in env"},
                         ensure_ascii=False))
        return

    marker_file = os.path.join(
        os.getcwd(), "violation_query", "data", f".task_done_{tab_id}.json"
    )
    if os.path.exists(marker_file):
        try:
            with open(marker_file, "r", encoding="utf-8") as f:
                marker = json.load(f)
            print(json.dumps({"done": True, "marker_file": marker_file, "marker": marker},
                             ensure_ascii=False))
        except Exception:
            print(json.dumps({"done": True, "marker_file": marker_file, "marker": None},
                             ensure_ascii=False))
    else:
        print(json.dumps({"done": False, "marker_file": marker_file, "marker": None},
                         ensure_ascii=False))


def cmd_cleanup_stale_tabs():
    """Garbage-collect zombie tabs.

    Thin delegate to session_manager.py cleanup-stale, which implements
    three-tier detection (completion marker → progress activity → age timeout).

    Options (forwarded as-is):
      --idle-hours <n>       Progress-file idle threshold (default 2)
      --max-age-hours <n>    Absolute age threshold (default 6)
      --instance-port <p>    Only clean tabs on a specific instance
      --dry-run              Report without cleaning

    Outputs JSON: {ok, cleaned: [...], kept: [...], summary}
    """
    session_mgr = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "session_manager.py"
    )
    # Forward all arguments after the subcommand name (sys.argv[0]=helper,
    # sys.argv[1]=cleanup-stale-tabs, sys.argv[2:]=user flags)
    args = sys.argv[2:]
    result = _run([sys.executable or "python3", session_mgr, "cleanup-stale"] + args,
                  timeout=30)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)


def _touch_tab_activity(tab_id):
    """Update last_activity timestamp in tab_registry.json for the given tab_id.

    Best-effort: failures are silently ignored so they never break the
    main operation (e.g. open-vehicle, collect-violations).
    """
    if not tab_id:
        return
    try:
        data_dir = _get_data_dir()
        registry_path = os.path.join(data_dir, "tab_registry.json")
        if not os.path.exists(registry_path):
            return
        with open(registry_path, "r", encoding="utf-8") as f:
            registry = json.load(f)
        updated = False
        for info in registry.values():
            if info.get("tab_id") == tab_id:
                info["last_activity"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                updated = True
                break
        if updated:
            with open(registry_path, "w", encoding="utf-8") as f:
                json.dump(registry, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def cmd_cleanup():
    """Run the cleanup daemon (oneshot mode). Delegates to cleanup_daemon.py."""
    cleanup_script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "cleanup_daemon.py"
    )
    args = sys.argv[2:]  # forward all flags after "cleanup"
    # Auto-add --project-root if not explicitly provided
    if "--project-root" not in args:
        args = ["--project-root", os.path.expanduser("~")] + args
    result = _run([sys.executable or "python3", cleanup_script] + args, timeout=120)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    sys.exit(result.returncode)


# Rate-limit / feng-kong indicators from 12123 platform
RATE_LIMIT_KEYWORDS = [
    "频繁", "异常操作", "强制退出", "黑名单", "限制使用",
    "第三方软件", "爬取", "泄露", "法律责任", "暂停服务",
    "操作过于频繁", "请稍后再试", "访问被拒绝", "account locked",
    "suspended", "rate limit", "too many requests",
]


# LOGIN_INDICATORS is set above (line 218) to POST_LOGIN_KEYWORDS.
# Do NOT override with LOGIN_KEYWORDS — those are chat-reply keywords
# ("已登录", "好的") meant for Feishu message matching, not browser page
# detection. poll-login uses LOGIN_INDICATORS for browser-side login
# detection and needs POST_LOGIN_KEYWORDS ("退出", "我的主页", …).


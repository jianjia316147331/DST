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

# ============================================================
# Global: output file (set before subcommand dispatch)
# ============================================================
_OUTPUT_FILE = None

# ============================================================
# Fix Windows console encoding (GBK -> UTF-8)
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

_fix_encoding()

# ============================================================
# Global: tee output to file (bypass terminal encoding)
# ============================================================
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

# ============================================================
# SQLite 数据库 Schema 与目录管理
# ============================================================

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    query_date TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER REFERENCES companies(id),
    plate_number TEXT NOT NULL,
    plate_type TEXT,
    plate_type_label TEXT,
    status_code TEXT,
    status_label TEXT,
    inspection_date TEXT,
    unprocessed_count INTEGER DEFAULT 0,
    query_date TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id INTEGER REFERENCES vehicles(id),
    plate_number TEXT NOT NULL,
    plate_type TEXT,
    plate_type_label TEXT,
    violation_time TEXT,
    violation_location TEXT,
    violation_behavior TEXT,
    violation_code TEXT,
    fine_amount REAL DEFAULT 0,
    points INTEGER DEFAULT 0,
    handling_status TEXT,
    handling_status_label TEXT,
    payment_status TEXT,
    payment_status_label TEXT,
    authority TEXT,
    province TEXT,
    city TEXT,
    unique_id TEXT,
    processing_time TEXT,
    data_update_time TEXT,
    first_collection_time TEXT,
    query_date TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_vehicles_company ON vehicles(company_id);
CREATE INDEX IF NOT EXISTS idx_vehicles_plate ON vehicles(plate_number);
CREATE INDEX IF NOT EXISTS idx_violations_vehicle ON violations(vehicle_id);
CREATE INDEX IF NOT EXISTS idx_violations_plate ON violations(plate_number);
CREATE INDEX IF NOT EXISTS idx_violations_date ON violations(violation_time);
CREATE TABLE IF NOT EXISTS profiles (
    company_name TEXT PRIMARY KEY,
    profile_name TEXT NOT NULL,
    profile_id TEXT,
    platform_url TEXT NOT NULL,
    instance_port INTEGER,
    last_login TEXT,
    is_logged_in INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_profiles_company ON profiles(company_name);
ALTER TABLE profiles ADD COLUMN is_logged_in INTEGER DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_violations_query_date ON violations(query_date);
"""

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
    return _find_project_root()

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

def _get_db_path():
    return os.path.join(_get_data_dir(), "violations.db")

def _init_db():
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    # Split schema to handle ALTER TABLE migration for existing databases
    for stmt in DB_SCHEMA.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e) or "already exists" in str(e):
                pass  # migration already applied
            else:
                raise
    conn.commit()
    conn.close()
    return db_path

def _ensure_subdirs():
    _get_screenshot_dir()
    _get_report_dir()
    _get_data_dir()

# --- Appendix 1: 号牌种类 cod 映射 ---
PLATE_TYPE_MAP = {
    "02": "小型汽车", "01": "大型汽车",
    "52": "小型新能源车辆", "51": "大型新能源车辆",
    "24": "警用摩托", "03": "使馆汽车", "04": "领馆汽车",
    "05": "境外汽车", "06": "外籍汽车", "07": "普通摩托车",
    "08": "轻便摩托车", "09": "使馆摩托车", "10": "领馆摩托车",
    "11": "境外摩托车", "12": "外籍摩托车", "13": "低速车",
    "14": "拖拉机", "15": "挂车", "16": "教练汽车",
    "17": "教练摩托车", "20": "临时入境汽车",
    "21": "临时入境摩托车", "22": "临时行驶车", "23": "警用汽车",
}

# --- Appendix 2: 车辆状态 cod 映射 ---
VEHICLE_STATUS_MAP = {
    "A": "正常", "B": "转出", "C": "被盗抢", "D": "停驶",
    "E": "注销", "G": "违法未处理", "H": "海关监管",
    "I": "事故未处理", "J": "嫌疑车", "K": "查封",
    "L": "暂扣", "M": "强制注销", "N": "事故逃逸",
    "O": "锁定", "Q": "逾期未检验",
}

# --- records 层: handlingStatus cod 映射 ---
HANDLING_STATUS_MAP = {
    "-1": "已删除", "0": "未处理", "1": "已处理",
    "2": "已转出", "9": "无需处理",
}

# --- records 层: paymentStatus cod 映射 ---
PAYMENT_STATUS_MAP = {
    "0": "未缴款", "1": "已缴款", "9": "无需缴款",
}

# --- 响应码说明: data.code ---
RESPONSE_CODE_MAP = {
    "80000": "成功",
    "80001": "无车辆数据",
    "80007": "车辆为转出状态",
    "30050": "缺少必要的参数",
    "30051": "请求数量过大",
    "90000": "失败",
}


# ============================================================
# Constants: Message Templates
# ============================================================

LOGIN_KEYWORDS = ["已登录", "登录成功", "好了", "ok", "OK", "好的"]

# Browser login indicators — keyword matches from pinchtab text/snap output
# that indicate the 12123 page has been logged in successfully.
# After scanning QR, 12123 lands on a company selection list page.
LOGIN_INDICATORS = [
    "公司列表", "公司名称", "请选择", "选择单位",
    "租赁车辆", "机动车", "违法", "业务办理",
    "退出", "首页", "确定",
]
QR_EXPIRED_KEYWORDS = ["过期", "失效", "重新"]

# ============================================================
# Helpers
# ============================================================

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
_PINCHTAB_CACHED = None
_LARK_CLI_CACHED = None

def _get_pinchtab_cached():
    global _PINCHTAB_CACHED
    if _PINCHTAB_CACHED is None:
        _PINCHTAB_CACHED = _pinchtab_path()
    return _PINCHTAB_CACHED

def _get_lark_cli_cached():
    global _LARK_CLI_CACHED
    if _LARK_CLI_CACHED is None:
        _LARK_CLI_CACHED = _lark_cli_path()
    return _LARK_CLI_CACHED

def _run(args, **kwargs):
    """Run a subprocess, return CompletedProcess. UTF-8 safe.
    Automatically resolves pinchtab/lark-cli paths:
    - Sentinel strings "pinchtab"/"lark-cli" → resolved
    - Pre-resolved .cmd paths → replaced with direct node invocation (bypasses cmd.exe)
    Sets PYTHONIOENCODING=utf-8 and PYTHONUTF8=1 for subprocess."""
    # Only check sentinel strings - cached path getters cause infinite recursion
    # when called during initialization (lark_cli_path -> run_silent -> run -> get_lark_cached...)
    if args and args[0] == "pinchtab":
        args = _pinchtab_base_cmd() + list(args[1:])
    if args and args[0] == "lark-cli":
        args = _lark_cli_base_cmd() + list(args[1:])
    env = kwargs.pop("env", os.environ.copy())
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    timeout = kwargs.pop("timeout", 30)
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=timeout, **kwargs
    )

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

# ============================================================
# Subcommand: get-dir
# ============================================================

def cmd_get_dir():
    """Output the base directory path for violation query files. Creates dir and subdirs."""
    target = _get_query_dir()
    os.makedirs(target, exist_ok=True)
    _ensure_subdirs()
    print(target)

# ============================================================
# Subcommand: license-lookup
# ============================================================

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

# ============================================================
# Subcommand: province-url
# ============================================================

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

# ============================================================
# Subcommand: gen-qr-msg
# ============================================================

def cmd_gen_qr_msg():
    """Generate QR notification post message JSON.
    Args (JSON on stdin or CLI flags):
      --image-key KEY
      --platform "12123公安部" (省份信息，用于展示)
      --company "xxx公司"
      --date "2026-05-21"
      --target-type personal|group
      --user-id ou_xxx       (group @ target)
      --user-name 姓名        (group @ target)
    Output: JSON to stdout.
    """
    p = _parse_qr_msg_args()

    title = "🔑 自动查询12123违章信息 - 需要您扫码登录"
    platform_str = f"🌍 平台：{p['platform']}\n" if p.get('platform') else ""
    header_text = (
        f"📋 自动化查询12123车辆违章\n"
        f"{platform_str}"
        f"🏢 公司：{p['company']}\n"
        f"🕐 时间：{p['date']}\n\n"
        f"📱 请使用「交管12123」APP 扫描下方二维码登录\n\n"
    )

    if p["target_type"] == "group" and p.get("user_id"):
        reply_hint = "④ 登录成功后，在群中回复「已登录」"
        content = [
            [{"tag": "at", "user_id": p["user_id"], "user_name": p.get("user_name", "")},
             {"tag": "text", "text": f" 请扫码登录12123查询违章\n\n{header_text}"}],
            [{"tag": "img", "image_key": p["image_key"]}],
            [{"tag": "text", "text": f"\n📝 登录步骤：\n① 打开交管12123 APP\n② 扫一扫上方二维码\n③ 完成人脸识别\n{reply_hint}"}]
        ]
    else:
        reply_hint = "④ 登录成功后，在此飞书对话中回复「已登录」"
        content = [
            [{"tag": "text", "text": header_text}],
            [{"tag": "img", "image_key": p["image_key"]}],
            [{"tag": "text", "text": f"\n📝 登录步骤：\n① 打开交管12123 APP\n② 扫一扫上方二维码\n③ 完成人脸识别\n{reply_hint}"}]
        ]

    msg = {"zh_cn": {"title": title, "content": content}}
    print(json.dumps(msg, ensure_ascii=False))

def _parse_qr_msg_args():
    p = {"image_key": "", "platform": "", "company": "", "date": "",
         "target_type": "personal", "user_id": "", "user_name": ""}
    text = _read_stdin_text()
    if text:
        try:
            d = json.loads(text)
            p.update(d)
            return p
        except (json.JSONDecodeError, ValueError):
            pass
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--image-key" and i + 1 < len(args):
            p["image_key"] = args[i + 1]; i += 2
        elif args[i] == "--platform" and i + 1 < len(args):
            p["platform"] = args[i + 1]; i += 2
        elif args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        elif args[i] == "--date" and i + 1 < len(args):
            p["date"] = args[i + 1]; i += 2
        elif args[i] == "--target-type" and i + 1 < len(args):
            p["target_type"] = args[i + 1]; i += 2
        elif args[i] == "--user-id" and i + 1 < len(args):
            p["user_id"] = args[i + 1]; i += 2
        elif args[i] == "--user-name" and i + 1 < len(args):
            p["user_name"] = args[i + 1]; i += 2
        else:
            i += 1
    return p

# ============================================================
# Subcommand: gen-qr-fallback
# ============================================================

def cmd_gen_qr_fallback():
    """Generate fallback text-only post message JSON."""
    p = {"target_type": "personal", "user_id": "", "user_name": ""}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--target-type" and i + 1 < len(args):
            p["target_type"] = args[i + 1]; i += 2
        elif args[i] == "--user-id" and i + 1 < len(args):
            p["user_id"] = args[i + 1]; i += 2
        elif args[i] == "--user-name" and i + 1 < len(args):
            p["user_name"] = args[i + 1]; i += 2
        else:
            i += 1

    title = "🔑 自动查询12123违章信息 - 需要您扫码登录"
    if p["target_type"] == "group" and p.get("user_id"):
        content = [[
            {"tag": "at", "user_id": p["user_id"], "user_name": p.get("user_name", "")},
            {"tag": "text", "text": " 请扫码登录12123查询违章\n\n📝 登录步骤：\n① 打开交管12123 APP\n② 扫一扫上方二维码\n③ 完成人脸识别\n④ 登录成功后，在群中回复「已登录」"}
        ]]
    else:
        content = [[
            {"tag": "text", "text": "📱 请使用「交管12123」APP 扫描上方二维码登录\n\n📝 登录步骤：\n① 打开交管12123 APP\n② 扫一扫上方二维码\n③ 完成人脸识别\n④ 登录成功后，在此飞书对话中回复「已登录」"}
        ]]

    msg = {"zh_cn": {"title": title, "content": content}}
    print(json.dumps(msg, ensure_ascii=False))

# ============================================================
# Subcommand: gen-result-msg
# ============================================================

def cmd_gen_result_msg():
    """Generate query completion notification post message JSON."""
    p = {
        "company": "", "date": "", "vehicle_count": "0",
        "violation_count": "0", "fine_amount": "0",
        "db_path": "", "report_path": "",
        "target_type": "personal", "user_id": "", "user_name": ""
    }
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        for key in ["company", "date", "vehicle_count", "violation_count",
                     "fine_amount", "db_path", "report_path", "target_type",
                     "user_id", "user_name"]:
            if args[i] == f"--{key.replace('_', '-')}" and i + 1 < len(args):
                p[key] = args[i + 1]; i += 2; break
        else:
            i += 1

    title = "✅ 12123违章查询完成"
    summary = f"📊 查询完成\n🏢 {p['company']} 🕐 {p['date']}\n🚗 {p['vehicle_count']}台 ⚠️ {p['violation_count']}条 💰 {p['fine_amount']}元"

    content_blocks = [[{"tag": "text", "text": f"{summary}\n\n📄 报告已保存至本地：\n{p.get('report_path', '')}\n\n🗄️ 数据库已保存至：\n{p.get('db_path', '')}\n\n数据来源于12123平台，仅供参考。"}]]

    msg = {"zh_cn": {"title": title, "content": content_blocks}}
    print(json.dumps(msg, ensure_ascii=False))

# ============================================================
# Subcommand: upload-image
# ============================================================

def cmd_upload_image():
    """Upload an image to Feishu and return image_key.
    Args: --dir /path/to/screenshots --file login_qrcode_xxx.png
    """
    p = {"dir": "", "file": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--dir" and i + 1 < len(args):
            p["dir"] = args[i + 1]; i += 2
        elif args[i] == "--file" and i + 1 < len(args):
            p["file"] = args[i + 1]; i += 2
        else:
            i += 1

    lark = _lark_cli_path()
    result = _run(
        [lark, "im", "images", "create", "--as", "bot",
         "--file", f"image=./{p['file']}",
         "--data", '{"image_type":"message"}'],
        cwd=p["dir"]
    )

    image_key = ""
    try:
        d = json.loads(result.stdout)
        image_key = d.get("data", {}).get("image_key", "") or d.get("image_key", "")
    except (json.JSONDecodeError, ValueError):
        pass

    print(image_key)

# ============================================================
# Subcommand: send-msg
# ============================================================

def cmd_send_msg():
    """Send a post message via lark-cli.
    Args: --msg-file /path/to/msg.json [--user-id ou_xxx | --chat-id oc_xxx] [--as bot|user]
    """
    p = {"msg_file": "", "user_id": "", "chat_id": "", "as": "bot"}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--msg-file" and i + 1 < len(args):
            p["msg_file"] = args[i + 1]; i += 2
        elif args[i] == "--user-id" and i + 1 < len(args):
            p["user_id"] = args[i + 1]; i += 2
        elif args[i] == "--chat-id" and i + 1 < len(args):
            p["chat_id"] = args[i + 1]; i += 2
        elif args[i] == "--as" and i + 1 < len(args):
            p["as"] = args[i + 1]; i += 2
        else:
            i += 1

    with open(p["msg_file"], "r", encoding="utf-8") as f:
        content = f.read()

    lark = _lark_cli_path()
    cmd = [lark, "im", "+messages-send", "--as", p["as"],
           "--msg-type", "post", "--content", content]
    if p.get("chat_id"):
        cmd += ["--chat-id", p["chat_id"]]
    elif p.get("user_id"):
        cmd += ["--user-id", p["user_id"]]

    result = _run(cmd)
    # Validate response: must have ok=true and message_id, else fail loudly
    try:
        d = json.loads(result.stdout) if result.stdout and result.stdout.strip() else {}
        if not d.get("ok"):
            err = d.get("error", {}).get("message", result.stdout[:200])
            print(f"SEND_FAILED: {err}", file=sys.stderr)
            print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
            sys.exit(1)
        if not d.get("data", {}).get("message_id"):
            print(f"SEND_FAILED: no message_id in response", file=sys.stderr)
            print(result.stdout, end="")
            sys.exit(1)
    except json.JSONDecodeError:
        print(f"SEND_FAILED: invalid JSON response: {result.stdout[:200] if result.stdout else '(empty)'}", file=sys.stderr)
        print(result.stdout, end="") if result.stdout else None
        sys.exit(1)
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")

# ============================================================
# Subcommand: send-image-msg
# ============================================================

def cmd_send_image_msg():
    """Send an image message (fallback path)."""
    p = {"dir": "", "file": "", "user_id": "", "chat_id": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--dir" and i + 1 < len(args):
            p["dir"] = args[i + 1]; i += 2
        elif args[i] == "--file" and i + 1 < len(args):
            p["file"] = args[i + 1]; i += 2
        elif args[i] == "--user-id" and i + 1 < len(args):
            p["user_id"] = args[i + 1]; i += 2
        elif args[i] == "--chat-id" and i + 1 < len(args):
            p["chat_id"] = args[i + 1]; i += 2
        else:
            i += 1

    lark = _lark_cli_path()
    cmd = [lark, "im", "+messages-send", "--as", "bot"]
    if p.get("chat_id"):
        cmd += ["--chat-id", p["chat_id"]]
    elif p.get("user_id"):
        cmd += ["--user-id", p["user_id"]]
    cmd += ["--image", f"./{p['file']}"]

    result = _run(cmd, cwd=p["dir"])
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")

# ============================================================
# Subcommand: init-db
# ============================================================

def cmd_init_db():
    """Initialize SQLite database and return path."""
    db_path = _init_db()
    print(db_path)

# ============================================================
# Subcommand: db-insert-company
# ============================================================

def cmd_db_insert_company():
    """Upsert a company record. If name exists, return existing id; else insert new.
    Args (stdin JSON or CLI): --name --query-date
    Returns JSON with company_id."""
    p = {"name": "", "query_date": ""}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--name" and i + 1 < len(args):
            p["name"] = args[i + 1]; i += 2
        elif args[i] == "--query-date" and i + 1 < len(args):
            p["query_date"] = args[i + 1]; i += 2
        else:
            i += 1
    _init_db()
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT id FROM companies WHERE name = ?", (p["name"],))
    row = cur.fetchone()
    if row:
        company_id = row[0]
        conn.execute("UPDATE companies SET query_date = ? WHERE id = ?",
                     (p["query_date"], company_id))
    else:
        cur = conn.execute("INSERT INTO companies (name, query_date) VALUES (?, ?)",
                           (p["name"], p["query_date"]))
        company_id = cur.lastrowid
    conn.commit()
    conn.close()
    print(json.dumps({"company_id": company_id}))

# ============================================================
# Subcommand: db-insert-vehicle
# ============================================================

def cmd_db_insert_vehicle():
    """Upsert a vehicle record. If plate_number + company_id exists, update; else insert.
    Args (stdin JSON or CLI):
    --company-id --plate-number --plate-type --plate-type-label --status-code
    --status-label --inspection-date --unprocessed-count --query-date
    Returns JSON with vehicle_id."""
    p = {"company_id": 0, "plate_number": "", "plate_type": "", "plate_type_label": "",
         "status_code": "", "status_label": "", "inspection_date": "",
         "unprocessed_count": 0, "query_date": ""}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        for key in ["company-id", "plate-number", "plate-type", "plate-type-label",
                     "status-code", "status-label", "inspection-date",
                     "unprocessed-count", "query-date"]:
            if args[i] == f"--{key}" and i + 1 < len(args):
                p[key.replace("-", "_")] = args[i + 1]; i += 2; break
        else:
            i += 1
    _init_db()
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "SELECT id FROM vehicles WHERE plate_number = ? AND company_id = ?",
        (p["plate_number"], p["company_id"]))
    row = cur.fetchone()
    if row:
        vehicle_id = row[0]
        conn.execute(
            """UPDATE vehicles SET plate_type=?, plate_type_label=?,
               status_code=?, status_label=?, inspection_date=?,
               unprocessed_count=?, query_date=?
               WHERE id=?""",
            (p["plate_type"], p["plate_type_label"],
             p["status_code"], p["status_label"], p["inspection_date"],
             int(p["unprocessed_count"]), p["query_date"], vehicle_id))
    else:
        cur = conn.execute(
            """INSERT INTO vehicles (company_id, plate_number, plate_type, plate_type_label,
               status_code, status_label, inspection_date, unprocessed_count, query_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (p["company_id"], p["plate_number"], p["plate_type"], p["plate_type_label"],
             p["status_code"], p["status_label"], p["inspection_date"],
             int(p["unprocessed_count"]), p["query_date"]))
        vehicle_id = cur.lastrowid
    conn.commit()
    conn.close()
    print(json.dumps({"vehicle_id": vehicle_id}))

# ============================================================
# Subcommand: db-insert-violation
# ============================================================

def _upsert_violation(conn, data):
    """Internal: upsert a single violation record into an open sqlite3 connection.
    Match by natural key (plate_number + violation_time + violation_location + violation_behavior).
    If exists, update status/fine/points; else insert new.
    Does NOT commit — caller is responsible for conn.commit().
    Returns violation_id."""
    plate = data.get("plate_number", "")
    vtime = data.get("violation_time", "")
    vloc = data.get("violation_location", "")
    vbeh = data.get("violation_behavior", "")
    cur = conn.execute(
        """SELECT id FROM violations
           WHERE plate_number = ? AND violation_time = ?
           AND violation_location = ? AND violation_behavior = ?""",
        (plate, vtime, vloc, vbeh))
    row = cur.fetchone()
    if row:
        violation_id = row[0]
        conn.execute(
            """UPDATE violations SET
               handling_status=?, handling_status_label=?,
               payment_status=?, payment_status_label=?,
               fine_amount=?, points=?, vehicle_id=?,
               query_date=?, authority=?, unique_id=?,
               processing_time=?, data_update_time=?
               WHERE id=?""",
            (data.get("handling_status", ""), data.get("handling_status_label", ""),
             data.get("payment_status", ""), data.get("payment_status_label", ""),
             data.get("fine_amount", 0), data.get("points", 0),
             data.get("vehicle_id", 0), data.get("query_date", ""),
             data.get("authority", ""), data.get("unique_id", ""),
             data.get("processing_time", ""), data.get("data_update_time", ""),
             violation_id))
    else:
        cur = conn.execute(
            """INSERT INTO violations (vehicle_id, plate_number, plate_type, plate_type_label,
               violation_time, violation_location, violation_behavior, violation_code,
               fine_amount, points, handling_status, handling_status_label,
               payment_status, payment_status_label, authority, province, city,
               unique_id, processing_time, data_update_time, first_collection_time, query_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (data.get("vehicle_id", 0), plate, data.get("plate_type", ""),
             data.get("plate_type_label", ""), vtime,
             vloc, vbeh,
             data.get("violation_code", ""), data.get("fine_amount", 0), data.get("points", 0),
             data.get("handling_status", ""), data.get("handling_status_label", ""),
             data.get("payment_status", ""), data.get("payment_status_label", ""),
             data.get("authority", ""), data.get("province", ""), data.get("city", ""),
             data.get("unique_id", ""), data.get("processing_time", ""),
             data.get("data_update_time", ""), data.get("first_collection_time", ""),
             data.get("query_date", "")))
        violation_id = cur.lastrowid
    return violation_id


def _collect_detail_to_db_record(detail, plate, query_date):
    """Map a collect-violations detail dict (from _parse_detail_popup) to DB schema dict."""
    return {
        "vehicle_id": 0,
        "plate_number": plate,
        "plate_type": detail.get("type", ""),
        "plate_type_label": detail.get("type", ""),
        "violation_time": detail.get("time", ""),
        "violation_location": detail.get("location", ""),
        "violation_behavior": detail.get("behavior", ""),
        "violation_code": "",
        "fine_amount": detail.get("fine", 0),
        "points": detail.get("points", 0),
        "handling_status": "0" if detail.get("unprocessed") else "1",
        "handling_status_label": detail.get("status", ""),
        "payment_status": "0" if detail.get("payment") == "未缴款" else ("1" if detail.get("payment") == "已缴款" else "9"),
        "payment_status_label": detail.get("payment", ""),
        "authority": detail.get("authority", ""),
        "province": "",
        "city": "",
        "unique_id": "",
        "processing_time": "",
        "data_update_time": "",
        "first_collection_time": "",
        "query_date": query_date,
    }


def cmd_db_insert_violation():
    """Upsert a violation record. Match by natural key (plate + time + location + behavior).
    Args (stdin JSON):
    {vehicle_id, plate_number, plate_type, plate_type_label, violation_time,
     violation_location, violation_behavior, violation_code, fine_amount, points,
     handling_status, handling_status_label, payment_status, payment_status_label,
     authority, province, city, unique_id, processing_time, data_update_time,
     first_collection_time, query_date}
    Returns JSON with violation_id."""
    p = {}
    _read_stdin_json(p)
    _init_db()
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    vid = _upsert_violation(conn, p)
    conn.commit()
    conn.close()
    print(json.dumps({"violation_id": vid}))

# ============================================================
# Subcommand: profile-lookup
# ============================================================

def cmd_profile_lookup():
    """Look up a company's profile mapping.
    Args: --company "公司名"
    Returns: JSON {found: true, profile_name, profile_id, platform_url, instance_port, last_login, is_logged_in}
             or {found: false} if not registered.
    """
    p = {"company": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        else:
            i += 1

    _init_db()
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "SELECT company_name, profile_name, profile_id, platform_url, instance_port, last_login, is_logged_in FROM profiles WHERE company_name = ?",
        (p["company"],))
    row = cur.fetchone()
    conn.close()

    if row:
        print(json.dumps({
            "found": True,
            "company_name": row[0],
            "profile_name": row[1],
            "profile_id": row[2],
            "platform_url": row[3],
            "instance_port": row[4],
            "last_login": row[5],
            "is_logged_in": bool(row[6])
        }, ensure_ascii=False))
    else:
        print(json.dumps({"found": False}))

# ============================================================
# Subcommand: profile-register
# ============================================================

def cmd_profile_register():
    """Register a company -> profile mapping after successful login.
    Args: --company "公司名" --profile-name "default" --profile-id "prof_xxx" --platform-url "https://bj.122.gov.cn" [--instance-port 9868]
    Upserts: if company exists, updates profile info; else inserts new.
    """
    p = {"company": "", "profile_name": "", "profile_id": "", "platform_url": "", "instance_port": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        elif args[i] == "--profile-name" and i + 1 < len(args):
            p["profile_name"] = args[i + 1]; i += 2
        elif args[i] == "--profile-id" and i + 1 < len(args):
            p["profile_id"] = args[i + 1]; i += 2
        elif args[i] == "--platform-url" and i + 1 < len(args):
            p["platform_url"] = args[i + 1]; i += 2
        elif args[i] == "--instance-port" and i + 1 < len(args):
            p["instance_port"] = args[i + 1]; i += 2
        else:
            i += 1

    _init_db()
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        """INSERT INTO profiles (company_name, profile_name, profile_id, platform_url, instance_port, last_login, is_logged_in)
           VALUES (?, ?, ?, ?, ?, ?, 1)
           ON CONFLICT(company_name) DO UPDATE SET
           profile_name=excluded.profile_name, profile_id=excluded.profile_id,
           platform_url=excluded.platform_url, instance_port=excluded.instance_port,
           last_login=excluded.last_login, is_logged_in=1""",
        (p["company"], p["profile_name"], p["profile_id"],
         p["platform_url"], p["instance_port"], now))
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "company": p["company"], "profile_name": p["profile_name"]},
                     ensure_ascii=False))

# ============================================================
# Subcommand: profile-logout
# ============================================================

def cmd_profile_logout():
    """Mark a company profile as logged out and stop keep-alive.
    Args: --company "公司名"
    Called when: user explicitly logs out, keep-alive detects session expired,
    or get-login-type detects page returned to login screen.
    """
    p = {"company": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        else:
            i += 1

    _init_db()
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE profiles SET is_logged_in = 0 WHERE company_name = ?",
        (p["company"],))
    updated = conn.total_changes
    conn.commit()
    conn.close()
    print(json.dumps({
        "ok": True,
        "company": p["company"],
        "logged_out": updated > 0
    }, ensure_ascii=False))

# ============================================================
# Subcommand: search-user
# ============================================================

def cmd_search_user():
    """Search Feishu user by name.
    Args: --query "张三" [--exclude-external-users]
    """
    p = {"query": "", "exclude_external": False}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--query" and i + 1 < len(args):
            p["query"] = args[i + 1]; i += 2
        elif args[i] == "--exclude-external-users":
            p["exclude_external"] = True; i += 1
        else:
            i += 1

    lark = _lark_cli_path()
    cmd = [lark, "contact", "+search-user", "--query", p["query"], "--as", "user"]
    if p["exclude_external"]:
        cmd.append("--exclude-external-users")

    result = _run(cmd)
    print(result.stdout, end="")

# ============================================================
# Subcommand: search-chat
# ============================================================

def cmd_search_chat():
    """Search Feishu group chat by name."""
    p = {"query": ""}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--query" and i + 1 < len(args):
            p["query"] = args[i + 1]; i += 2
        else:
            i += 1

    lark = _lark_cli_path()
    result = _run([
        lark, "api", "GET", "/open-apis/im/v1/chats/search",
        "--params", json.dumps({"query": p["query"], "page_size": 20}),
        "--as", "bot"
    ])
    print(result.stdout, end="")

# ============================================================
# Subcommand: batch-get-id
# ============================================================

def cmd_batch_get_id():
    """Look up Feishu user by mobile number."""
    p = {"mobile": ""}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--mobile" and i + 1 < len(args):
            p["mobile"] = args[i + 1]; i += 2
        else:
            i += 1

    lark = _lark_cli_path()
    result = _run([
        lark, "api", "POST", "/open-apis/contact/v3/users/batch_get_id",
        "--data", json.dumps({"mobiles": [p["mobile"]]}),
        "--params", json.dumps({"user_id_type": "open_id"}),
        "--as", "bot"
    ])
    print(result.stdout, end="")

# ============================================================
# Subcommand: get-screenshot-dir
# ============================================================

def cmd_get_screenshot_dir():
    """Output the screenshots subdirectory path."""
    print(_get_screenshot_dir())

# ============================================================
# Subcommand: get-report-dir
# ============================================================

def cmd_get_report_dir():
    """Output the reports subdirectory path."""
    print(_get_report_dir())

# ============================================================
# Subcommand: get-data-dir
# ============================================================

def cmd_get_data_dir():
    """Output the data subdirectory path."""
    print(_get_data_dir())

# ============================================================
# Subcommand: pt-find
# ============================================================

def cmd_pt_find():
    """Run pinchtab find (Chinese args passed via subprocess list, no shell)."""
    args = sys.argv[2:]
    result = _run(["pinchtab", "find"] + args)
    print(result.stdout, end="")

# ============================================================
# Subcommand: pt-wait
# ============================================================

def cmd_pt_wait():
    """Run pinchtab wait (Chinese args passed via subprocess list, no shell)."""
    args = sys.argv[2:]
    result = _run(["pinchtab", "wait"] + args)
    print(result.stdout, end="")

# ============================================================
# Subcommand: poll-login
# ============================================================
#
# Exit codes:
#   0 = LOGIN_CONFIRMED (user replied with 已登录/OK/etc)
#   1 = TIMEOUT (no reply in time window, QR may or may not be expired)
#   2 = QR_EXPIRED (user explicitly reported QR expired in chat)
#   3 = QR_EXPIRED_DETECTED (polling exhausted, browser check confirmed QR expired)
#
# Interval strategy:
#   0-60s:   10s interval (6 polls/min, casual wait)
#   60-180s:  5s interval (24 polls in 2min, aggressive—user may have missed notification)
#   180-300s: 15s interval (8 polls in 2min, tapering off)
#   Total: ~38 polls over 5 minutes
# ============================================================

# QR expiration indicators to check in browser page
QR_EXPIRED_PAGE_INDICATORS = [
    "二维码已过期", "已失效", "请重新刷新", "refresh", "expired",
]

_QR_CHECK_JS = """
(function() {
  var body = document.body.textContent || '';
  var indicators = ['二维码已过期', '已失效', '请重新刷新', '二维码失效'];
  for (var i = 0; i < indicators.length; i++) {
    if (body.indexOf(indicators[i]) !== -1) return 'expired:' + indicators[i];
  }
  // Check if QR image is still present and not a stale/error placeholder
  var imgs = document.querySelectorAll('img');
  var hasQR = false;
  for (var j = 0; j < imgs.length; j++) {
    var src = imgs[j].src || '';
    if (src.indexOf('qr') !== -1 || src.indexOf('code') !== -1 || src.indexOf('login') !== -1) {
      hasQR = true;
      break;
    }
  }
  // If no QR-related images found at all, consider it expired
  if (!hasQR && imgs.length === 0) return 'expired:no_images';
  return 'ok';
})()
"""

def cmd_poll_login():
    """Poll Feishu messages waiting for login receipt.
    Dynamic polling intervals + browser QR expiration check on timeout.

    Args:
      --chat-id CHAT_ID         Feishu chat to poll (required)
      --target-user-id OU_XXX   Target user open_id (required)
      --qr-msg-id OM_XXX        QR notification message_id (required)
      --qr-sent-as bot|user     Who sent the QR message. When 'bot', skip reply_to
                                matching (in bot-user P2P chats, user messages are
                                always directed at the bot). When 'user' (group chat),
                                require reply_to == qr_msg_id. Default: user.
      --max-duration SECONDS    (default 300 = 5min). Replaces --max-retries.
      --check-qr                Enable browser QR expiration check after polling exhausted.
      --check-login             Enable browser auto-detect login. Every ~30s, runs pinchtab
                                text+snap to check if page shows logged-in state (unit user
                                indicators). If detected, exits 0 immediately without waiting
                                for Feishu reply. Default: false.
      --qr-refresh-count N      Current QR refresh count (0-indexed). Default 0.
      --max-qr-refreshes N      Max QR refreshes before giving up. Default 3.
                                When refresh count >= max, expired QR returns exit 1 (TIMEOUT)
                                instead of exit 3 (QR_EXPIRED_DETECTED).
    Deprecated but still parsed: --max-retries (mapped to --max-duration)."""
    p = {"chat_id": "", "target_user_id": "", "qr_msg_id": "",
         "qr_sent_as": "user", "max_duration": "300",
         "lark_cli": _lark_cli_path(), "pt_path": _pinchtab_path(),
         "check_qr": "false", "check_login": "false",
         "qr_refresh_count": "0", "max_qr_refreshes": "3"}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--chat-id" and i + 1 < len(args):
            p["chat_id"] = args[i + 1]; i += 2
        elif args[i] == "--target-user-id" and i + 1 < len(args):
            p["target_user_id"] = args[i + 1]; i += 2
        elif args[i] == "--qr-msg-id" and i + 1 < len(args):
            p["qr_msg_id"] = args[i + 1]; i += 2
        elif args[i] == "--qr-sent-as" and i + 1 < len(args):
            p["qr_sent_as"] = args[i + 1]; i += 2
        elif args[i] == "--max-duration" and i + 1 < len(args):
            p["max_duration"] = args[i + 1]; i += 2
        elif args[i] == "--max-retries" and i + 1 < len(args):
            retries = int(args[i + 1])
            p["max_duration"] = str(max(30, retries * 10)); i += 2
        elif args[i] == "--qr-refresh-count" and i + 1 < len(args):
            p["qr_refresh_count"] = args[i + 1]; i += 2
        elif args[i] == "--max-qr-refreshes" and i + 1 < len(args):
            p["max_qr_refreshes"] = args[i + 1]; i += 2
        elif args[i] == "--lark-cli" and i + 1 < len(args):
            p["lark_cli"] = args[i + 1]; i += 2
        elif args[i] == "--pt-path" and i + 1 < len(args):
            p["pt_path"] = args[i + 1]; i += 2
        elif args[i] == "--check-qr":
            p["check_qr"] = "true"; i += 1
        elif args[i] == "--check-login":
            p["check_login"] = "true"; i += 1
        else:
            i += 1

    lark = p["lark_cli"]
    pt = p["pt_path"]
    chat_id = p["chat_id"]
    target_user_id = p["target_user_id"]
    qr_msg_id = p["qr_msg_id"]
    qr_sent_as = p["qr_sent_as"]
    check_qr = p["check_qr"] == "true"
    check_login = p["check_login"] == "true"
    max_duration = int(p["max_duration"])
    qr_refresh_count = int(p["qr_refresh_count"])
    max_qr_refreshes = int(p["max_qr_refreshes"])

    start_time = time.time()
    poll_count = 0

    while True:
        elapsed = time.time() - start_time
        if elapsed > max_duration:
            break

        poll_count += 1
        now = time.strftime("%H:%M:%S")
        result = _run([
            lark, "im", "+chat-messages-list", "--chat-id", chat_id,
            "--sort", "desc", "--page-size", "10", "--as", "bot"
        ])

        try:
            d = json.loads(result.stdout)
            msgs = d.get("data", {}).get("messages", [])
            for msg in msgs:
                # When QR was sent as bot (bot-user P2P): skip reply_to check
                # because user messages are always directed at the bot.
                # When QR was sent as user (group chat): require reply_to match.
                if qr_sent_as != "bot":
                    reply_to = msg.get("reply_to", "") or msg.get("parent_id", "")
                    if reply_to != qr_msg_id:
                        continue

                sender = msg.get("sender", {}).get("id", "")
                if sender != target_user_id:
                    continue

                # When skipping reply_to (bot mode), ignore messages older than polling start
                if qr_sent_as == "bot":
                    msg_time = msg.get("create_time", "")
                    if msg_time:
                        try:
                            from datetime import datetime
                            msg_ts = datetime.strptime(msg_time, "%Y-%m-%d %H:%M:%S").timestamp()
                            if msg_ts < start_time - 10:  # 10s grace
                                continue
                        except (ValueError, OSError):
                            pass

                msg_type = msg.get("msg_type", "")
                content = msg.get("content", "")
                text = ""
                if msg_type == "text":
                    # lark-cli +chat-messages-list may return content as
                    # plain text directly OR as a JSON string like {"text":"..."}
                    try:
                        body = json.loads(content)
                        if isinstance(body, dict):
                            text = re.sub(r'<at[^>]*>.*?</at>', '', body.get("text", "")).strip()
                        else:
                            text = str(body).strip()
                    except (json.JSONDecodeError, AttributeError):
                        text = content.strip()
                elif msg_type == "post":
                    body = json.loads(content)
                    for paragraph in body.get("zh_cn", {}).get("content", []):
                        for block in paragraph:
                            if block.get("tag") == "text":
                                text += block.get("text", "")

                print(f"  [{now}] matched reply: {text}", flush=True)

                for kw in LOGIN_KEYWORDS:
                    if kw in text:
                        print("LOGIN_CONFIRMED", flush=True)
                        sys.exit(0)

                for kw in QR_EXPIRED_KEYWORDS:
                    if kw in text:
                        print("QR_EXPIRED", flush=True)
                        sys.exit(2)

        except json.JSONDecodeError:
            pass
        except Exception as e:
            print(f"  [{now}] {e}", flush=True)

        # Auto-detect login via browser (every ~30s, roughly every 3 polls)
        if check_login and pt and poll_count % 3 == 0:
            try:
                page_text = (_run_silent([pt, "text"]).stdout or "") + \
                            (_run_silent([pt, "snap"]).stdout or "")
                for kw in LOGIN_INDICATORS:
                    if kw in page_text:
                        print(f"  [{now}] browser login detected: {kw}", flush=True)
                        print("LOGIN_DETECTED_BROWSER", flush=True)
                        sys.exit(0)
            except Exception as e:
                print(f"  [{now}] browser check skipped: {e}", flush=True)

        # Dynamic interval
        if elapsed < 60:
            interval = 10
        elif elapsed < 180:
            interval = 5
        else:
            interval = 15
        time.sleep(interval)

    # --- Polling exhausted ---
    # Check browser for QR expiration if requested
    if check_qr and pt:
        print(f"  [{time.strftime('%H:%M:%S')}] Polling exhausted, checking browser QR status...", flush=True)
        qr_result = _run([pt, "eval", _QR_CHECK_JS])
        qr_status = qr_result.stdout.strip()
        print(f"  QR check result: {qr_status}", flush=True)
        if qr_status.startswith("expired"):
            if qr_refresh_count >= max_qr_refreshes:
                print(f"  QR expired but max refreshes ({max_qr_refreshes}) reached, waiting for user...", flush=True)
                print("TIMEOUT", flush=True)
                sys.exit(1)
            print("QR_EXPIRED_DETECTED", flush=True)
            sys.exit(3)

    print("TIMEOUT", flush=True)
    sys.exit(1)

# ============================================================
# Subcommand: consume-event
# ============================================================

def cmd_consume_event():
    """Run lark-cli event consume."""
    args = sys.argv[2:]
    lark = _lark_cli_path()
    result = _run([lark, "event", "consume"] + args)
    print(result.stdout, end="")

# ============================================================
# Subcommand: extract-message-id
# ============================================================

def cmd_extract_message_id():
    """Extract message_id from lark-cli JSON response on stdin."""
    data = _read_stdin_text()
    try:
        d = json.loads(data)
        msg_id = d.get("data", {}).get("message_id", "") or d.get("message_id", "")
        if not msg_id:
            m = re.search(r'"message_id"\s*:\s*"([^"]+)"', data)
            if m:
                msg_id = m.group(1)
        print(msg_id)
    except (json.JSONDecodeError, ValueError):
        m = re.search(r'"message_id"\s*:\s*"([^"]+)"', data)
        print(m.group(1) if m else "")

# ============================================================
# Subcommand: prepare-dir
# ============================================================

def cmd_prepare_dir():
    """Create the query output directory + subdirs and print its path."""
    target = _get_query_dir()
    os.makedirs(target, exist_ok=True)
    _ensure_subdirs()
    print(target)

# ============================================================
# Subcommand: init
# ============================================================

def cmd_init():
    """Initialize environment for violation query.
    1. Copy self to TEMP/violation_helper.py (bypass Chinese-in-path issues)
    2. Detect lark-cli and pinchtab paths, write to temp files
    3. Create output directory (违章查询/)
    4. Detect Python path
    Output: JSON with all paths.
    """
    # 1. Copy self to temp dir atomically (write to tmp file then rename)
    src = os.path.abspath(sys.argv[0])
    temp_dir = os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"
    dst = os.path.join(temp_dir, "violation_helper.py")
    tmp_dst = dst + f".{os.getpid()}.tmp"
    shutil.copy2(src, tmp_dst)
    os.replace(tmp_dst, dst)  # atomic on Windows

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

    result = {
        "helper": dst,
        "lark_cli": lark,
        "pinchtab": pt,
        "query_dir": query_dir,
        "python": py_path
    }
    print(json.dumps(result, ensure_ascii=False))

# ============================================================
# Subcommand: run-js
# ============================================================

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

# ============================================================
# Subcommand: list-vehicles
# ============================================================

def cmd_list_vehicles():
    """Extract vehicle list + pagination info from current page as JSON."""
    js = """
(function() {
  var vehicles = [];
  var table = document.querySelector('table');
  if (!table) { return JSON.stringify({error: 'no table found'}); }

  var rows = table.querySelectorAll('tr');
  for (var r = 0; r < rows.length; r++) {
    var tds = rows[r].querySelectorAll('td');
    if (tds.length >= 5) {
      var vals = [];
      for (var c = 0; c < tds.length; c++) {
        vals.push(tds[c].textContent.trim());
      }
      var first = vals[0] || '';
      if (first.length >= 7 && first.length <= 8) {
        vehicles.push({
          plate: vals[0] || '',
          type: vals[1] || '',
          status: vals[2] || '',
          inspection: vals[3] || '',
          scrap: vals[4] || '',
          unprocessed: parseInt(vals[5]) || 0
        });
      }
    }
  }

  var pagination = {current: 1, total: 1, has_next: false, has_prev: false};
  var pageLinks = document.querySelectorAll('a');
  var maxPage = 1;
  for (var p = 0; p < pageLinks.length; p++) {
    var num = parseInt(pageLinks[p].textContent.trim());
    if (num > maxPage) maxPage = num;
  }
  pagination.total = maxPage;

  var allPageElements = document.querySelectorAll('a, span, li');
  for (var q = 0; q < allPageElements.length; q++) {
    var t = allPageElements[q].textContent.trim();
    if (/^\\d+$/.test(t) && allPageElements[q].tagName !== 'A') {
      pagination.current = parseInt(t);
      break;
    }
  }

  for (var s = 0; s < pageLinks.length; s++) {
    if (pageLinks[s].textContent.trim() === '下一页' || pageLinks[s].textContent.trim().includes('next')) {
      pagination.has_next = true;
      break;
    }
  }

  return JSON.stringify({vehicles: vehicles, pagination: pagination});
})()
"""
    result = _run(["pinchtab", "eval", js])
    out = result.stdout.strip()
    m = re.search(r'\{.*\}', out, re.DOTALL)
    if m:
        print(m.group(0))
    else:
        print(out)

# ============================================================
# Subcommand: open-vehicle
# ============================================================

def cmd_open_vehicle():
    """Double-click the Nth vehicle row on the list page to open its detail.
    Args: --index N (1-based)

    Features:
    - Dismiss popup before attempting
    - Triple retry with exponential backoff (2s, 4s, 8s)
    - URL verification (must navigate to vehdetail.html)
    - Rate-limit detection on repeated failures
    """
    p = {"index": "1"}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--index" and i + 1 < len(args):
            p["index"] = args[i + 1]; i += 2
        else:
            i += 1

    idx = int(p["index"])

    # Dismiss any popup first
    _dismiss_popup_js()

    # Vehicle plate pattern: province prefix + letter
    plate_js = f"""
(function() {{
  var rows = document.querySelectorAll('table tr');
  var count = 0;
  for (var r = 0; r < rows.length; r++) {{
    var tds = rows[r].querySelectorAll('td');
    if (tds.length >= 1) {{
      var t = tds[0].textContent.trim();
      if (/^[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁][A-Z]/.test(t)) {{
        count++;
        if (count === {idx}) {{
          tds[0].dispatchEvent(new MouseEvent('dblclick', {{bubbles: true, cancelable: true, view: window}}));
          return JSON.stringify({{ok: true, plate: t, row: count}});
        }}
      }}
    }}
  }}
  return JSON.stringify({{ok: false, error: 'index {idx} not found', rows_found: count}});
}})()
"""
    # Triple retry with exponential backoff
    max_retries = 3
    for attempt in range(max_retries):
        if attempt > 0:
            backoff = 2 ** attempt  # 2, 4, 8 seconds
            time.sleep(backoff)
            _dismiss_popup_js()  # Re-dismiss any popup that appeared
            time.sleep(random.uniform(1, 2))

        result = _run(["pinchtab", "eval", plate_js])
        try:
            info = json.loads(result.stdout.strip())
        except (json.JSONDecodeError, ValueError):
            info = {"ok": False, "error": result.stdout.strip()}

        if info.get("ok"):
            time.sleep(random.uniform(3, 8))
            # Verify navigation to detail page
            check = _run(["pinchtab", "eval",
                "(function(){return window.location.href.indexOf('vehdetail')!==-1?'detail':'other'})()"])
            if 'detail' in check.stdout:
                print(json.dumps({"ok": True, "plate": info.get("plate", ""),
                                  "attempt": attempt + 1}, ensure_ascii=False))
                return
            else:
                # Double-click didn't navigate - retry
                if attempt < max_retries - 1:
                    continue

        if attempt < max_retries - 1:
            continue

    # All retries exhausted - check for rate limiting
    rate_check = _check_rate_limit()
    if rate_check["blocked"]:
        print(json.dumps({"ok": False, "error": "rate_limited",
                          "keywords": rate_check["keywords_found"]}, ensure_ascii=False))
    else:
        print(json.dumps({"ok": False, "error": "max_retries_exhausted",
                          "index": idx}, ensure_ascii=False))


def _dismiss_popup_js():
    """Internal: dismiss system popups via JS. Non-fatal on failure."""
    js = """
(function() {
  var texts = ['本人已知晓', '确定', '知道了', '关闭'];
  var all = document.querySelectorAll('button, a');
  for (var i = 0; i < all.length; i++) {
    var t = (all[i].textContent || '').trim();
    for (var j = 0; j < texts.length; j++) {
      if (t.indexOf(texts[j]) !== -1 && all[i].offsetHeight > 0) {
        all[i].click(); return 'ok';
      }
    }
  }
  return 'none';
})()
"""
    _run(["pinchtab", "eval", js])


# Rate-limit indicators from XHR responses (silent API rate-limiting)
RATE_LIMIT_XHR_PATTERNS = [
    "查询过于频繁", "操作频繁", "请求过于频繁", "访问被限制",
    "rate limit", "too many requests", "try again later",
]

def _setup_xhr_monitor():
    """Inject XHR monitoring JS into the page. Captures rate-limit responses.
    Must be called ONCE per page load. Subsequent XHR calls will be tracked
    in window.__xhrRateLimited."""
    js = """
(function() {
  if (window.__xhrMonitorInstalled) return 'already-installed';
  window.__xhrMonitorInstalled = true;
  window.__xhrRateLimited = false;
  window.__xhrRateLimitReason = '';

  var origOpen = XMLHttpRequest.prototype.open;
  var origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function(method, url) {
    this.__monitorUrl = url;
    return origOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function(body) {
    var self = this;
    var handler = function() {
      if (self.status === 200 && self.responseText) {
        try {
          var resp = JSON.parse(self.responseText);
          if (resp.code === 500 || resp.code === '500') {
            var msg = resp.message || resp.msg || '';
            var patterns = ['查询过于频繁','操作频繁','请求过于频繁','访问被限制','rate limit','too many'];
            for (var i = 0; i < patterns.length; i++) {
              if (msg.indexOf(patterns[i]) !== -1) {
                window.__xhrRateLimited = true;
                window.__xhrRateLimitReason = self.__monitorUrl + ': ' + msg;
                break;
              }
            }
          }
        } catch(e) {}
      }
    };
    this.addEventListener('load', handler);
    // NOTE: Do NOT flag all XHR errors as rate-limiting.
    // Network errors can happen for many reasons (analytics, CORS, etc.)
    // Only actual rate-limit responses (code 500 + specific message) are flagged above.
    return origSend.apply(this, arguments);
  };
  return 'installed';
})()
"""
    _run(["pinchtab", "eval", js])


def _check_xhr_rate_limit():
    """Check if any XHR request was rate-limited. Returns (blocked, reason)."""
    result = _run(["pinchtab", "eval",
        "(function(){return JSON.stringify({blocked:!!window.__xhrRateLimited,reason:window.__xhrRateLimitReason||''})})()"])
    try:
        data = json.loads(result.stdout.strip())
        return data.get("blocked", False), data.get("reason", "")
    except (json.JSONDecodeError, ValueError):
        return False, ""


def _check_rate_limit():
    """Internal: check for rate-limit/feng-kong indicators. Returns dict.
    Checks BOTH page text keywords AND XHR response patterns."""
    text = _run(["pinchtab", "text"]).stdout
    snap = _run(["pinchtab", "snap"]).stdout
    combined = text + " " + snap
    found = [kw for kw in RATE_LIMIT_KEYWORDS if kw in combined]
    has_table = "号牌号码" in snap or "未处理违法" in snap
    on_vehlist = "vehlist" in snap

    # Check XHR rate-limiting
    xhr_blocked, xhr_reason = _check_xhr_rate_limit()
    if xhr_blocked and xhr_reason:
        found.append(f"XHR: {xhr_reason}")

    blocked = len(found) > 0 or (on_vehlist and not has_table)
    return {"blocked": blocked, "keywords_found": found, "xhr_blocked": xhr_blocked}

# ============================================================
# Subcommand: collect-violations
# ============================================================

def cmd_collect_violations():
    """On a vehicle detail page, collect violation details with smart pagination
    and SQLite comparison.

    Features:
    - Dismiss popups before extraction
    - Compare with SQLite DB: skip if already recorded and status unchanged
    - Only click '查看详情' for unprocessed/unpaid violations
    - Support detail page pagination (>10 violations)
    - Rate-limit detection on failure
    - Random delays: 1-2s clicks, 3-8s between violations
    - Resume support: --resume-from N to continue from Nth detail page

    Args: --plate PLATE (for DB lookup), --query-date DATE, --auto-insert (write each violation to SQLite immediately)
    """
    p = {"plate": "", "query_date": time.strftime("%Y-%m-%d"), "resume_from": "0", "auto_insert": False}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--plate" and i + 1 < len(args):
            p["plate"] = args[i + 1]; i += 2
        elif args[i] == "--query-date" and i + 1 < len(args):
            p["query_date"] = args[i + 1]; i += 2
        elif args[i] == "--resume-from" and i + 1 < len(args):
            p["resume_from"] = args[i + 1]; i += 2
        elif args[i] == "--auto-insert":
            p["auto_insert"] = True; i += 1
        else:
            i += 1

    plate = p["plate"]
    query_date = p["query_date"]
    resume_from = int(p["resume_from"])
    auto_insert = p["auto_insert"]

    # Open DB connection if auto-insert mode
    db_conn = None
    if auto_insert:
        _init_db()
        db_conn = sqlite3.connect(_get_db_path())

    # Dismiss popup
    _dismiss_popup_js()
    time.sleep(0.5)

    # Setup XHR monitor to catch silent API rate-limiting
    _setup_xhr_monitor()

    # Detect Beijing platform - requires clicking a.view element instead of cell ref
    is_beijing = False
    try:
        url_check = _run(["pinchtab", "eval", "(function(){return window.location.hostname})()"])
        is_beijing = 'bj.122.gov.cn' in url_check.stdout
    except Exception:
        pass

    # Load existing violations from DB for comparison
    existing_violations = _load_violations_from_db(plate)

    all_results = []
    detail_page = max(resume_from, 0)

    while True:
        # Extract violations from current detail page
        violations, total_pages = _extract_detail_page_violations()

        if not violations:
            break

        if is_beijing:
            # Beijing: use JS index (a.view), skip snap/refs
            for idx, v in enumerate(violations):
                unique_key = f"{plate}_{v['time']}_{v['location'][:20]}_{v['behavior'][:30]}"
                existing = existing_violations.get(unique_key)
                if existing and not (
                    existing.get("handling_status_label", "") != v['status'] or
                    existing.get("payment_status_label", "") != v['payment']
                ):
                    all_results.append({
                        "time": v['time'], "location": v['location'],
                        "behavior": v['behavior'], "status": v['status'],
                        "payment": v['payment'],
                        "fine": existing.get("fine_amount", 0),
                        "points": existing.get("points", 0),
                        "authority": existing.get("authority", ""),
                        "unprocessed": v['unprocessed'],
                        "from_db": True, "status_changed": False, "_index": idx
                    })
                    continue

                needs_detail = v['unprocessed'] or (v['status'] == '未处理') or (v['payment'] == '未缴费')
                if not needs_detail:
                    all_results.append({
                        "time": v['time'], "location": v['location'],
                        "behavior": v['behavior'], "status": v['status'],
                        "payment": v['payment'], "fine": 0, "points": 0,
                        "authority": "", "unprocessed": False,
                        "skipped": True, "_detail_page": detail_page, "_index": idx
                    })
                    continue

                time.sleep(random.uniform(1, 2))
                _run(["pinchtab", "eval",
                    f"(function(){{var links=document.querySelectorAll('a.view');if(links.length>{idx}){{links[{idx}].click();return'ok'}}return'fail'}})()"])
                time.sleep(random.uniform(2, 3))

                # Check XHR rate-limiting
                xhr_blocked, xhr_reason = _check_xhr_rate_limit()
                if xhr_blocked:
                    all_results.append({"_rate_limited": True, "_reason": xhr_reason})
                    if db_conn:
                        db_conn.close()
                    print(json.dumps(all_results, ensure_ascii=False, indent=2))
                    return

                # Get dialog text via JS with retry (Beijing dialog may load via XHR)
                dialog_text = ""
                for retry in range(3):
                    time.sleep(1)
                    dialog_text = _run(["pinchtab", "eval",
                        """(function(){var d=document.querySelector('.aui_dialog');if(!d||window.getComputedStyle(d).display==='none')return'';var t=d.textContent.trim();return t.length>20?t:'';})()"""]).stdout
                    if dialog_text and len(dialog_text) > 50:
                        break
                detail = _parse_detail_popup(dialog_text)
                detail["_index"] = idx
                detail["time"] = v["time"]
                detail["location"] = v["location"]
                detail["behavior"] = v["behavior"]
                detail["status"] = v["status"]
                detail["payment"] = v["payment"]
                detail["unprocessed"] = v['unprocessed']
                detail["_detail_page"] = detail_page
                detail["from_db"] = False
                all_results.append(detail)

                # Auto-insert to DB immediately (before closing dialog)
                if auto_insert and db_conn:
                    try:
                        record = _collect_detail_to_db_record(detail, plate, query_date)
                        _upsert_violation(db_conn, record)
                        db_conn.commit()
                    except Exception as e:
                        print(f"    DB insert warning: {e}", file=sys.stderr)

                # Close dialog via JS
                _run(["pinchtab", "eval",
                    """(function(){var el=document.querySelector('.aui_dialog');if(!el)return'none';var btns=el.querySelectorAll('button,a,span');for(var i=0;i<btns.length;i++){if(btns[i].textContent.trim()==='取消'){btns[i].click();return'closed';}}document.body.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',code:'Escape',keyCode:27}));return'escape';})()"""])
                time.sleep(random.uniform(1, 2))

                if idx < len(violations) - 1:
                    time.sleep(random.uniform(3, 8))

        else:
            # Snap for refs
            snap = _run(["pinchtab", "snap"])
            snap_text = snap.stdout
            detail_refs = []
            for line in snap_text.split('\n'):
                if 'cell "查看详情"' in line:
                    m = re.match(r'e(\d+):cell "查看详情"', line.strip())
                    if m:
                        detail_refs.append(f"e{m.group(1)}")

            for idx, v in enumerate(violations):
                # Build unique key: plate + time + location + behavior[:30]
                unique_key = f"{plate}_{v['time']}_{v['location'][:20]}_{v['behavior'][:30]}"

                # Check if exists in DB
                existing = existing_violations.get(unique_key)

                if existing:
                    # Check status change
                    old_status = existing.get("handling_status_label", "")
                    old_payment = existing.get("payment_status_label", "")
                    new_status = v['status']
                    new_payment = v['payment']

                    status_changed = (old_status != new_status) or (old_payment != new_payment)

                    if not status_changed:
                        # Skip - already recorded, no change
                        all_results.append({
                            "time": v['time'], "location": v['location'],
                            "behavior": v['behavior'], "status": v['status'],
                            "payment": v['payment'],
                            "fine": existing.get("fine_amount", 0),
                            "points": existing.get("points", 0),
                            "authority": existing.get("authority", ""),
                            "unprocessed": v['unprocessed'],
                            "from_db": True, "status_changed": False, "_index": idx
                        })
                        continue
                    # Status changed - re-query
                    v['_status_changed'] = True

                # Determine if we need detail click
                needs_detail = v['unprocessed'] or v.get('_status_changed') or \
                              (v['status'] == '未处理') or (v['payment'] == '未缴费')

                if needs_detail:
                    if idx < len(detail_refs):
                        time.sleep(random.uniform(1, 2))
                        _run(["pinchtab", "click", detail_refs[idx]])
                        time.sleep(random.uniform(1, 2))

                        # Check for silent XHR rate-limiting
                        xhr_blocked, xhr_reason = _check_xhr_rate_limit()
                        if xhr_blocked:
                            all_results.append({"_rate_limited": True, "_reason": xhr_reason})
                            print(json.dumps(all_results, ensure_ascii=False, indent=2))
                            return

                        text_result = _run(["pinchtab", "text"])
                        detail = _parse_detail_popup(text_result.stdout)
                        detail["_index"] = idx
                        detail["time"] = v["time"]
                        detail["location"] = v["location"]
                        detail["behavior"] = v["behavior"]
                        detail["status"] = v["status"]
                        detail["payment"] = v["payment"]
                        detail["unprocessed"] = v['unprocessed']
                        detail["_detail_page"] = detail_page
                        detail["from_db"] = False
                        all_results.append(detail)

                        # Auto-insert to DB immediately (before closing popup)
                        if auto_insert and db_conn:
                            try:
                                record = _collect_detail_to_db_record(detail, plate, query_date)
                                _upsert_violation(db_conn, record)
                                db_conn.commit()
                            except Exception as e:
                                print(f"    DB insert warning: {e}", file=sys.stderr)

                        _close_popup()
                        time.sleep(random.uniform(1, 2))
                else:
                    all_results.append({
                        "time": v['time'], "location": v['location'],
                        "behavior": v['behavior'], "status": v['status'],
                        "payment": v['payment'], "fine": 0, "points": 0,
                        "authority": "", "unprocessed": False,
                        "skipped": True, "_detail_page": detail_page, "_index": idx
                    })

                if idx < len(violations) - 1:
                    time.sleep(random.uniform(3, 8))

        # Check if more detail pages exist - skip if no unprocessed on this page
        has_unprocessed = any(v.get('unprocessed') or v.get('status') == '未处理' for v in violations)
        detail_page += 1
        if detail_page >= total_pages or total_pages <= 1 or not has_unprocessed:
            break

        # Smart pagination: navigate to the next detail page
        time.sleep(random.uniform(3, 8))
        ok = _click_detail_page(str(detail_page + 1))  # 1-based page number
        if not ok:
            break

    # Rate-limit check
    rate = _check_rate_limit()
    if rate["blocked"]:
        all_results.append({"_rate_limited": True, "_keywords": rate["keywords_found"]})

    # Close DB connection if auto-insert was used
    if db_conn:
        db_conn.close()

    print(json.dumps(all_results, ensure_ascii=False, indent=2))


def _extract_detail_page_violations():
    """Extract violation rows from current detail page. Returns (violations, total_pages)."""
    js = """
(function() {
  var rows = document.querySelectorAll('table tr');
  var violations = [];
  for (var r = 0; r < rows.length; r++) {
    var tds = rows[r].querySelectorAll('td');
    if (tds.length >= 9) {
      var action = tds[8].textContent.trim();
      if (action === '查看详情') {
        violations.push({
          plate_type: tds[1].textContent.trim(),
          plate: tds[2].textContent.trim(),
          time: tds[3].textContent.trim(),
          location: tds[4].textContent.trim(),
          behavior: tds[5].textContent.trim(),
          status: tds[6].textContent.trim(),
          payment: tds[7].textContent.trim(),
          unprocessed: tds[6].textContent.trim() === '未处理'
        });
      }
    }
  }

  // Check for detail page pagination
  var links = document.querySelectorAll('a');
  var pages = [];
  for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim();
    if (/^\\d+$/.test(t)) { var n = parseInt(t); if (n <= 200) pages.push(n); }
  }
  pages.sort(function(a,b){return a-b;});
  var total = pages.length > 0 ? pages[pages.length - 1] : 1;

  return JSON.stringify({violations: violations, total_pages: total});
})()
"""
    result = _run(["pinchtab", "eval", js])
    try:
        out = result.stdout.strip()
        m = re.search(r'\{.*\}', out, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            return data.get("violations", []), data.get("total_pages", 1)
    except (json.JSONDecodeError, ValueError):
        pass
    return [], 1


def _click_detail_page(target):
    """Click pagination on the violation detail page. Reuses same smart-pagination
    pattern as vehicle list click-page.

    Args: --target next|prev|N (page number, 1-based)
    For numeric targets, uses smart pagination: if target not visible, navigates
    via max-page hops until target appears in pagination window.
    """
    if target in ("next", "prev"):
        _click_page_direct(target)
        return True

    target_page = int(target)
    visited_pages = set()
    visited_actions = set()
    stale_count = 0

    while True:
        time.sleep(random.uniform(1, 2))
        pi = _get_pagination_state()
        if pi is None:
            return False

        min_p = pi["min_page"]
        max_p = pi["max_page"]

        if min_p <= target_page <= max_p:
            result = _click_page_number(target_page)
            if "clicked" in result:
                return True
            if target_page not in visited_pages:
                visited_pages.add(target_page)
                stale_count = 0
                continue

        progressed = False

        if target_page > max_p:
            if max_p not in visited_pages:
                visited_pages.add(max_p)
                _click_page_number(max_p)
                stale_count = 0; progressed = True; continue
            if "next" not in visited_actions:
                visited_actions.add("next")
                _click_page_direct("next")
                stale_count = 0; progressed = True; continue

        elif target_page < min_p:
            if min_p not in visited_pages:
                visited_pages.add(min_p)
                _click_page_number(min_p)
                stale_count = 0; progressed = True; continue
            if "prev" not in visited_actions:
                visited_actions.add("prev")
                _click_page_direct("prev")
                stale_count = 0; progressed = True; continue

        if not progressed:
            stale_count += 1
            if stale_count >= 3:
                return False
            time.sleep(random.uniform(1, 2))

    return False


def _get_detail_page_state():
    """Extract current page and total pages from the violation detail view."""
    js = """
(function() {
  var links = document.querySelectorAll('a');
  var pages = [];
  for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim();
    if (/^\\d+$/.test(t)) { var n = parseInt(t); if (n <= 200) pages.push(n); }
  }
  pages.sort(function(a,b){return a-b;});
  if (pages.length === 0) return JSON.stringify({current: 1, min_page: 1, max_page: 1, total: 1});
  // Current page: find non-link or highlighted page number near pagination
  var current = 1;
  var all = document.querySelectorAll('a,span,li,strong,b');
  for (var j = 0; j < all.length; j++) {
    var t = all[j].textContent.trim();
    if (/^\\d+$/.test(t) && all[j].tagName !== 'A') { current = parseInt(t); break; }
  }
  return JSON.stringify({
    current: current,
    min_page: pages[0],
    max_page: pages[pages.length - 1],
    total: pages[pages.length - 1]
  });
})()
"""
    result = _run(["pinchtab", "eval", js])
    try:
        m = re.search(r'\{.*\}', result.stdout, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        pass
    return {"current": 1, "min_page": 1, "max_page": 1, "total": 1}


def _load_violations_from_db(plate):
    """Load existing violations for a plate from SQLite DB. Returns dict keyed by unique_id."""
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        return {}

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            """SELECT violation_time, violation_location, violation_behavior,
                      fine_amount, points, handling_status_label, payment_status_label,
                      authority, unique_id
               FROM violations WHERE plate_number = ?""", (plate,))
        rows = cur.fetchall()
        conn.close()

        result = {}
        for row in rows:
            time_str = row[0] or ""
            location = row[1] or ""
            behavior = row[2] or ""
            key = f"{plate}_{time_str}_{location[:20]}_{behavior[:30]}"
            result[key] = {
                "violation_time": row[0], "violation_location": row[1],
                "violation_behavior": row[2], "fine_amount": row[3],
                "points": row[4], "handling_status_label": row[5],
                "payment_status_label": row[6], "authority": row[7],
                "unique_id": row[8]
            }
        return result
    except Exception:
        return {}


def _close_popup():
    """Try to close a modal/popup dialog. Multi-strategy approach (Issue #4 fix):

    Strategy order (tries next if current fails):
      1. JavaScript dispatchEvent click on close/×/取消 buttons (bypasses pinchtab occlusion check)
      2. PinchTab click on close button refs from snap
      3. JavaScript Escape key event
      4. Direct DOM removal of modal/overlay elements (last resort)

    Returns True if at least one strategy was attempted (not whether it succeeded —
    caller should verify by checking for absence of detail links).
    """
    # Strategy 1: JavaScript click on close buttons (bypasses occlusion check entirely)
    js_find_and_click_close = """
(function() {
  // Find close buttons by text content
  var allElements = document.querySelectorAll('button, a, span, div, i');
  var closeTexts = ['关闭', '×', '取消', 'close', 'x'];
  for (var i = 0; i < allElements.length; i++) {
    var el = allElements[i];
    var text = (el.textContent || '').trim();
    for (var j = 0; j < closeTexts.length; j++) {
      if (text === closeTexts[j] || text.indexOf(closeTexts[j]) !== -1) {
        // Use dispatchEvent to bypass occlusion/visibility checks
        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
        return 'js-clicked:' + text;
      }
    }
  }
  // Try clicking elements with close-related CSS classes
  var closeSelectors = ['.close', '.el-icon-close', '.dialog-close', '.modal-close',
                        '[class*="close"]', '[class*="Close"]', '.cancel-btn',
                        '.ant-modal-close', '.el-dialog__close'];
  for (var k = 0; k < closeSelectors.length; k++) {
    try {
      var els = document.querySelectorAll(closeSelectors[k]);
      for (var m = 0; m < els.length; m++) {
        els[m].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
        return 'js-clicked-selector:' + closeSelectors[k];
      }
    } catch(e) {}
  }
  return 'no-close-element-found';
})()
"""
    js_result = _run(["pinchtab", "eval", js_find_and_click_close])
    time.sleep(0.5)
    if 'js-clicked' in js_result.stdout:
        return True

    # Strategy 2: PinchTab click on found refs (may fail with occlusion — that's expected)
    snap = _run(["pinchtab", "snap"])
    snap_text = snap.stdout

    close_refs = []
    for line in snap_text.split('\n'):
        if any(kw in line for kw in ['button "关闭"', 'button "×"', 'button "close"',
                                        'button "取消"', 'cell "关闭"', 'cell "×"',
                                        'link "关闭"', 'link "×"', 'button "Close"',
                                        'button "X"']):
            m = re.match(r'e(\d+):', line.strip())
            if m:
                close_refs.append(f"e{m.group(1)}")

    if close_refs:
        # For each ref, try both pinchtab click and JS dispatchEvent
        for ref in close_refs:
            # Try pinchtab click first
            result = _run(["pinchtab", "click", ref])
            time.sleep(0.3)
            # If occluded, fall back to JS dispatchEvent on the same element
            if 'occluded' in result.stdout.lower() or 'error' in result.stderr.lower():
                # Use JS to click the same element by ref pattern
                ref_num = ref[1:]  # e123 -> 123
                js_click_by_idx = f"""
(function() {{
  var all = document.querySelectorAll('button, a, span, div, i');
  var closeTexts = ['关闭', '×', '取消', 'close'];
  for (var i = 0; i < all.length; i++) {{
    var t = (all[i].textContent || '').trim();
    for (var j = 0; j < closeTexts.length; j++) {{
      if (t === closeTexts[j] || t.indexOf(closeTexts[j]) !== -1) {{
        all[i].dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true, view: window}}));
        return 'js-fallback-clicked';
      }}
    }}
  }}
  return 'no-match';
}})()
"""
                _run(["pinchtab", "eval", js_click_by_idx])
                time.sleep(0.3)
            return True

    # Strategy 3: Escape key via JavaScript (bypasses pinchtab keyboard which may not reach)
    _run(["pinchtab", "eval",
          "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', keyCode: 27, bubbles: true}))"])
    time.sleep(0.5)
    # Also try programmatic Esc for any focused element
    _run(["pinchtab", "eval",
          "(function(){var e=new KeyboardEvent('keydown',{key:'Escape',keyCode:27,bubbles:true,cancelable:true});document.activeElement&&document.activeElement.dispatchEvent(e);document.body.dispatchEvent(e)})()"])
    time.sleep(0.3)

    # Strategy 4: Direct DOM removal of modal/overlay (last resort)
    js_remove_modal = """
(function() {
  // Try to find and hide/remove modal overlay elements
  var selectors = [
    '.el-dialog__wrapper', '.el-overlay', '.ant-modal-wrap', '.ant-modal-mask',
    '.modal', '.dialog', '.overlay', '.mask', '[role="dialog"]',
    '.v-modal', '.el-message-box__wrapper', '.el-drawer__wrapper',
    'div[class*="dialog"]', 'div[class*="modal"]', 'div[class*="overlay"]',
    'div[class*="mask"]', 'div[class*="popup"]'
  ];
  var removed = 0;
  for (var i = 0; i < selectors.length; i++) {
    try {
      var els = document.querySelectorAll(selectors[i]);
      for (var j = 0; j < els.length; j++) {
        // Only remove if visible (has non-zero dimensions)
        var rect = els[j].getBoundingClientRect();
        if (rect.width > 0 || rect.height > 0) {
          els[j].style.display = 'none';
          removed++;
        }
      }
    } catch(e) {}
  }
  // Also remove fixed position overlays with high z-index
  var allDivs = document.querySelectorAll('div');
  for (var k = 0; k < allDivs.length; k++) {
    var style = window.getComputedStyle(allDivs[k]);
    if (style.position === 'fixed' && parseInt(style.zIndex) > 100 &&
        (allDivs[k].offsetWidth > 100 || allDivs[k].offsetHeight > 100)) {
      allDivs[k].style.display = 'none';
      removed++;
    }
  }
  return 'removed:' + removed;
})()
"""
    _run(["pinchtab", "eval", js_remove_modal])
    time.sleep(0.5)
    return True

def _parse_detail_popup(text):
    """Parse violation detail popup text into structured dict.
    Handles multiple text formats from the 12123 popup.
    """
    data = {
        "plate": "", "type": "", "time": "", "location": "",
        "behavior": "", "authority": "", "points": 0, "fine": 0,
        "_raw_text": text[:500]
    }

    # Normalize text: collapse multiple newlines and spaces
    normalized = re.sub(r'\n\s*\n', '\n', text)

    m = re.search(r'号牌号码[：:]\s*\n?\s*(\S+)', normalized)
    if m: data["plate"] = m.group(1).strip()
    m = re.search(r'号牌种类[：:]\s*\n?\s*(\S+)', normalized)
    if m: data["type"] = m.group(1).strip()
    m = re.search(r'违法时间[：:]\s*\n?\s*([\d\-:\s]+)', normalized)
    if m: data["time"] = m.group(1).strip()
    m = re.search(r'违法地点[：:]\s*\n?\s*(.+?)(?:\n\s*(?:采集机关|记\s*分|罚))', normalized, re.DOTALL)
    if not m:
        m = re.search(r'违法地点[：:]\s*\n?\s*(.+?)$', normalized, re.DOTALL)
    if m: data["location"] = m.group(1).strip()
    m = re.search(r'违法行为[：:]\s*\n?\s*(.+?)(?:\n\s*(?:采集机关|记\s*分|罚))', normalized, re.DOTALL)
    if not m:
        m = re.search(r'违法行为[：:]\s*\n?\s*(.+?)$', normalized, re.DOTALL)
    if m: data["behavior"] = m.group(1).strip()
    m = re.search(r'采集机关[：:]\s*\n?\s*(.+?)(?:\n\s*(?:记\s*分|罚))', normalized, re.DOTALL)
    if not m:
        m = re.search(r'采集机关[：:]\s*\n?\s*(.+?)$', normalized, re.DOTALL)
    if m: data["authority"] = m.group(1).strip()

    # Points: match "记分 值: N" or "记分: N" or "记分值: N" with possible newlines
    m = re.search(r'记\s*分\s*值?\s*[：:]\s*\n?\s*(\d+)', normalized)
    if not m:
        m = re.search(r'记分[：:]\s*\n?\s*(\d+)', normalized)
    if m: data["points"] = int(m.group(1))

    # Fine amount: handle multiple formats
    # Format 1: "罚款金额：200" or "罚款金额: 200"
    # Format 2: "罚款金额（元）：200" or "罚款金额(元):200"
    # Format 3: "罚款金额 200" (no colon)
    # Format 4: "罚款总金额：200.00元"
    # Format 5: "罚款金额：200元"
    m = re.search(r'罚款(?:总)?金额\s*(?:[(（]元[)）])?\s*[：:]\s*\n?\s*(\d+(?:\.\d+)?)', normalized)
    if not m:
        m = re.search(r'罚款(?:总)?金额\s*\n?\s*(\d+(?:\.\d+)?)', normalized)
    if not m:
        m = re.search(r'罚\s*款\s*[：:]\s*\n?\s*(\d+(?:\.\d+)?)', normalized)
    if not m:
        # Try to find "罚款" anywhere followed by a number
        m = re.search(r'罚\s*款.*?(\d+(?:\.\d+)?)\s*元?', normalized)
    if m:
        data["fine"] = int(float(m.group(1)))

    return data

# ============================================================
# Subcommand: go-back
# ============================================================

def cmd_go_back():
    """Navigate back from detail page to vehicle list page.
    Uses history.back() as primary method to preserve the original page position.
    Only falls back to the back-link click if history.back() doesn't work."""
    # Primary: use history.back() to return to list at original page position
    _run(["pinchtab", "eval", "history.back()"])
    time.sleep(random.uniform(1, 2))

    # Verify we're back on the list page
    check = _run(["pinchtab", "eval",
        "(function(){var u=window.location.href;return u.indexOf('vehlist')!==-1||u.indexOf('qrl')!==-1?'list':'detail'})()"])
    if 'list' in check.stdout:
        print("ok")
        return

    # Fallback 1: Find and click the back/return link
    js = """
(function() {
  var links = document.querySelectorAll('a');
  for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim();
    if (t.charCodeAt(0) === 36820) { // 返
      links[i].click();
      return 'clicked-back-link';
    }
  }
  // Try common return/back link patterns
  for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim();
    if (t.indexOf('返回') !== -1 || t.indexOf('退') !== -1) {
      links[i].click();
      return 'clicked-return-link';
    }
  }
  return 'no-back-link';
})()
"""
    _run(["pinchtab", "eval", js])
    time.sleep(random.uniform(1, 2))

    # Verify again
    check2 = _run(["pinchtab", "eval",
        "(function(){var u=window.location.href;return u.indexOf('vehlist')!==-1||u.indexOf('qrl')!==-1?'list':'detail'})()"])
    if 'list' in check2.stdout:
        print("ok")
        return

    # Fallback 2: history.go(-1) as last resort
    _run(["pinchtab", "eval", "history.go(-1)"])
    time.sleep(random.uniform(1, 2))
    print("ok")

# ============================================================
# Subcommand: click-page
# ============================================================

def cmd_click_page():
    """Click pagination on the vehicle list page.
    Args: --target next|prev|N (page number)
    For page number targets, uses smart pagination:
    - If target > max displayed page, click max page to shift window right
    - If target < min displayed page, click min page to shift window left
    - Repeat until target found, page range stabilizes, or all visible pages visited.

    No hard retry limit: uses visited-set of page numbers to detect loops.
    For 210-page datasets with ~5-link windows, needs ~40 hops (each hop
    shifts the window by the visible page count). The visited-set ensures
    we don't cycle; when all visible pages are visited, we try next/prev,
    and only exit when no new navigation moves remain.
    """
    p = {"target": "next"}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--target" and i + 1 < len(args):
            p["target"] = args[i + 1]; i += 2
        else:
            i += 1

    target = p["target"]

    if target in ("next", "prev"):
        _click_page_direct(target)
        return

    # Smart pagination for numeric targets
    target_page = int(target)
    visited_pages = set()      # page numbers already clicked
    visited_actions = set()    # "next"/"prev" already tried from current position
    stale_count = 0            # consecutive iterations with no progress

    while True:
        # Slow down between hops to avoid rate limiting
        time.sleep(random.uniform(1, 2))

        page_info = _get_pagination_state()
        if page_info is None:
            print("error: cannot read pagination state")
            return

        current = page_info["current"]
        min_page = page_info["min_page"]
        max_page = page_info["max_page"]

        # Check if target is directly clickable
        if min_page <= target_page <= max_page:
            result = _click_page_number(target_page)
            if "clicked" in result:
                print(f"navigated to page {target_page}")
                return
            # Target in range but not clickable - try to get it visible
            # Click the page nearest to target in the visible range
            if target_page not in visited_pages:
                visited_pages.add(target_page)
                stale_count = 0
                continue

        # Target is beyond range - use smart navigation
        progressed = False

        if target_page > max_page:
            if max_page not in visited_pages:
                visited_pages.add(max_page)
                _click_page_number(max_page)
                stale_count = 0
                progressed = True
                continue
            # Max page already visited, try next button
            if "next" not in visited_actions:
                visited_actions.add("next")
                _click_page_direct("next")
                stale_count = 0
                progressed = True
                continue

        elif target_page < min_page:
            if min_page not in visited_pages:
                visited_pages.add(min_page)
                _click_page_number(min_page)
                stale_count = 0
                progressed = True
                continue
            if "prev" not in visited_actions:
                visited_actions.add("prev")
                _click_page_direct("prev")
                stale_count = 0
                progressed = True
                continue

        # If we're in the right range but target isn't clickable,
        # try stepping via next/prev to make it appear
        if min_page <= target_page <= max_page and "next" not in visited_actions:
            visited_actions.add("next")
            _click_page_direct("next")
            stale_count = 0
            progressed = True
            continue
        if min_page <= target_page <= max_page and "prev" not in visited_actions:
            visited_actions.add("prev")
            _click_page_direct("prev")
            stale_count = 0
            progressed = True
            continue

        if not progressed:
            stale_count += 1
            if stale_count >= 3:
                print(f"error: stuck at page {current}, cannot reach target {target_page}")
                return
            time.sleep(random.uniform(1, 2))


def _click_page_direct(target):
    """Click next/prev page button. Uses JavaScript text-matching (ref-independent).

    Issue #5 fix: Prefer clicking page numbers over next/prev.
    When next/prev must be used, match by text content first, then by
    CSS selectors, then by character codes as last resort.
    """
    if target == "next":
        js = r"""
(function() {
  // Strategy 1: Match by text content (most robust)
  var links = document.querySelectorAll('a, button, span[role="button"]');
  for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim();
    if (t === '\u4e0b\u4e00\u9875' || t === '下一页' || t.indexOf('下一页') !== -1 ||
        t === '\u4e0b\u9875' || t === '下页' || t === 'next' || t === 'Next') {
      links[i].click();
      return 'clicked-next(text)';
    }
  }
  // Strategy 2: Match by CSS class or aria-label
  var selectors = ['.next', '.pagination-next', '[aria-label="next"]',
                   '[aria-label="下一页"]', '.el-pagination button:last-child',
                   '.ant-pagination-next'];
  for (var j = 0; j < selectors.length; j++) {
    try {
      var el = document.querySelector(selectors[j]);
      if (el) { el.click(); return 'clicked-next(selector)'; }
    } catch(e) {}
  }
  // Strategy 3: Character code matching (legacy fallback)
  for (var k = 0; k < links.length; k++) {
    var t = links[k].textContent.trim();
    if (t.length >= 2 && t.charCodeAt(0) === 19979) {
      links[k].click();
      return 'clicked-next(charcode)';
    }
  }
  return 'next-link-not-found';
})()
"""
    elif target == "prev":
        js = r"""
(function() {
  var links = document.querySelectorAll('a, button, span[role="button"]');
  for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim();
    if (t === '\u4e0a\u4e00\u9875' || t === '上一页' || t.indexOf('上一页') !== -1 ||
        t === '\u4e0a\u9875' || t === '上页' || t === 'prev' || t === 'Prev') {
      links[i].click();
      return 'clicked-prev(text)';
    }
  }
  var selectors = ['.prev', '.pagination-prev', '[aria-label="prev"]',
                   '[aria-label="上一页"]', '.el-pagination button:first-child',
                   '.ant-pagination-prev'];
  for (var j = 0; j < selectors.length; j++) {
    try {
      var el = document.querySelector(selectors[j]);
      if (el) { el.click(); return 'clicked-prev(selector)'; }
    } catch(e) {}
  }
  for (var k = 0; k < links.length; k++) {
    var t = links[k].textContent.trim();
    if (t.length >= 2 && t.charCodeAt(0) === 19978) {
      links[k].click();
      return 'clicked-prev(charcode)';
    }
  }
  return 'prev-link-not-found';
})()
"""
    result = _run(["pinchtab", "eval", js])
    time.sleep(random.uniform(1, 2))
    print(result.stdout.strip())


def _click_page_number(page_num):
    """Click a specific page number link."""
    js = f"""
(function() {{
  var links = document.querySelectorAll('a');
  for (var i = 0; i < links.length; i++) {{
    if (links[i].textContent.trim() === '{page_num}') {{
      links[i].click();
      return 'clicked-page-{page_num}';
    }}
  }}
  return 'page-{page_num}-not-found';
}})()
"""
    result = _run(["pinchtab", "eval", js])
    time.sleep(random.uniform(1, 2))
    return result.stdout.strip()


def _get_pagination_state():
    """Extract current pagination state from the page: current, min, max pages."""
    js = """
(function() {
  var links = document.querySelectorAll('a');
  var pages = [];
  for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim();
    if (/^\\d+$/.test(t)) {
      pages.push(parseInt(t));
    }
  }
  if (pages.length === 0) return JSON.stringify({error: 'no page numbers found'});
  pages.sort(function(a,b) { return a - b; });

  // Find current page (non-link page number, usually highlighted)
  var current = pages[0];
  var allElements = document.querySelectorAll('a, span, li, strong, b, em');
  for (var j = 0; j < allElements.length; j++) {
    var t = allElements[j].textContent.trim();
    if (/^\\d+$/.test(t) && allElements[j].tagName !== 'A') {
      current = parseInt(t);
      break;
    }
  }

  return JSON.stringify({
    current: current,
    min_page: pages[0],
    max_page: pages[pages.length - 1],
    visible_pages: pages
  });
})()
"""
    result = _run(["pinchtab", "eval", js])
    try:
        out = result.stdout.strip()
        m = re.search(r'\{.*\}', out, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        pass
    return None

# ============================================================
# Subcommand: pinchtab-path
# ============================================================

def cmd_pinchtab_path():
    """Output the full path to pinchtab executable."""
    print(_pinchtab_path())

# ============================================================
# Subcommand: lark-cli-path
# ============================================================

def cmd_lark_cli_path():
    """Output the full path to lark-cli executable."""
    print(_lark_cli_path())

# ============================================================
# Subcommand: get-login-url
# ============================================================

def cmd_get_login_url():
    """Output the national unit login URL."""
    print(UNIT_LOGIN_URL)

# ============================================================
# Subcommand: save-detail-progress
# ============================================================

def cmd_save_detail_progress():
    """Save/resume detail progress: mark a plate as processed at a given page+index.
    Args (stdin JSON or CLI):
      --page N              Vehicle list page number
      --vehicle-index N     Vehicle index on the page (1-based)
      --plate PLATE         Plate number of last processed vehicle
      --company NAME        Company name (required, used for file isolation)
      --query-date DATE     Query date YYYY-MM-DD (required, used for file isolation)
      --total-violations N  Total violation count so far (optional)
      --detail-page N       Detail page within the vehicle (0-based)
      --violation-index N   Violation index within the detail page (0-based)
      --violation-time T    Timestamp of last processed violation (for cross-ref)
    Writes to details_progress_<company>_<date>.json with resume point.
    """
    p = {"page": "1", "vehicle_index": "0", "plate": "", "company": "",
         "query_date": "", "total_violations": "0",
         "detail_page": "-1", "violation_index": "-1", "violation_time": ""}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--page" and i + 1 < len(args):
            p["page"] = args[i + 1]; i += 2
        elif args[i] == "--vehicle-index" and i + 1 < len(args):
            p["vehicle_index"] = args[i + 1]; i += 2
        elif args[i] == "--plate" and i + 1 < len(args):
            p["plate"] = args[i + 1]; i += 2
        elif args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        elif args[i] == "--query-date" and i + 1 < len(args):
            p["query_date"] = args[i + 1]; i += 2
        elif args[i] == "--total-violations" and i + 1 < len(args):
            p["total_violations"] = args[i + 1]; i += 2
        elif args[i] == "--detail-page" and i + 1 < len(args):
            p["detail_page"] = args[i + 1]; i += 2
        elif args[i] == "--violation-index" and i + 1 < len(args):
            p["violation_index"] = args[i + 1]; i += 2
        elif args[i] == "--violation-time" and i + 1 < len(args):
            p["violation_time"] = args[i + 1]; i += 2
        else:
            i += 1

    if not p["company"] or not p["query_date"]:
        print(json.dumps({"ok": False, "error": "--company and --query-date are required"}, ensure_ascii=False))
        sys.exit(1)

    data_dir = _get_data_dir()
    safe_company = re.sub(r'[<>:"/\\|?*]', '_', p["company"])
    prog_file = os.path.join(data_dir, f"details_progress_{safe_company}_{p['query_date']}.json")

    # Load existing progress
    progress = {}
    if os.path.exists(prog_file):
        try:
            with open(prog_file, "r", encoding="utf-8") as f:
                progress = json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass

    # Update progress
    progress["last_page"] = int(p["page"])
    progress["last_vehicle_index"] = int(p["vehicle_index"])
    progress["last_plate"] = p["plate"]
    progress["total_violations"] = int(progress.get("total_violations", 0)) + int(p.get("total_violations", 0))

    # Violation-level resume: only save if explicitly provided (>=0)
    detail_page = int(p["detail_page"])
    violation_idx = int(p["violation_index"])
    if detail_page >= 0:
        progress["last_detail_page"] = detail_page
    if violation_idx >= 0:
        progress["last_violation_index"] = violation_idx
    if p["violation_time"]:
        progress["last_violation_time"] = p["violation_time"]

    # Track processed plates
    plates = progress.get("processed_plates", [])
    if p["plate"] and p["plate"] not in plates:
        plates.append(p["plate"])
    progress["processed_plates"] = plates

    with open(prog_file, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)

    print(json.dumps({"ok": True, "resume_page": progress["last_page"],
                       "resume_index": progress["last_vehicle_index"],
                       "resume_plate": progress["last_plate"],
                       "resume_detail_page": progress.get("last_detail_page", -1),
                       "resume_violation_index": progress.get("last_violation_index", -1),
                       "resume_violation_time": progress.get("last_violation_time", "")}, ensure_ascii=False))


# ============================================================
# Subcommand: load-detail-progress
# ============================================================

def cmd_load_detail_progress():
    """Load the detail progress resume point.
    Args: --company NAME --query-date DATE (both required for file isolation)
    Returns JSON: {resume_page, resume_vehicle_index, resume_plate, processed_plates, total_violations}
    """
    p = {"company": "", "query_date": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        elif args[i] == "--query-date" and i + 1 < len(args):
            p["query_date"] = args[i + 1]; i += 2
        else:
            i += 1

    if not p["company"] or not p["query_date"]:
        print(json.dumps({"resume_page": 1, "resume_vehicle_index": 0,
                          "resume_plate": "", "processed_plates": [],
                          "total_violations": 0, "fresh": True,
                          "error": "--company and --query-date required"}, ensure_ascii=False))
        return

    data_dir = _get_data_dir()
    safe_company = re.sub(r'[<>:"/\\|?*]', '_', p["company"])
    prog_file = os.path.join(data_dir, f"details_progress_{safe_company}_{p['query_date']}.json")

    if not os.path.exists(prog_file):
        print(json.dumps({"resume_page": 1, "resume_vehicle_index": 0,
                          "resume_plate": "", "processed_plates": [],
                          "total_violations": 0, "fresh": True}, ensure_ascii=False))
        return

    try:
        with open(prog_file, "r", encoding="utf-8") as f:
            progress = json.load(f)
    except (json.JSONDecodeError, ValueError):
        progress = {}

    # If progress was cleared (empty dict or just empty plates), treat as fresh
    last_page = progress.get("last_page", 0)
    last_idx = progress.get("last_vehicle_index", 0)

    if last_page == 0:
        # No resume point set, but plates might exist from pre-resume-point era
        # Treat as fresh start
        print(json.dumps({"resume_page": 1, "resume_vehicle_index": 0,
                          "resume_plate": "", "processed_plates": progress.get("processed_plates", []),
                          "total_violations": progress.get("total_violations", 0),
                          "fresh": True, "note": "no resume point, plates list preserved"}, ensure_ascii=False))
        return

    result = {
        "resume_page": last_page,
        "resume_vehicle_index": last_idx,
        "resume_plate": progress.get("last_plate", ""),
        "resume_detail_page": progress.get("last_detail_page", -1),
        "resume_violation_index": progress.get("last_violation_index", -1),
        "resume_violation_time": progress.get("last_violation_time", ""),
        "processed_plates": progress.get("processed_plates", []),
        "total_violations": progress.get("total_violations", 0),
        "fresh": False
    }
    print(json.dumps(result, ensure_ascii=False))


# ============================================================
# Subcommand: reset-detail-progress
# ============================================================

def cmd_reset_detail_progress():
    """Safely reset detail progress. Keeps full vehicle list intact.
    Only clears the detail-level progress (plates processed, resume point).
    Does NOT touch all_vehicles_progress.json.

    Args: --company NAME --query-date DATE (both required)
    """
    p = {"company": "", "query_date": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        elif args[i] == "--query-date" and i + 1 < len(args):
            p["query_date"] = args[i + 1]; i += 2
        else:
            i += 1

    if not p["company"] or not p["query_date"]:
        print(json.dumps({"ok": False, "error": "--company and --query-date required"}, ensure_ascii=False))
        sys.exit(1)

    data_dir = _get_data_dir()
    safe_company = re.sub(r'[<>:"/\\|?*]', '_', p["company"])
    prog_file = os.path.join(data_dir, f"details_progress_{safe_company}_{p['query_date']}.json")
    details_file = os.path.join(data_dir, f"violation_details_{safe_company}_{p['query_date']}.json")

    with open(prog_file, "w", encoding="utf-8") as f:
        json.dump({"processed_plates": [], "total_violations": 0,
                   "last_page": 0, "last_vehicle_index": 0, "last_plate": ""}, f, ensure_ascii=False, indent=2)

    with open(details_file, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False)

    print(json.dumps({"ok": True, "message": "detail progress reset, vehicle list untouched"}, ensure_ascii=False))


# ============================================================
# Subcommand: get-page-vehicles
# ============================================================

def cmd_get_page_vehicles():
    """Get vehicles on the current page AND the current page number.
    Returns JSON: {vehicles: [...], page: N, total_pages: N}
    This is the primary command for the page-by-page batch query flow.

    Auto-dismisses popups before extraction.
    """
    # Dismiss any popup first
    _dismiss_popup_js()
    time.sleep(0.5)

    # Extract vehicles from current page
    vehicles_js = """
(function() {
  var vehicles = [];
  var table = document.querySelector('table');
  if (!table) { return JSON.stringify({error: 'no table found'}); }

  var rows = table.querySelectorAll('tr');
  for (var r = 0; r < rows.length; r++) {
    var tds = rows[r].querySelectorAll('td');
    if (tds.length >= 6) {
      var vals = [];
      for (var c = 0; c < tds.length; c++) {
        vals.push(tds[c].textContent.trim());
      }
      var first = vals[0] || '';
      // Chinese plate: province char + letter, 7-8 chars
      if (/^[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁][A-Z]/.test(first)) {
        vehicles.push({
          plate: vals[0] || '',
          type: vals[1] || '',
          status: vals[2] || '',
          inspection: vals[3] || '',
          scrap: vals[4] || '',
          unprocessed: parseInt(vals[5]) || 0
        });
      }
    }
  }
  return JSON.stringify(vehicles);
})()
"""
    v_result = _run(["pinchtab", "eval", vehicles_js])
    vehicles = []
    try:
        out = v_result.stdout.strip()
        m = re.search(r'\[.*\]', out, re.DOTALL)
        if m:
            vehicles = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        pass

    # Get pagination
    page_state = _get_pagination_state()
    current_page = page_state.get("current", 1) if page_state else 1
    max_page = page_state.get("max_page", 1) if page_state else 1

    result = {
        "vehicles": vehicles,
        "page": current_page,
        "total_pages": max_page
    }
    print(json.dumps(result, ensure_ascii=False))


# ============================================================
# Subcommand: find-plate-page
# ============================================================

def cmd_find_plate_page():
    """Find which page a plate is on, starting from current page.
    If plate not found on current page, try next 3 pages.
    If still not found, reset to page 1 and scan page by page.

    Args: --plate PLATE --max-forward N (default 3)

    Returns JSON: {found: bool, page: N, method: "current"|"forward"|"scan"}
    """
    p = {"plate": "", "max_forward": "3"}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--plate" and i + 1 < len(args):
            p["plate"] = args[i + 1]; i += 2
        elif args[i] == "--max-forward" and i + 1 < len(args):
            p["max_forward"] = args[i + 1]; i += 2
        else:
            i += 1

    plate = p["plate"]
    max_forward = int(p["max_forward"])

    if not plate:
        print(json.dumps({"found": False, "error": "missing --plate"}))
        return

    # Step 1: Check current page
    vehicles = _get_current_page_vehicles()
    if plate in vehicles:
        page_info = _get_pagination_state()
        pg = page_info["current"] if page_info else 0
        print(json.dumps({"found": True, "page": pg, "method": "current"}))
        return

    # Step 2: Try forward up to max_forward pages (data may have shifted)
    for fwd in range(1, max_forward + 1):
        time.sleep(random.uniform(1, 2))  # slow down
        _click_page_direct("next")
        time.sleep(random.uniform(1, 2))
        vehicles = _get_current_page_vehicles()
        if plate in vehicles:
            page_info = _get_pagination_state()
            pg = page_info["current"] if page_info else 0
            print(json.dumps({"found": True, "page": pg, "method": "forward", "forward_count": fwd}))
            return

    # Step 3: Not found - return to page 1 for full scan
    # Navigate back to page 1 using smart pagination
    page_info = _get_pagination_state()
    if page_info:
        min_p = page_info["min_page"]
        if min_p > 1:
            # Click min page to shift window toward page 1
            for _ in range(10):
                _click_page_number(min_p)
                time.sleep(random.uniform(1, 2))
                pi = _get_pagination_state()
                if pi and pi["min_page"] <= 1:
                    break
                min_p = pi["min_page"] if pi else min_p - 5

            # Click page 1 if visible
            pi = _get_pagination_state()
            if pi and 1 >= pi["min_page"] and 1 <= pi["max_page"]:
                _click_page_number(1)
                time.sleep(random.uniform(1, 2))

    print(json.dumps({"found": False, "page": 1, "method": "scan",
                       "message": f"plate {plate} not found in {max_forward} forward pages, reset to page 1"}))


def _get_current_page_vehicles():
    """Get set of plate numbers on the current page. Used internally by find-plate-page."""
    js = """
(function() {
  var plates = [];
  var table = document.querySelector('table');
  if (!table) return JSON.stringify([]);
  var rows = table.querySelectorAll('tr');
  for (var r = 0; r < rows.length; r++) {
    var tds = rows[r].querySelectorAll('td');
    if (tds.length >= 1) {
      var first = tds[0].textContent.trim();
      if (first.length >= 7 && first.length <= 8) {
        plates.push(first);
      }
    }
  }
  return JSON.stringify(plates);
})()
"""
    result = _run(["pinchtab", "eval", js])
    try:
        out = result.stdout.strip()
        m = re.search(r'\[.*\]', out, re.DOTALL)
        if m:
            return set(json.loads(m.group(0)))
    except (json.JSONDecodeError, ValueError):
        pass
    return set()


# ============================================================
# Subcommand: get-login-type
# ============================================================

def cmd_get_login_type():
    """Detect current login type: unit (单位) or personal (个人).
    Returns JSON: {type: 'unit'|'personal'|'none'}
    Used to verify we're logged in as unit user before proceeding.
    """
    text = _run(["pinchtab", "text"]).stdout
    snap = _run(["pinchtab", "snap"]).stdout

    result = {"type": "none", "details": ""}

    # Check for unit user indicators
    unit_indicators = ["公司列表", "公司名称", "单位信息", "租赁车辆", "企业用户",
                       "unit", "company", "enterprise"]
    personal_indicators = ["个人用户", "个人中心", "我的车辆", "驾驶人",
                           "personal", "individual"]

    for kw in unit_indicators:
        if kw in text or kw in snap:
            result["type"] = "unit"
            result["details"] = f"found unit indicator: {kw}"
            break

    if result["type"] == "none":
        for kw in personal_indicators:
            if kw in text or kw in snap:
                result["type"] = "personal"
                result["details"] = f"found personal indicator: {kw}"
                break

    # Check if any text at all (login state detection)
    if result["type"] == "none":
        login_kw = ["首页", "业务办理", "违法", "机动车", "home", "logout", "退出"]
        for kw in login_kw:
            if kw in text or kw in snap:
                result["type"] = "unknown"
                result["details"] = "logged in but cannot determine type"
                break

    print(json.dumps(result, ensure_ascii=False))


# ============================================================
# Subcommand: detect-rate-limit
# ============================================================

# Rate-limit / feng-kong indicators from 12123 platform
RATE_LIMIT_KEYWORDS = [
    "频繁", "异常操作", "强制退出", "黑名单", "限制使用",
    "第三方软件", "爬取", "泄露", "法律责任", "暂停服务",
    "操作过于频繁", "请稍后再试", "访问被拒绝", "account locked",
    "suspended", "rate limit", "too many requests",
]

def cmd_detect_rate_limit():
    """Check if the current page shows rate-limiting or feng-kong warnings.
    Returns JSON: {blocked: bool, keywords_found: [...], should_stop: bool}
    Exit code 1 if blocked (for script use).
    """
    text = _run(["pinchtab", "text"]).stdout
    snap = _run(["pinchtab", "snap"]).stdout
    combined = text + " " + snap

    found = [kw for kw in RATE_LIMIT_KEYWORDS if kw in combined]

    # Also check: is the vehicle table missing but we should be on vehlist?
    has_table = "号牌号码" in snap or "未处理违法" in snap
    on_vehlist = "vehlist" in snap or "租赁车" in snap

    blocked = len(found) > 0 or (on_vehlist and not has_table)

    result = {
        "blocked": blocked,
        "keywords_found": found,
        "should_stop": blocked,
        "details": "rate-limit keywords detected" if found else (
            "vehicle table missing on vehlist page" if (on_vehlist and not has_table) else "ok"
        )
    }
    print(json.dumps(result, ensure_ascii=False))
    if blocked:
        sys.exit(1)


# ============================================================
# Subcommand: dismiss-popup
# ============================================================

def cmd_dismiss_popup():
    """Dismiss any system popup/modal that blocks the vehicle table.
    Handles: 本人已知晓, 系统提示, 安全提醒, etc.
    Returns JSON: {dismissed: bool, method: str}
    """
    js = """
(function() {
  // Strategy 1: Look for dismiss buttons by text
  var dismissTexts = ['本人已知晓', '确定', '知道了', '关闭', '同意', '确认', '我知道了'];
  var all = document.querySelectorAll('button, a, span[role="button"], div[role="button"]');
  for (var i = 0; i < all.length; i++) {
    var t = (all[i].textContent || '').trim();
    for (var j = 0; j < dismissTexts.length; j++) {
      if (t.indexOf(dismissTexts[j]) !== -1 && all[i].offsetHeight > 0) {
        all[i].click();
        return 'clicked:' + t;
      }
    }
  }

  // Strategy 2: Close buttons (×)
  var closeSelectors = ['.close', '.aui_close', '.el-icon-close', '.dialog-close',
                        '.modal-close', '[class*="close"]', '.layui-layer-close'];
  for (var k = 0; k < closeSelectors.length; k++) {
    try {
      var els = document.querySelectorAll(closeSelectors[k]);
      for (var m = 0; m < els.length; m++) {
        if (els[m].offsetHeight > 0) {
          els[m].click();
          return 'closed:' + closeSelectors[k];
        }
      }
    } catch(e) {}
  }

  // Strategy 3: Look for modal with system notice text
  var modals = document.querySelectorAll('div[class*="dialog"], div[class*="modal"], div[class*="popup"], div[class*="notice"], .layui-layer');
  for (var n = 0; n < modals.length; n++) {
    var text = (modals[n].textContent || '');
    if (text.indexOf('妥善保管') !== -1 || text.indexOf('系统提示') !== -1 ||
        text.indexOf('安全提醒') !== -1 || text.indexOf('本人已知晓') !== -1) {
      // Find the confirm button inside this modal
      var btns = modals[n].querySelectorAll('button, a');
      for (var p = 0; p < btns.length; p++) {
        if (btns[p].offsetHeight > 0) {
          btns[p].click();
          return 'modal-btn-clicked';
        }
      }
    }
  }

  return 'no-popup-found';
})()
"""
    result = _run(["pinchtab", "eval", js])
    dismissed = 'clicked' in result.stdout or 'closed' in result.stdout or 'modal' in result.stdout
    print(json.dumps({"dismissed": dismissed, "method": result.stdout.strip()}, ensure_ascii=False))


# ============================================================
# Subcommand: check-login-valid
# ============================================================

def cmd_check_login_valid():
    """Check if current login is valid for the target province/company.
    Does NOT logout - only verifies. Returns JSON with login state.
    """
    snap = _run(["pinchtab", "snap"]).stdout
    text = _run(["pinchtab", "text"]).stdout
    combined = text + " " + snap

    has_logout = "退出" in combined
    has_unit = "公司列表" in combined or "租赁车" in combined
    has_personal = "个人用户" in combined
    is_logged_in = has_logout and not "单位用户登录" in snap and not "个人用户登录" in snap

    result = {
        "logged_in": is_logged_in,
        "is_unit": has_unit or (is_logged_in and not has_personal),
        "has_logout_btn": has_logout,
        "action": "continue" if is_logged_in and (has_unit or has_logout) else "login_required"
    }
    print(json.dumps(result, ensure_ascii=False))



# ============================================================
# Subcommand: new-tab
# ============================================================

def cmd_new_tab():
    """Create a new browser tab and return its ID. Used for session isolation
    so multiple Claude processes can share the same Chrome login session
    without interfering with each other's page navigation.

    Returns JSON: {tab_id: str, ok: bool}
    """
    # Get existing tab IDs before creating new one
    before_result = _run(["pinchtab", "tab"])
    before_ids = set(re.findall(r'\b(\d+)\b', before_result.stdout))

    # Open a new blank tab via JS
    _run(["pinchtab", "eval", "window.open('about:blank')"])
    time.sleep(1)

    # Get tab IDs after
    after_result = _run(["pinchtab", "tab"])
    after_ids = set(re.findall(r'\b(\d+)\b', after_result.stdout))

    new_ids = after_ids - before_ids
    if new_ids:
        tab_id = sorted(new_ids, key=int)[-1]
    else:
        # Fallback: take the highest numeric ID from after
        all_ids = sorted(after_ids, key=int)
        tab_id = all_ids[-1] if all_ids else "0"

    # Switch to the new tab so it's active
    _run(["pinchtab", "tab", tab_id])

    print(json.dumps({"tab_id": tab_id, "ok": True}, ensure_ascii=False))


# ============================================================
# Subcommand: switch-tab
# ============================================================

def cmd_switch_tab():
    """Switch to a specific browser tab by ID.

    Args: --id TAB_ID
    Returns JSON: {tab_id: str, ok: bool}
    """
    p = {"id": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--id" and i + 1 < len(args):
            p["id"] = args[i + 1]; i += 2
        else:
            i += 1

    if not p["id"]:
        print(json.dumps({"ok": False, "error": "missing --id"}, ensure_ascii=False))
        sys.exit(1)

    result = _run(["pinchtab", "tab", p["id"]])
    success = result.returncode == 0
    print(json.dumps({"tab_id": p["id"], "ok": success}, ensure_ascii=False))
    if not success:
        sys.exit(1)



SUBCOMMANDS = {
    "get-dir": cmd_get_dir,
    "get-screenshot-dir": cmd_get_screenshot_dir,
    "get-report-dir": cmd_get_report_dir,
    "get-data-dir": cmd_get_data_dir,
    "license-lookup": cmd_license_lookup,
    "province-url": cmd_province_url,
    "province-login-url": cmd_province_login_url,
    "get-login-url": cmd_get_login_url,
    "pinchtab-path": cmd_pinchtab_path,
    "lark-cli-path": cmd_lark_cli_path,
    "gen-qr-msg": cmd_gen_qr_msg,
    "gen-qr-fallback": cmd_gen_qr_fallback,
    "gen-result-msg": cmd_gen_result_msg,
    "upload-image": cmd_upload_image,
    "send-msg": cmd_send_msg,
    "send-image-msg": cmd_send_image_msg,
    "switch-tab": cmd_switch_tab,
    "init-db": cmd_init_db,
    "db-insert-company": cmd_db_insert_company,
    "db-insert-vehicle": cmd_db_insert_vehicle,
    "db-insert-violation": cmd_db_insert_violation,
    "profile-lookup": cmd_profile_lookup,
    "profile-register": cmd_profile_register,
    "profile-logout": cmd_profile_logout,
    "search-user": cmd_search_user,
    "search-chat": cmd_search_chat,
    "batch-get-id": cmd_batch_get_id,
    "pt-find": cmd_pt_find,
    "pt-wait": cmd_pt_wait,
    "poll-login": cmd_poll_login,
    "consume-event": cmd_consume_event,
    "extract-message-id": cmd_extract_message_id,
    "prepare-dir": cmd_prepare_dir,
    "init": cmd_init,
    "run-js": cmd_run_js,
    "list-vehicles": cmd_list_vehicles,
    "new-tab": cmd_new_tab,
    "open-vehicle": cmd_open_vehicle,
    "collect-violations": cmd_collect_violations,
    "go-back": cmd_go_back,
    "click-page": cmd_click_page,
    "save-detail-progress": cmd_save_detail_progress,
    "load-detail-progress": cmd_load_detail_progress,
    "reset-detail-progress": cmd_reset_detail_progress,
    "get-page-vehicles": cmd_get_page_vehicles,
    "get-login-type": cmd_get_login_type,
    "detect-rate-limit": cmd_detect_rate_limit,
    "dismiss-popup": cmd_dismiss_popup,
    "check-login-valid": cmd_check_login_valid,
    "find-plate-page": cmd_find_plate_page,
}

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 violation_helper.py <subcommand> [args...]", file=sys.stderr)
        print(f"Available: {', '.join(sorted(SUBCOMMANDS))}", file=sys.stderr)
        sys.exit(1)

    # Parse --output/-o before dispatching
    _setup_output_file()

    subcmd = sys.argv[1]
    if subcmd in SUBCOMMANDS:
        SUBCOMMANDS[subcmd]()
    else:
        print(f"Unknown subcommand: {subcmd}", file=sys.stderr)
        print(f"Available: {', '.join(sorted(SUBCOMMANDS))}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

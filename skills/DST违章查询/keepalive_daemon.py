#!/usr/bin/env python3
"""
Keepalive Daemon — 独立于 Claude 会话的 12123 登录保活守护进程。

每个公司一个独立守护进程，通过 nohup + disown 启动后完全脱离 Claude 会话。
支持多 PinchTab 实例（不同 instance_port）和标签页隔离。

用法:
  启动: nohup python3 keepalive_daemon.py --company "公司名" --project-root "/path/to/project" &
  停止: python3 keepalive_daemon.py --company "公司名" --project-root "/path/to/project" --stop
  状态: python3 keepalive_daemon.py --company "公司名" --project-root "/path/to/project" --status

生命周期:
  1. 启动时从 profiles 表读取公司信息（instance_port、platform_url）
  2. 创建专属 keepalive 标签页并导航到平台 URL（首次）
  3. 每 55 分钟执行一个保活周期：nav vehlist + dismiss popup
  4. 每个周期开始前检查 is_logged_in，若变为 0 则自动退出
  5. nav 连续失败 → profile-logout → 退出
  6. 检测到登录页或风控关键词 → profile-logout → 退出（若启用 auto-recover 则尝试一次 QR 恢复）
  7. 收到 SIGTERM/SIGINT → 清理 PID 文件后退出

自动恢复策略（--auto-recover）:
  - 每次保活会话最多触发 **一次** 自动恢复
  - 那次恢复内最多发送 **3 次** QR 码（应对二维码过期自动刷新）
  - 3 次 QR 均超时或恢复失败 → 静默退出，等待下次查询任务自然触发重新登录

多公司隔离:
  - PID 文件: violation_query/data/keepalive_<公司>.pid
  - 日志文件: violation_query/data/keepalive_<公司>.log
  - 标签页: 每个守护进程创建独立的 PinchTab Tab，通过 tab_id 隔离
  - 实例: 通过 profiles.instance_port 支持不同 PinchTab daemon 实例
"""

import argparse
import json
import logging
import os
import random

import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

# Ensure lib/ is importable (keepalive_daemon.py lives in the skill dir alongside lib/)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.core import (
    UNIT_LOGIN_URL,
    RATE_LIMIT_KEYWORDS,
    _parse_tab_ids,
)

# ── constants ──────────────────────────────────────────────────
INTERVAL_SECONDS = 55 * 60          # 55 minutes (full page reload cycle)
HEARTBEAT_MIN_SEC = 60               # min seconds between light heartbeats
HEARTBEAT_MAX_SEC = 120              # max seconds between light heartbeats
PAGE_LOAD_WAIT = 5                   # wait after reload
POPUP_DISMISS_WAIT = 3               # wait after dismiss
MAX_CONSECUTIVE_FAILURES = 3         # consecutive reload failures → exit
MAX_CONSECUTIVE_UNKNOWN = 3         # consecutive unknown states → exit (CDP broken?)
MAX_CONSECUTIVE_HEARTBEAT_FAILS = 5  # consecutive heartbeat fails → treat as potential stall

# ── exit codes (for systemd RestartPreventExitStatus) ─────────
# systemd Restart=always will NOT restart on these exit codes
# when RestartPreventExitStatus=42 43 is configured.
EXIT_OK = 0              # Normal exit (SIGTERM/SIGINT, user stop)
EXIT_LOGGED_OUT = 42     # Login expired / is_logged_in=0 → don't restart
EXIT_RATE_LIMITED = 43   # Rate-limited / fengkong → don't restart
EXIT_ALREADY_RUNNING = 44  # Another daemon already running → don't restart
MAX_RECOVERY_ATTEMPTS = 1            # one recovery opportunity per keepalive session
MAX_QR_REFRESHES = 3                 # within the one recovery, up to 3 QR sends (过期刷新)
RECOVERY_POLL_INTERVAL = 10          # seconds between login checks during recovery
RECOVERY_TIMEOUT = 300               # 5 minutes per QR code before refresh
LOGIN_PAGE_WAIT = 3                  # wait after selecting unit login tab before QR appears

# Paths for external tools (resolved at runtime)
_TAB_SESSION = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "session_manager.py")
_COOKIE_PERSIST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "cookie_persist.py")
_LARK_CLI = "lark-cli"

def _resolve_pinchtab():
    """Find pinchtab binary on PATH."""
    for name in ["pinchtab", "pinchtab.exe"]:
        for d in os.environ.get("PATH", "").split(os.pathsep):
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return "pinchtab"


def _run_pinchtab(args, instance_port=None, timeout=60):
    """Run pinchtab command, return CompletedProcess.

    Direct subprocess.run with timeout.  If the child process hangs in D state
    (uninterruptible sleep), the main loop will stop feeding the systemd
    watchdog and systemd will kill + restart the daemon after WatchdogSec.

    If instance_port is set, injects --server flag to target the correct
    PinchTab daemon instance.

    Tab isolation: if VIOLATION_TAB_ID env var is set, automatically injects
    --tab <id> into pinchtab commands that support it.  This prevents
    competing with other sessions for the global active tab."""
    pt_bin = _resolve_pinchtab()
    cmd = [pt_bin]

    # ── Instance isolation: inject --server flag ──
    if instance_port:
        cmd += ["--server", f"http://127.0.0.1:{instance_port}"]

    # ── Tab isolation: inject --tab <id> ──
    _TAB_AWARE = frozenset({
        "nav", "eval", "click", "dblclick", "snap", "text", "find", "wait",
        "screenshot", "reload", "back",
    })
    tab_id = os.environ.get("VIOLATION_TAB_ID", "")
    if tab_id and len(args) > 0 and args[0] in _TAB_AWARE:
        cmd += [args[0], "--tab", tab_id] + list(args[1:])
    else:
        cmd += args

    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, timeout=timeout
    )


def _get_db_path(project_root, company=None):
    data_dir = os.path.join(project_root, "violation_query", "data")
    return os.path.join(data_dir, "violations.db")


def _get_pid_file(project_root, company):
    safe = _safe_name(company)
    data_dir = os.path.join(project_root, "violation_query", "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, f"keepalive_{safe}.pid")


def _get_log_file(project_root, company):
    safe = _safe_name(company)
    data_dir = os.path.join(project_root, "violation_query", "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, f"keepalive_{safe}.log")


def _get_profile_dir(profile_name=None, instance_port=None):
    """Resolve Chrome profile directory path.

    Tries the named profile directory first.  If that directory doesn't
    contain a Default/Cookies file (i.e. the profile doesn't actually exist),
    falls back to scanning for prof_* directories that do contain Cookies."""
    name = profile_name or "default"
    named = os.path.expanduser(f"~/.pinchtab/profiles/{name}")
    if os.path.exists(os.path.join(named, "Default", "Cookies")):
        return named
    # Fallback: scan for prof_* directory that has a real Cookies DB
    profiles_root = os.path.expanduser("~/.pinchtab/profiles")
    try:
        for entry in sorted(os.listdir(profiles_root)):
            if entry.startswith("prof_"):
                p = os.path.join(profiles_root, entry)
                if os.path.exists(os.path.join(p, "Default", "Cookies")):
                    return p
    except OSError:
        pass
    return named  # last resort — caller will log a warning if path is missing


def _get_tab_file(project_root, company):
    """File to persist the keepalive tab ID across daemon restarts."""
    safe = _safe_name(company)
    data_dir = os.path.join(project_root, "violation_query", "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, f"keepalive_tab_{safe}.txt")


def _get_health_file(project_root, company):
    """File touched every cycle so query processes can verify liveness."""
    safe = _safe_name(company)
    data_dir = os.path.join(project_root, "violation_query", "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, f"keepalive_health_{safe}.json")


def _touch_health(health_file, state, tab_id, cycle_count, instance_port, log):
    """Write health status for external consumers (query processes)."""
    try:
        data = {
            "state": state,               # "logged_in" | "login_expired" | "rate_limited"
            "tab_id": tab_id,             # current keepalive tab (hex)
            "cycle_count": cycle_count,
            "instance_port": instance_port,
            "last_check": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log.debug(f"Health file write skipped: {e}")


def _get_session_state_path(project_root):
    """Path to session_state.json (N2)."""
    data_dir = os.path.join(project_root, "violation_query", "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "session_state.json")


def _write_session_state(project_root, company, instance_port, tab_id, cycle_count,
                         session_valid, log):
    """N2: Write keepalive heartbeat to session_state.json.
    Pure informational — does NOT trigger query actions.
    Query processes read this to check if session is still valid."""
    try:
        path = _get_session_state_path(project_root)
        # Read-modify-write to preserve query-side fields
        data = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        data["last_keepalive_heartbeat"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data["keepalive_cycle"] = cycle_count
        data["session_valid"] = session_valid
        data["keepalive_instance_port"] = instance_port
        data["keepalive_tab_id"] = tab_id
        data["keepalive_company"] = company
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.debug(f"Session state write skipped: {e}")


def _get_notify_file(project_root, company):
    """File to persist the auto-recovery notify target across daemon restarts.
    Written by the query flow so the daemon knows who to notify without
    needing CLI args on every restart."""
    safe = _safe_name(company)
    data_dir = os.path.join(project_root, "violation_query", "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, f"keepalive_notify_{safe}.json")


def _load_notify(project_root, company, log=None):
    """Load persisted notify target. Returns dict or None."""
    notify_file = _get_notify_file(project_root, company)
    try:
        if os.path.exists(notify_file):
            with open(notify_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("id"):
                if log:
                    log.info("Loaded persisted notify: %s=%s (%s…)",
                             data.get("type"), data.get("label"), data["id"][:8])
                return data
    except Exception as e:
        if log:
            log.warning("Failed to load notify file %s: %s", notify_file, e)
    return None


def _save_notify(project_root, company, notify, log=None):
    """Persist notify target to disk. Returns True on success."""
    notify_file = _get_notify_file(project_root, company)
    try:
        with open(notify_file, "w", encoding="utf-8") as f:
            json.dump(notify, f, ensure_ascii=False)
        if log:
            log.info("Notify target persisted: %s=%s (%s…)",
                     notify.get("type"), notify.get("label"),
                     (notify.get("id") or "")[:8])
        return True
    except Exception as e:
        if log:
            log.warning("Failed to persist notify: %s", e)
        return False


# ── systemd notify with failure tracking ──────────────────────
_sd_notify_failures = 0

def _sd_notify(msg, log=None):
    """Send watchdog notification to systemd. No-op if not running under systemd.

    Tracks consecutive failures and logs warnings so notification breakdowns
    are visible instead of silently allowing systemd watchdog timeout."""
    global _sd_notify_failures
    try:
        import socket
        sock_path = os.environ.get("NOTIFY_SOCKET", "")
        if not sock_path:
            return
        addr = sock_path if sock_path.startswith("/") else f"\0{sock_path}"
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.sendto(msg.encode("utf-8"), addr)
        s.close()
        _sd_notify_failures = 0  # reset on success
    except Exception as e:
        _sd_notify_failures += 1
        if _sd_notify_failures >= 3 and log:
            log.warning(
                "sd_notify('%s') failed %d consecutive times: %s",
                msg, _sd_notify_failures, e
            )
        elif _sd_notify_failures == 1 and log:
            log.debug("sd_notify('%s') failed (1st occurrence): %s", msg, e)


def _safe_name(company):
    return company.replace("/", "_").replace(" ", "_")


def _read_profile(db_path, company):
    """Read full profile for company. Returns dict or None."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "SELECT company_name, is_logged_in, profile_name, platform_url, instance_port "
        "FROM profiles WHERE company_name = ?", (company,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {
            "company_name": row[0],
            "is_logged_in": bool(row[1]),
            "profile_name": row[2],
            "platform_url": row[3],
            "instance_port": row[4],
        }
    return None


def _set_logged_out(db_path, company):
    """Set is_logged_in=0 for company. Returns True if updated."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE profiles SET is_logged_in = 0 WHERE company_name = ?",
        (company,))
    updated = conn.total_changes
    conn.commit()
    conn.close()
    return updated > 0




def _set_logged_in(db_path, company, instance_port=None):
    """Set is_logged_in=1 for company. Returns True if updated."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE profiles SET is_logged_in = 1, last_login = datetime('now','localtime')"
        + (" , instance_port = ?" if instance_port else "") +
        " WHERE company_name = ?",
        (instance_port, company) if instance_port else (company,))
    updated = conn.total_changes
    conn.commit()
    conn.close()
    return updated > 0


# ── pid file management ────────────────────────────────────────

def _write_pid(pid_file):
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))


def _remove_pid(pid_file):
    try:
        os.remove(pid_file)
    except OSError:
        pass


def _check_running(pid_file):
    """Check if a daemon is already running. Returns (running: bool, pid: int|None)."""
    if not os.path.exists(pid_file):
        return False, None
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True, pid
    except (OSError, ValueError):
        _remove_pid(pid_file)
        return False, None


# ── tab management ─────────────────────────────────────────────

def _save_tab_id(tab_file, tab_id):
    with open(tab_file, "w") as f:
        f.write(str(tab_id))


def _load_tab_id(tab_file):
    """Read persisted tab ID. Returns None if not found."""
    if not os.path.exists(tab_file):
        return None
    try:
        with open(tab_file) as f:
            return f.read().strip()
    except Exception:
        return None


def _cleanup_stale_tabs(instance_port, keep_tab_id, log):
    """Close stale login-page tabs, keep the platform-page tab.

    Stale tabs (gab.122.gov.cn/m/login, deptLoginNext) accumulate from
    repeated restarts.  Closing them is safe — cookies are per-profile,
    not per-tab.  The platform-page tab (fj/sc/gd.122.gov.cn/*) MUST be
    kept — closing the last tab breaks the 12123 session.
    """
    if not instance_port:
        return
    try:
        result = _run_pinchtab(["tab", "--json"], instance_port=instance_port, timeout=10)
        tabs = json.loads(result.stdout)
        if isinstance(tabs, dict):
            tabs = tabs.get("tabs", [])
    except Exception as e:
        log.debug(f"Cleanup: cannot list tabs: {e}")
        return

    stale_patterns = [
        "gab.122.gov.cn/m/login",
        "gab.122.gov.cn/m/deptLoginNext",
    ]
    closed = 0
    for tab in tabs:
        url = tab.get("url", "")
        tid = tab.get("id", "")
        if tid == keep_tab_id:
            continue  # never close the keepalive tab
        if any(p in url for p in stale_patterns):
            try:
                _run_pinchtab(["close", tid], instance_port=instance_port, timeout=10)
                log.info(f"Cleanup: closed stale tab {tid[:16]}... ({url[:50]})")
                closed += 1
            except Exception as e:
                log.debug(f"Cleanup: close tab {tid[:16]}... failed: {e}")
    if closed:
        log.info(f"Cleanup: closed {closed} stale login tab(s) on instance {instance_port}")


def _cleanup_tab_registry(project_root, company, log):
    """Remove this daemon's entry from tab_registry.json."""
    registry_path = os.path.join(project_root, "violation_query", "data", "tab_registry.json")
    if not os.path.exists(registry_path):
        return
    try:
        with open(registry_path) as f:
            registry = json.load(f)
    except Exception:
        return

    # Key format used by session_manager.py: keepalive_<company>
    key = f"keepalive_{company}"
    if key in registry:
        del registry[key]
        try:
            with open(registry_path, "w") as f:
                json.dump(registry, f, indent=2, ensure_ascii=False)
            log.info(f"Cleanup: removed '{key}' from tab_registry.json")
        except Exception as e:
            log.debug(f"Cleanup: write tab_registry.json failed: {e}")


def _create_keepalive_tab(instance_port, platform_url, project_root, company, log):
    """Create a new browser tab for keepalive and navigate directly to 我的主页 (vehlist).

    Uses tab_session.py init --json to create and register the tab, then sets
    VIOLATION_TAB_ID so all subsequent _run_pinchtab() calls auto-inject --tab.
    No global tab switching — all operations target this tab via --tab flag.

    Returns the new tab_id (hex string), or None on failure."""
    try:
        # Create tab via session_manager.py (uses PinchTab HTTP API POST /tab)
        safe_label = _safe_name(company)
        init_cmd = [sys.executable, _TAB_SESSION, "init", "--json",
                    "--label", f"keepalive_{safe_label}",
                    "--project-root", project_root]
        if instance_port:
            init_cmd += ["--instance-port", str(instance_port)]
        new_tab_result = subprocess.run(
            init_cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=15,
        )
        if new_tab_result.returncode != 0:
            log.error(f"tab_session init failed: {new_tab_result.stderr.strip()}")
            return None
        tab_info = json.loads(new_tab_result.stdout)
        if not tab_info.get("ok"):
            log.error(f"tab_session init returned error: {tab_info.get('error', 'unknown')}")
            return None
        tab_id = tab_info["tab_id"]
        log.info(f"Created keepalive tab {tab_id} via tab_session.py")

        # Set VIOLATION_TAB_ID so all subsequent _run_pinchtab() calls
        # auto-inject --tab <id> without switching the global active tab
        os.environ["VIOLATION_TAB_ID"] = tab_id

        # Navigate to 我的主页 (vehicle list page) when we have a valid session.
        # Auto-recovery passes UNIT_LOGIN_URL here — skip vehlist nav in that case
        # because the user hasn't logged in yet.
        if "122.gov.cn" in platform_url and "/m/login" not in platform_url:
            vehlist_url = platform_url.rstrip("/") + "/views/memrent/vehlist.html"
            log.info(f"Navigating keepalive tab {tab_id} to {vehlist_url}")
            _run_pinchtab(["nav", vehlist_url], instance_port=instance_port, timeout=30)
        else:
            log.info(f"Navigating keepalive tab {tab_id} to {platform_url}")
            _run_pinchtab(["nav", platform_url], instance_port=instance_port, timeout=30)
        time.sleep(PAGE_LOAD_WAIT)

        # Dismiss any popup that appears after navigation
        _dismiss_popup(instance_port, log)

        return tab_id
    except Exception as e:
        log.error(f"Failed to create keepalive tab: {e}")
        return None


def _verify_tab(tab_id, instance_port, log):
    """Verify the keepalive tab still exists and is on a 12123 page.

    Uses eval with --tab targeting (injected by _run_pinchtab via VIOLATION_TAB_ID),
    so this does NOT switch the global active tab — no interference with other sessions.

    Returns True if the tab is alive and on a known 12123 domain or about:blank."""
    try:
        url_js = "(function(){return window.location.href;})()"
        url_result = _run_pinchtab(["eval", url_js],
                                   instance_port=instance_port, timeout=5)
        current_url = (url_result.stdout or "").strip()
        stderr_text = (url_result.stderr or "").strip()
        rc = url_result.returncode

        # Check for dead-tab indicators first
        stderr_lower = stderr_text.lower()
        if "404" in stderr_lower or "tab not found" in stderr_lower:
            log.warning(f"Tab {tab_id} verification failed — tab not found in browser "
                        f"(rc={rc}, stderr: {stderr_text[:120]})")
            return False

        if rc != 0:
            log.warning(f"Tab {tab_id} verification failed — non-zero exit code "
                        f"(rc={rc}, stderr: {stderr_text[:120]})")
            return False

        if not current_url:
            log.warning(f"Tab {tab_id} verification failed — empty URL returned "
                        f"(rc={rc}, stderr: {stderr_text[:120]})")
            return False

        if "122.gov.cn" in current_url or "about:blank" in current_url:
            log.debug(f"Tab {tab_id} verified: {current_url[:80]}")
            return True

        log.warning(f"Tab {tab_id} verification failed — "
                    f"unexpected URL: {current_url[:120]}")
        return False
    except Exception as e:
        log.warning(f"Tab verify exception: {e}")
        return False


# ── keepalive cycle ────────────────────────────────────────────

def _dismiss_popup(instance_port, log):
    """Dismiss popup dialogs via JS dispatchEvent (4-strategy)."""
    js = """
(function() {
  var allElements = document.querySelectorAll('button, a, span, div, i');
  var closeTexts = ['关闭', '\xd7', '取消', 'close', 'x', '本人已知晓',
                     '知道了', '确定', '我已阅读', '同意'];
  for (var i = 0; i < allElements.length; i++) {
    var el = allElements[i];
    var text = (el.textContent || '').trim();
    for (var j = 0; j < closeTexts.length; j++) {
      if (text === closeTexts[j] || text.indexOf(closeTexts[j]) !== -1) {
        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
        return 'js-clicked:' + text;
      }
    }
  }
  var closeSelectors = ['.close', '.el-icon-close', '.dialog-close', '.modal-close',
                        '[class*="close"]', '.cancel-btn', '.ant-modal-close',
                        '.el-dialog__close', '.layui-layer-close'];
  for (var k = 0; k < closeSelectors.length; k++) {
    try {
      var els = document.querySelectorAll(closeSelectors[k]);
      for (var m = 0; m < els.length; m++) {
        els[m].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
        return 'js-clicked-selector:' + closeSelectors[k];
      }
    } catch(e) {}
  }
  try {
    document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape',code:'Escape',keyCode:27,bubbles:true}));
  } catch(e2) {}
  var modals = document.querySelectorAll('.el-dialog__wrapper, .el-overlay, .modal, .ant-modal-wrap, .layui-layer');
  for (var n = 0; n < modals.length; n++) {
    modals[n].style.display = 'none';
  }
  return 'done';
})()
"""
    try:
        _run_pinchtab(["eval", js], instance_port=instance_port, timeout=15)
        time.sleep(POPUP_DISMISS_WAIT)
        log.debug("Popup dismiss executed")
    except Exception as e:
        log.warning(f"Popup dismiss error: {e}")


def _navigate_to_homepage(instance_port, log):
    """Click 我的主页 to navigate to the vehicle list page.

    The vehicle list page (vehlist.html) is the best keepalive target because:
    - It shows real login state (退出/我的主页 buttons are only visible when logged in)
    - Rate-limiting will manifest here (vehicle list won't load)
    - Session expiry is immediately visible (redirect to login page)
    """
    try:
        # Check if we're already on the homepage (has vehicle list or 我的主页 button)
        snap = _run_pinchtab(["snap"], instance_port=instance_port, timeout=15)
        snap_text = snap.stdout

        # Find the ref for 我的主页
        ref_match = re.search(r'(e\d+):(?:button|link)\s+"我的主页', snap_text)
        if not ref_match:
            log.debug("我的主页 button not found in snap — may already be on homepage or logged out")
            return False

        ref = ref_match.group(1)
        log.info(f"Clicking 我的主页 (ref={ref})")
        _run_pinchtab(["click", ref], instance_port=instance_port, timeout=15)
        time.sleep(PAGE_LOAD_WAIT)

        # Dismiss any popup that appears after navigation
        _dismiss_popup(instance_port, log)
        return True
    except Exception as e:
        log.warning(f"Navigate to homepage error: {e}")
        return False


def _check_page_state(instance_port, platform_url, log):
    """Check page state after reload.

    After reload, verify the current URL is still on the platform (platform_url).
    If the session expired, the server redirects to gab.122.gov.cn/m/login.

    Rate-limit detection: snap check for anti-crawl keywords, since rate-limit
    popups don't cause a URL redirect.

    Returns: ("logged_in"|"login_expired"|"rate_limited"|"unknown", detail_text)
    """
    # ── rate-limit check (best-effort, non-fatal on failure) ──
    try:
        snap_result = _run_pinchtab(["snap"], instance_port=instance_port, timeout=15)
        snap_text = snap_result.stdout
        for kw in RATE_LIMIT_KEYWORDS:
            if kw in snap_text:
                log.warning(f"Rate-limit keyword detected: '{kw}'")
                return ("rate_limited", f"found keyword: {kw}")
    except Exception as e:
        log.debug(f"Snap rate-limit check skipped (non-fatal): {e}")

    # ── URL check: must be on the platform, not anywhere else ──
    try:
        url_result = _run_pinchtab(
            ["eval", "window.location.href"],
            instance_port=instance_port, timeout=5
        )
        url = (url_result.stdout or "").strip()
        if url.startswith(platform_url) and "vehlist" in url and "/m/login" not in url:
            log.info(f"Session OK (URL={url[:80]})")
            return ("logged_in", url[:80])
        log.warning(f"Session expired: not on 我的主页 (URL={url[:80]})")
        return ("login_expired", f"not on 我的主页: {url[:80]}")
    except Exception as e:
        # URL eval failed — instance may be slow/busy, don't mark as
        # login_expired (that would trigger false auto-recovery).
        log.warning(f"URL check failed (transient, retry next cycle): {e}")
        return ("unknown", f"URL check failed: {e}")


# ── heartbeat ──────────────────────────────────────────────────

def _heartbeat(instance_port, log):
    """Light touch to simulate user activity without a full page reload.

    Performs a random scroll + lightweight JS ping to keep the server-side
    session warm and detect page stalls early (60-120s vs 18 min).

    Returns:
        True  — heartbeat succeeded (page responsive, JS executed)
        False — heartbeat failed (pinchtab timeout / eval error / tab dead)
    """
    try:
        # Random scroll: pick a y-offset in [100, 1200] to simulate reading
        scroll_y = random.randint(100, 1200)
        scroll_js = f"(function(){{window.scrollTo(0, {scroll_y}); return 'ok'}})()"

        scroll_result = _run_pinchtab(
            ["eval", scroll_js], instance_port=instance_port, timeout=10
        )
        # Check returncode AND detect dead-tab indicators
        # pinchtab eval on a dead tab returns exit 0 + empty stdout + "404" in stderr
        scroll_stderr = (scroll_result.stderr or "").lower()
        if scroll_result.returncode != 0:
            log.warning(f"Heartbeat scroll failed (rc={scroll_result.returncode}): "
                        f"{scroll_result.stderr[:100]}")
            return False
        if "404" in scroll_stderr or "tab not found" in scroll_stderr:
            log.warning(f"Heartbeat scroll: tab appears DEAD (stderr: {scroll_result.stderr[:100]})")
            return False
        if not (scroll_result.stdout or "").strip():
            log.warning("Heartbeat scroll: empty stdout — tab may be dead")
            return False

        # Lightweight ping: read a known DOM element to verify page is alive
        ping_js = (
            "(function(){"
            "var e=document.querySelector('.navbar-brand')||document.querySelector('h1')"
            "||document.querySelector('title');"
            "return e?e.textContent.trim().substring(0,20):'alive';"
            "})()"
        )
        ping_result = _run_pinchtab(
            ["eval", ping_js], instance_port=instance_port, timeout=10
        )
        ping_stderr = (ping_result.stderr or "").lower()
        if ping_result.returncode != 0:
            log.warning(f"Heartbeat ping failed (rc={ping_result.returncode}): "
                        f"{ping_result.stderr[:100]}")
            return False
        if "404" in ping_stderr or "tab not found" in ping_stderr:
            log.warning(f"Heartbeat ping: tab appears DEAD (stderr: {ping_result.stderr[:100]})")
            return False
        if not (ping_result.stdout or "").strip():
            log.warning("Heartbeat ping: empty stdout — tab may be dead")
            return False

        # Occasionally (1/5 chance) dismiss any popups that may have appeared
        if random.randint(1, 5) == 1:
            _dismiss_popup(instance_port, log)

        return True

    except Exception as e:
        log.warning(f"Heartbeat exception: {e}")
        return False


# ── cookie persistence ──────────────────────────────────────────

def _persist_cookies(profile_dir, log):
    """Convert session cookies to persistent so they survive Chrome restart."""
    if not os.path.exists(_COOKIE_PERSIST):
        log.warning(f"cookie_persist.py not found at {_COOKIE_PERSIST}")
        return False
    try:
        result = subprocess.run(
            [sys.executable, _COOKIE_PERSIST, "--profile", profile_dir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=15
        )
        if result.returncode == 0:
            # Extract how many were converted from output
            for line in result.stdout.splitlines():
                if "Converted" in line:
                    log.info(f"Cookie persist: {line.strip()}")
                    return True
            log.info("Cookie persist: already all persistent (no changes needed)")
            return True
        else:
            log.warning(f"Cookie persist returned exit {result.returncode}: "
                        f"{result.stderr[:200]}")
            return False
    except Exception as e:
        log.error(f"Cookie persist exception: {e}")
        return False


# ── auto-recovery: QR re-login ─────────────────────────────────

def _run_lark(args, timeout=30, cwd=None):
    """Run lark-cli command, return CompletedProcess."""
    cmd = [_LARK_CLI] + args
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        env=env, timeout=timeout, cwd=cwd
    )


def _resolve_notify_target(name=None, phone=None, chat=None, log=None):
    """Resolve a human-readable notify target to (type, id, label).

    Supports three resolution methods (tried in order):
      - name  → lark-cli contact +search-user (--as user)
      - phone → lark-cli api batch_get_id (--as bot)
      - chat  → lark-cli api chat search (--as bot)

    Returns dict {"type": "user"|"chat", "id": "<open_id or chat_id>",
                   "label": "<human-readable>"}
    or {"type": None, "id": None, "label": None} if all methods fail.
    """
    if name:
        for attempt in range(3):
            try:
                result = _run_lark(
                    ["contact", "+search-user", "--query", name, "--as", "user"],
                    timeout=20
                )
                data = json.loads(result.stdout)
                user_list = data if isinstance(data, list) else []
                if not user_list:
                    inner = data.get("data", {}) if isinstance(data, dict) else {}
                    user_list = inner.get("users", [])
                if user_list:
                    uid = user_list[0].get("open_id") or user_list[0].get("id")
                    if uid:
                        label = user_list[0].get("localized_name") or user_list[0].get("name") or name
                        if log:
                            log.info("Resolved user '%s' → %s (%s…)", name, label, uid[:8])
                        return {"type": "user", "id": uid, "label": label}
            except Exception as e:
                if log:
                    log.warning("search-user attempt %d/3 failed: %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(2)
        if log:
            log.warning("Could not resolve user '%s' after 3 attempts", name)

    if phone:
        for attempt in range(3):
            try:
                result = _run_lark([
                    "api", "POST", "/open-apis/contact/v3/users/batch_get_id",
                    "--data", json.dumps({"mobiles": [phone]}),
                    "--params", json.dumps({"user_id_type": "open_id"}),
                    "--as", "bot"
                ], timeout=20)
                data = json.loads(result.stdout)
                user_list = data.get("data", {}).get("user_list", [])
                if user_list:
                    uid = user_list[0].get("user_id") or user_list[0].get("open_id")
                    if uid:
                        if log:
                            log.info("Resolved phone '%s' → %s…", phone, uid[:8])
                        return {"type": "user", "id": uid, "label": phone}
            except Exception as e:
                if log:
                    log.warning("batch_get_id attempt %d/3 failed: %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(2)
        if log:
            log.warning("Could not resolve phone '%s' after 3 attempts", phone)

    if chat:
        for attempt in range(3):
            try:
                result = _run_lark([
                    "api", "GET", "/open-apis/im/v1/chats/search",
                    "--params", json.dumps({"query": chat, "page_size": 5}),
                    "--as", "bot"
                ], timeout=20)
                data = json.loads(result.stdout)
                items = data.get("data", {}).get("items", [])
                if items:
                    chat_id = items[0].get("chat_id")
                    chat_name = items[0].get("name", chat)
                    if chat_id:
                        if log:
                            log.info("Resolved chat '%s' → %s (%s…)", chat, chat_name, chat_id[:8])
                        return {"type": "chat", "id": chat_id, "label": chat_name}
            except Exception as e:
                if log:
                    log.warning("chat search attempt %d/3 failed: %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(2)
        if log:
            log.warning("Could not resolve chat '%s' after 3 attempts", chat)

    return {"type": None, "id": None, "label": None}


def _take_qr_screenshot(instance_port, output_path, log):
    """Take screenshot of current page (QR code). Returns True if successful."""
    try:
        result = _run_pinchtab(
            ["screenshot", "-o", output_path, "--format", "png"],
            instance_port=instance_port, timeout=15
        )
        if result.returncode == 0 and os.path.exists(output_path):
            log.info(f"QR screenshot saved: {output_path}")
            return True
        else:
            log.error(f"Screenshot failed: {result.stderr[:200]}")
            return False
    except Exception as e:
        log.error(f"Screenshot exception: {e}")
        return False


def _ensure_unit_login_tab(instance_port, log):
    """Ensure the '单位用户登录' tab is selected on the login page.

    12123 login page has two tabs: 个人用户登录 (t=1) and 单位用户登录 (t=2).
    Even with ?t=2 in URL, the page may not reliably select the unit tab.
    This function checks if the unit tab is active, and clicks it if not.

    Returns True if unit tab is confirmed selected, False on error.
    """
    js = """
(function() {
  // Find the unit login tab element
  var all = document.querySelectorAll('*');
  var unitTab = null;
  for (var i = 0; i < all.length; i++) {
    var el = all[i];
    var text = (el.textContent || '').trim();
    if (text.indexOf('单位用户') !== -1 && text.indexOf('登录') !== -1 && text.length < 30) {
      unitTab = el;
      break;
    }
  }
  if (!unitTab) return 'UNIT_TAB_NOT_FOUND';

  // Check if already active: check the element and its parents for active/selected/cur classes
  var node = unitTab;
  for (var depth = 0; depth < 5 && node; depth++) {
    var cls = (node.className || '') + ' ' + (node.getAttribute('class') || '');
    if (/\\b(active|selected|cur|on|current)\\b/i.test(cls)) {
      return 'ALREADY_ACTIVE:' + unitTab.textContent.trim();
    }
    node = node.parentElement;
  }

  // Not active — click it
  var target = unitTab;
  while (target && target.tagName && target.tagName.toLowerCase() !== 'button'
         && target.tagName.toLowerCase() !== 'a'
         && target.tagName.toLowerCase() !== 'li'
         && target.tagName.toLowerCase() !== 'span') {
    target = target.parentElement;
  }
  if (target) {
    target.click();
    return 'CLICKED:' + target.tagName + ':' + unitTab.textContent.trim();
  }
  unitTab.click();
  return 'CLICKED_ELEMENT:' + unitTab.tagName + ':' + unitTab.textContent.trim();
})()
"""
    try:
        result = _run_pinchtab(["eval", js], instance_port=instance_port, timeout=15)
        output = result.stdout.strip()
        log.info(f"Ensure unit login tab: {output[:120]}")
        if "UNIT_TAB_NOT_FOUND" in output:
            log.warning("Unit login tab not found on page — may already show QR")
            return True  # Tab not found but maybe the page doesn't need it
        if "ALREADY_ACTIVE" in output:
            log.info("Unit login tab already selected")
            return True
        if "CLICKED" in output:
            log.info("Clicked unit login tab to select it")
            return True
        return True  # Default: assume OK
    except Exception as e:
        log.error(f"Ensure unit login tab error: {e}")
        return True  # Don't block on this check — proceed with screenshot


def _click_company_on_deptnext(instance_port, company, log):
    """Select company and click login on deptLoginNext page.

    Follows DST skill SKILL.md "A. deptLoginNext 流程" precisely:
      a. Extract companies via querySelectorAll('.company-name')
      b. Fuzzy-match target company against list entries
      c. Click matching .company-name div
      d. Verify: parentElement.classList.contains('active')
      e. Click 登录 button (document.getElementById('btnQyyhdl'))

    Returns True if company was selected AND login button was clicked,
    False otherwise (caller should retry on next poll iteration).
    """
    # Build keyword list for fuzzy matching: full name + progressively shorter forms
    keywords = [company]
    # Add short name (strip 有限公司/股份有限公司 etc.)
    short = company
    for suffix in ["有限", "责任", "股份", "公司", "汽车", "服务", "租赁", "新能源"]:
        short = short.replace(suffix, "")
    short = short.strip()
    if short and short not in keywords:
        keywords.append(short)
    # Also try the first meaningful word (e.g. city name)
    parts = [p for p in company.replace("有限", " ").replace("公司", " ").split() if len(p) >= 2]
    for p in parts:
        if p not in keywords:
            keywords.append(p)

    keywords_json = json.dumps(keywords, ensure_ascii=False)

    # Step 1: querySelectorAll('.company-name'), match, click, verify
    select_js = f"""(function(){{
  var keywords = {keywords_json};
  var companies = document.querySelectorAll('.company-name');
  if (!companies || companies.length === 0) {{
    return 'NO_COMPANY_ELEMENTS';
  }}
  var target = null;
  for (var k = 0; k < keywords.length && !target; k++) {{
    var kw = keywords[k];
    for (var i = 0; i < companies.length; i++) {{
      if ((companies[i].textContent || '').indexOf(kw) >= 0) {{
        target = companies[i];
        break;
      }}
    }}
  }}
  if (!target) {{
    // Gather all company names for diagnostics
    var names = [];
    for (var i = 0; i < companies.length; i++) {{
      names.push((companies[i].textContent || '').trim());
    }}
    return 'NOT_FOUND:' + JSON.stringify(names);
  }}
  target.click();
  // Verify: check parentElement.classList.contains('active')
  var verified = target.parentElement && target.parentElement.classList.contains('active');
  return 'CLICKED:' + target.textContent.trim() + '|verified=' + verified;
}})()"""

    try:
        result = _run_pinchtab(
            ["eval", select_js],
            instance_port=instance_port, timeout=15
        )
        output = (result.stdout or "").strip()
        log.info(f"Company selection: {output[:300]}")

        if output.startswith("NO_COMPANY_ELEMENTS"):
            log.warning("No .company-name elements found — page may still be loading")
            return False

        if output.startswith("NOT_FOUND:"):
            log.warning(f"Company '{company}' not in deptLoginNext list. "
                        f"Keywords tried: {keywords}. List: {output}")
            return False  # Don't click login — would select wrong company

        if not output.startswith("CLICKED:"):
            log.warning(f"Unexpected company selection result: {output[:200]}")
            return False

        log.info(f"Company clicked: {output}")
    except Exception as e:
        log.error(f"Company selection eval error: {e}")
        return False

    # Step 2: Click 登录 button (btnQyyhdl)
    login_js = """(function(){
  var btn = document.getElementById('btnQyyhdl');
  if (btn) { btn.click(); return 'LOGIN_CLICKED'; }
  // Fallback: search for login button by visible text
  var all = document.querySelectorAll('button, a, .btn, [role="button"]');
  for (var i = 0; i < all.length; i++) {
    var t = (all[i].textContent || '').trim();
    if (t === '登录' || t === '登 录' || t === '确认登录') {
      all[i].click();
      return 'LOGIN_CLICKED_FALLBACK:' + all[i].tagName;
    }
  }
  return 'LOGIN_BTN_NOT_FOUND';
})()"""
    try:
        result = _run_pinchtab(
            ["eval", login_js],
            instance_port=instance_port, timeout=15
        )
        output = (result.stdout or "").strip()
        log.info(f"Login button: {output[:120]}")
        if "LOGIN_CLICKED" in output:
            time.sleep(PAGE_LOAD_WAIT)
            return True
        else:
            log.warning(f"Login button not found after company selection: {output}")
            return False
    except Exception as e:
        log.error(f"Login button click error: {e}")
        return False


def _poll_until_logged_in(instance_port, timeout_seconds, poll_interval, log,
                          shutdown_flag=None, health_file=None, tab_id=None,
                          cycle_count=0, company=None):
    """Poll page state until logged in or timeout. Uses keyword match on
    post-login indicators (公司列表, 退出, 车辆管理 etc.) — this is the
    intended use of text matching: detecting the transition after QR scan.

    When a company list page is detected, attempts to click the target
    company automatically before continuing to poll.

    Sends WATCHDOG=1 each iteration so the daemon won't be killed by
    systemd watchdog timeout during the potentially 5-minute poll window.
    Also updates health_file (if provided) for external pull-mode watchdog.

    Returns True if logged in, False on timeout or shutdown signal."""
    deadline = time.time() + timeout_seconds
    company_clicked = False
    while time.time() < deadline:
        if shutdown_flag and shutdown_flag.get("triggered"):
            log.info("Shutdown signaled during QR poll — aborting.")
            return False
        try:
            text_result = _run_pinchtab(["text"], instance_port=instance_port, timeout=15)
            page_text = text_result.stdout

            if "退出" in page_text:
                log.info("Login detected: '退出' button found")
                return True
            if any(kw in page_text for kw in ["车辆管理", "租赁车辆", "业务办理", "违法查询"]):
                log.info(f"Login detected: business menus found")
                return True
            # Company list page (deptLoginNext post-QR landing)
            # ── Aligned with DST skill SKILL.md "A. deptLoginNext 流程" ──
            # Uses querySelectorAll('.company-name') for precise selection,
            # verifies active state, then clicks 登录 button.
            if any(kw in page_text for kw in ["公司列表", "公司名称", "请选择", "选择单位"]):
                if company and not company_clicked:
                    log.info(f"Login detected: deptLoginNext page, selecting company '{company}'...")
                    company_clicked = _click_company_on_deptnext(
                        instance_port, company, log
                    )
                elif not company:
                    log.info("Login detected: deptLoginNext page (no company to auto-select)")
        except Exception as e:
            log.warning(f"Poll check error: {e}")

        remaining = int(deadline - time.time())
        log.info(f"Waiting for scan... ({remaining}s remaining)")
        # Keep systemd watchdog happy while we wait — each poll iteration
        # could be the last thing we do before systemd's WatchdogSec expires.
        _sd_notify("WATCHDOG=1", log)
        if health_file and tab_id:
            _touch_health(health_file, "recovering", tab_id, cycle_count, instance_port, log)
        time.sleep(poll_interval)

    log.warning(f"Poll timeout after {timeout_seconds}s")
    return False


def _get_data_dir(project_root):
    """Get the data directory for keepalive files."""
    data_dir = os.path.join(project_root, "violation_query", "data")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def _auto_recover_login(company, instance_port, platform_url, project_root, log,
                        notify=None, shutdown_flag=None, health_file=None,
                        tab_id=None, cycle_count=0):
    """Attempt to recover login via QR code when session expires.

    At most ONE recovery attempt per invocation. Within that attempt,
    up to MAX_QR_REFRESHES (3) QR codes are sent if they expire.

    notify is a dict {"type": "user"|"chat", "id": "<id>", "label": "<name>"}
    from _resolve_notify_target().  If None or missing id, QR is saved to
    disk only (no Lark notification).

    Flow:
    1. Navigate to UNIT_LOGIN_URL (https://gab.122.gov.cn/m/login?t=2)
    2. Confirm '单位用户登录' tab is selected (personal/unit tabs share QR area)
    3. Screenshot QR code
    4. Send to Lark (if notify configured): user→P2P, chat→group
    5. Poll for login success (5 min timeout per QR)
    6. On QR expiry: reload page → new QR → re-send (up to 3 total)
    7. On success: navigate to platform_url, mark is_logged_in=1,
       record notified person in log.
    8. On all QR attempts exhausted: return False

    Returns True if login was recovered, False otherwise.
    """
    max_qr_sends = MAX_QR_REFRESHES

    log.info(f"=== Auto-recovery: starting QR re-login (max {max_qr_sends} QR sends) ===")

    for qr_attempt in range(1, max_qr_sends + 1):
        log.info(f"QR send {qr_attempt}/{max_qr_sends}")

        try:
            # Step 1: Navigate to unit login page
            log.info("Navigating to unit login page...")
            nav_result = _run_pinchtab(
                ["nav", UNIT_LOGIN_URL],
                instance_port=instance_port, timeout=30
            )
            if nav_result.returncode != 0:
                stderr_text = (nav_result.stderr or "").lower()
                if "tab" in stderr_text and ("not found" in stderr_text or "404" in stderr_text):
                    log.warning("Tab not found during auto-recovery — creating fresh tab")
                    new_tab_id = _create_keepalive_tab(instance_port, UNIT_LOGIN_URL,
                                                       project_root, company, log)
                    if new_tab_id:
                        os.environ["VIOLATION_TAB_ID"] = new_tab_id
                        tab_file = os.path.join(_get_data_dir(project_root),
                                                f"keepalive_tab_{_safe_name(company)}.txt")
                        _save_tab_id(tab_file, new_tab_id)
                        log.info("Retrying navigation with fresh tab...")
                        nav_result = _run_pinchtab(
                            ["nav", UNIT_LOGIN_URL],
                            instance_port=instance_port, timeout=30
                        )
                        if nav_result.returncode != 0:
                            log.error(f"Navigation still failed after fresh tab: {nav_result.stderr[:200]}")
                            time.sleep(10)
                            continue
                    else:
                        log.error("Failed to create fresh tab")
                        time.sleep(10)
                        continue
                else:
                    log.error(f"Navigation failed: {nav_result.stderr[:200]}")
                    time.sleep(10)
                    continue
            time.sleep(PAGE_LOAD_WAIT)

            # Step 2: Confirm unit login tab is selected (personal/unit share same QR page)
            log.info("Ensuring '单位用户登录' tab is selected...")
            _ensure_unit_login_tab(instance_port, log)
            time.sleep(PAGE_LOAD_WAIT)

            # Step 3: Take QR screenshot
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            data_dir = _get_data_dir(project_root)
            qr_file = os.path.join(
                data_dir,
                f"recovery_qr_{_safe_name(company)}_{timestamp}.png"
            )

            # Remove old recovery QR files for this company (QR is single-use)
            try:
                import glob
                old_pattern = os.path.join(data_dir, f"recovery_qr_{_safe_name(company)}_*.png")
                for old in glob.glob(old_pattern):
                    try:
                        os.remove(old)
                    except OSError:
                        pass
            except Exception:
                pass

            screenshot_ok = _take_qr_screenshot(instance_port, qr_file, log)

            # Step 4: Send to Lark if notify target is configured
            notify_type = notify.get("type") if notify else None
            notify_id = notify.get("id") if notify else None
            notify_label = notify.get("label", "") if notify else ""

            if notify_id and screenshot_ok:
                try:
                    log.info("Uploading QR image to Lark...")
                    upload_file = os.path.basename(qr_file)
                    upload_result = _run_lark(
                        ["im", "images", "create", "--as", "bot",
                         "--file", f"image=./{upload_file}",
                         "--data", '{"image_type":"message"}'],
                        timeout=20,
                        cwd=data_dir
                    )

                    image_key = ""
                    try:
                        d = json.loads(upload_result.stdout)
                        image_key = d.get("data", {}).get("image_key", "") or d.get("image_key", "")
                    except (json.JSONDecodeError, ValueError):
                        pass

                    if image_key:
                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                        at_user_id = notify.get("at_user_id", "") if notify else ""
                        at_user_name = notify.get("at_user_name", "") if notify else ""

                        # Build post content matching query-flow gen-qr-msg format
                        header_text = (
                            f"⚠️ 保活程序检测到登录已过期\n"
                            f"\U0001f3e2 公司：{company}\n"
                            f"\U0001f550 时间：{now_str}\n"
                            f"\U0001f504 恢复尝试：第 {qr_attempt}/{max_qr_sends} 次\n\n"
                            f"\U0001f4f1 请使用「交管12123」APP 扫描下方二维码重新登录\n\n"
                        )
                        steps_text = (
                            f"\n\U0001f4dd 登录步骤：\n"
                            f"① 打开交管12123 APP\n"
                            f"② 扫一扫上方二维码\n"
                            f"③ 完成人脸识别\n"
                            f"④ 登录成功后系统将自动恢复保活"
                        )

                        if notify_type == "chat" and at_user_id:
                            # Group chat with @mention — same format as gen-qr-msg
                            at_block = {"tag": "at", "user_id": at_user_id,
                                        "user_name": at_user_name}
                            content = [
                                [at_block,
                                 {"tag": "text", "text": f" 请扫码重新登录12123\n\n{header_text}"}],
                                [{"tag": "img", "image_key": image_key}],
                                [{"tag": "text", "text": steps_text}],
                            ]
                        elif notify_type == "chat":
                            # Group chat without @mention target
                            content = [
                                [{"tag": "text", "text": header_text}],
                                [{"tag": "img", "image_key": image_key}],
                                [{"tag": "text", "text": steps_text}],
                            ]
                        else:
                            # P2P: no @ needed, personalized header
                            p2p_header = f"\U0001f4e3 请 {notify_label} 扫码重新登录\n\n{header_text}"
                            content = [
                                [{"tag": "text", "text": p2p_header}],
                                [{"tag": "img", "image_key": image_key}],
                                [{"tag": "text", "text": steps_text}],
                            ]

                        recovery_msg = json.dumps({
                            "zh_cn": {
                                "title": "\U0001f504 12123登录已过期 - 需要重新扫码",
                                "content": content,
                            }
                        }, ensure_ascii=False)

                        # Send: user → P2P via --user-id, chat → group via --chat-id
                        send_args = ["im", "+messages-send", "--as", "bot",
                                     "--msg-type", "post", "--content", recovery_msg]
                        if notify_type == "user":
                            send_args += ["--user-id", notify_id]
                        else:
                            send_args += ["--chat-id", notify_id]

                        _run_lark(send_args, timeout=20)
                        target_desc = f"{notify_type}:{notify_label}({notify_id[:8]}…)"
                        log.info("Recovery QR sent to %s", target_desc)
                    else:
                        log.warning("Image upload succeeded but no image_key returned")
                except Exception as e:
                    log.warning("Lark notification failed (non-fatal): %s", e)
            elif not notify_id and screenshot_ok:
                log.info("No notify target configured — QR saved to disk only: %s", qr_file)

            # Step 5: Poll for login
            log.info(f"Polling for login (timeout={RECOVERY_TIMEOUT}s, interval={RECOVERY_POLL_INTERVAL}s)...")
            logged_in = _poll_until_logged_in(
                instance_port, RECOVERY_TIMEOUT, RECOVERY_POLL_INTERVAL, log,
                shutdown_flag=shutdown_flag, health_file=health_file,
                tab_id=tab_id, cycle_count=cycle_count, company=company
            )

            if logged_in:
                # Step 6: Success! Navigate to platform URL and mark logged in
                who = notify_label if notify_label else "unknown (no notify configured)"
                log.info("Login recovered successfully! Scanned by: %s", who)
                db_path = _get_db_path(project_root)
                _set_logged_in(db_path, company, instance_port)
                if platform_url:
                    _run_pinchtab(["nav", platform_url], instance_port=instance_port, timeout=30)
                    time.sleep(PAGE_LOAD_WAIT)
                return True
            else:
                log.warning(f"QR {qr_attempt} timed out (not scanned in {RECOVERY_TIMEOUT}s)")
                # Clean up old QR file
                try:
                    os.remove(qr_file)
                except OSError:
                    pass

        except Exception as e:
            log.error(f"Recovery QR {qr_attempt} exception: {e}")
            time.sleep(10)

    log.error(f"Auto-recovery failed after {max_qr_sends} QR sends")
    return False


def _run_daemon(company, project_root, auto_recover=False,
                notify_user=None, notify_phone=None, notify_chat=None,
                lark_chat_id=None):
    """Run the keepalive daemon loop. Does not return until exit signal.

    notify_user/notify_phone/notify_chat are resolved to a notify dict
    at startup via Lark API.  lark_chat_id (raw chat ID) is kept for
    backward compatibility and takes priority as a chat target."""
    db_path = _get_db_path(project_root)
    pid_file = _get_pid_file(project_root, company)
    log_file = _get_log_file(project_root, company)
    tab_file = _get_tab_file(project_root, company)
    health_file = _get_health_file(project_root, company)

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )
    log = logging.getLogger("keepalive")
    log.info(f"Keepalive daemon starting: company={company}, project_root={project_root}")
    log.info(f"PID file: {pid_file}")

    # Check for existing daemon
    running, existing_pid = _check_running(pid_file)
    if running:
        log.error(f"Daemon already running with PID {existing_pid}. Refusing to start.")
        print(json.dumps({"ok": False, "error": f"already running pid={existing_pid}"}))
        sys.exit(EXIT_ALREADY_RUNNING)

    # Write PID file
    _write_pid(pid_file)

    # Signal handlers for graceful shutdown
    shutdown_flag = {"triggered": False}

    def _on_signal(signum, frame):
        log.info(f"Received signal {signum}, shutting down...")
        shutdown_flag["triggered"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGABRT, _on_signal)

    # ── resolve notify target ──
    # Resolve human-readable notify specs (name / phone / chat) to Lark IDs
    # at startup so we know exactly who to notify during auto-recovery.
    # lark_chat_id (raw) takes priority as a chat target for backward compat.
    notify = {"type": None, "id": None, "label": None}
    if lark_chat_id:
        notify = {"type": "chat", "id": lark_chat_id, "label": lark_chat_id}
        log.info("Notify target: raw chat_id=%s", lark_chat_id)
    elif notify_user or notify_phone or notify_chat:
        notify = _resolve_notify_target(
            name=notify_user, phone=notify_phone, chat=notify_chat, log=log
        )
        if notify["id"]:
            log.info("Notify target resolved: %s=%s (%s…)",
                     notify["type"], notify["label"], notify["id"][:8])
            # Persist for future restarts (e.g. after machine reboot)
            _save_notify(project_root, company, notify, log)
        else:
            log.warning("Notify target resolution failed — QR will be saved "
                        "to disk only during auto-recovery")
    else:
        # No CLI args — try persisted config from a previous query session
        persisted = _load_notify(project_root, company, log)
        if persisted:
            notify = persisted
        else:
            log.info("No notify target configured (no CLI args, no persisted config). "
                     "Auto-recovery QR will be saved to disk only.")

    cycle_count = 0

    # Verify login state at startup
    profile = _read_profile(db_path, company)
    if profile is None:
        log.error(f"Company '{company}' not found in profiles table.")
        _remove_pid(pid_file)
        print(json.dumps({"ok": False, "error": "company not found in profiles"}))
        sys.exit(1)

    # Notify systemd we're ready BEFORE auto-recovery.
    # Default TimeoutStartSec=90s kills the process if READY=1 isn't sent
    # in time, but auto-recovery (3 QR × 5 min each) can take up to 15 min.
    # After READY=1, WatchdogSec=240 takes over; poll loop feeds WATCHDOG=1
    # every 10s, and the longest gap between QR attempts is ~100s < 240s.
    _sd_notify("READY=1", log)

    if not profile["is_logged_in"]:
        if auto_recover:
            log.warning(f"Company '{company}' is_logged_in=0. "
                        f"Auto-recover enabled — attempting QR re-login...")
            instance_port = profile.get("instance_port")
            platform_url = profile.get("platform_url", "")

            # Need a tab for recovery — create one if not persisted
            tab_id = _load_tab_id(tab_file)
            if not tab_id or not _verify_tab(tab_id, instance_port, log):
                tab_id = _create_keepalive_tab(instance_port, platform_url or UNIT_LOGIN_URL,
                                               project_root, company, log)
                if tab_id:
                    _save_tab_id(tab_file, tab_id)

            if tab_id:
                # Ensure VIOLATION_TAB_ID is set so _run_pinchtab targets this tab.
                # _create_keepalive_tab sets it, but when reusing an existing tab
                # (line 1375 passes verification), it's never set.
                os.environ["VIOLATION_TAB_ID"] = tab_id
                recovered = _auto_recover_login(
                    company, instance_port, platform_url, project_root, log,
                    notify=notify, shutdown_flag=shutdown_flag,
                    health_file=health_file, tab_id=tab_id, cycle_count=cycle_count
                )
                if recovered:
                    log.info("Startup recovery successful! Proceeding with normal keepalive.")
                    profile = _read_profile(db_path, company)  # re-read updated profile
                    if not profile or not profile.get("platform_url"):
                        log.error("Profile missing platform_url after recovery. "
                                  "Run: profile-register --company '...' --platform-url 'https://xx.122.gov.cn'")
                        _remove_pid(pid_file)
                        sys.exit(EXIT_LOGGED_OUT)
                        _remove_pid(pid_file)
                        sys.exit(EXIT_LOGGED_OUT)
                    # Fall through to normal keepalive loop below
                else:
                    log.error("Startup auto-recovery failed — exiting.")
                    _remove_pid(pid_file)
                    sys.exit(EXIT_LOGGED_OUT)
            else:
                log.error("Cannot create tab for recovery — exiting.")
                _remove_pid(pid_file)
                sys.exit(EXIT_LOGGED_OUT)
        else:
            log.error(f"Company '{company}' is_logged_in=0. Nothing to keep alive.")
            _remove_pid(pid_file)
            print(json.dumps({"ok": False, "error": "is_logged_in is already 0"}))
            sys.exit(EXIT_LOGGED_OUT)

    instance_port = profile.get("instance_port")
    platform_url = profile.get("platform_url", "")
    log.info(f"Profile: is_logged_in=1, platform={platform_url}, "
             f"instance_port={instance_port or 'default'}, profile={profile.get('profile_name', '?')}")

    # Persist cookies on startup so they survive any Chrome restart
    _persist_cookies(_get_profile_dir(profile.get("profile_name")), log)

    # ── resolve keepalive tab ──
    tab_id = _load_tab_id(tab_file)

    if tab_id:
        # Existing tab — verify it's still alive (uses --tab targeting, no global switch)
        log.info(f"Found persisted tab {tab_id}, verifying...")
        if _verify_tab(tab_id, instance_port, log):
            log.info(f"Reusing existing keepalive tab {tab_id}")
        else:
            log.warning(f"Persisted tab {tab_id} is stale, creating new tab")
            tab_id = None
            _save_tab_id(tab_file, "")  # clear stale

    if not tab_id:
        if not platform_url:
            log.error("No platform_url in profile and no existing tab — cannot create keepalive tab.")
            # NOTE: do NOT set is_logged_in=0 here — this is a config issue, not auth failure.
            _remove_pid(pid_file)
            print(json.dumps({"ok": False, "error": "no platform_url configured"}))
            sys.exit(1)
        tab_id = _create_keepalive_tab(instance_port, platform_url, project_root, company, log)
        if not tab_id:
            log.critical("Failed to create keepalive tab.")
            # NOTE: do NOT set is_logged_in=0 here — this could be pinchtab not found,
            # Chrome crash, etc. The session cookies may still be valid.
            _remove_pid(pid_file)
            print(json.dumps({"ok": False, "error": "failed to create tab"}))
            sys.exit(1)
        _save_tab_id(tab_file, tab_id)
        log.info(f"Created and persisted new keepalive tab {tab_id}")

    # ── Set VIOLATION_TAB_ID for --tab injection ──
    # All subsequent _run_pinchtab() calls will auto-inject --tab <id>,
    # targeting the keepalive tab without switching the global active tab.
    os.environ["VIOLATION_TAB_ID"] = tab_id

    consecutive_failures = 0
    consecutive_unknown = 0
    cycle_count = 0
    recovery_used = False  # only ONE auto-recovery opportunity per keepalive session
    exit_code = EXIT_OK    # track exit reason for systemd RestartPreventExitStatus

    while not shutdown_flag["triggered"]:
        cycle_count += 1
        cycle_start = time.time()
        # Notify systemd watchdog immediately at cycle start so the
        # nav + dismiss + check (20-30s of pinchtab calls)
        # can't push us past WatchdogSec if the last heartbeat was >90s ago.
        _sd_notify("WATCHDOG=1", log)
        _touch_health(health_file, "logged_in", tab_id, cycle_count, instance_port, log)
        log.info(f"=== Keepalive cycle #{cycle_count} (tab={tab_id}) ===")

        # ── pre-flight: check is_logged_in ──
        profile = _read_profile(db_path, company)
        if profile is None or not profile["is_logged_in"]:
            log.info("is_logged_in is 0 or profile missing — exiting.")
            exit_code = EXIT_LOGGED_OUT
            break

        # Update instance_port in case it changed (e.g. PinchTab restarted on new port)
        instance_port = profile.get("instance_port")

        # ── step 0: verify keepalive tab is still alive ──
        if not _verify_tab(tab_id, instance_port, log):
            log.warning("Tab verify failed — tab may have been closed. Creating new tab.")
            tab_id = _create_keepalive_tab(instance_port, platform_url, project_root, company, log)
            if not tab_id:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.critical("Cannot create/recover tab after failures — exiting.")
                    _set_logged_out(db_path, company)
                    exit_code = EXIT_LOGGED_OUT
                    break
                # Sharded sleep with watchdog feed — prevent systemd kill
                for _ in range(12):  # 12 × 5s = 60s
                    if shutdown_flag["triggered"]:
                        break
                    time.sleep(5)
                    _sd_notify("WATCHDOG=1", log)
                    _touch_health(health_file, "logged_in", tab_id, cycle_count, instance_port, log)
                continue
            _save_tab_id(tab_file, tab_id)

        # ── step 1: navigate to 我的主页 (vehlist) ──
        # Direct navigation ensures we always land on the vehicle list page
        # regardless of where the tab was previously (root, recovery page, etc.).
        vehlist_url = platform_url.rstrip("/") + "/views/memrent/vehlist.html"
        try:
            nav_result = _run_pinchtab(["nav", vehlist_url], instance_port=instance_port, timeout=30)
            if nav_result.returncode != 0:
                consecutive_failures += 1
                log.error(f"Nav to vehlist failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): "
                          f"{nav_result.stderr[:200]}")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.critical("Too many consecutive nav failures — exiting.")
                    _set_logged_out(db_path, company)
                    exit_code = EXIT_LOGGED_OUT
                    break
                # Sharded sleep with watchdog feed — prevent systemd kill
                for _ in range(12):  # 12 × 5s = 60s
                    if shutdown_flag["triggered"]:
                        break
                    time.sleep(5)
                    _sd_notify("WATCHDOG=1", log)
                    _touch_health(health_file, "logged_in", tab_id, cycle_count, instance_port, log)
                continue
            consecutive_failures = 0
            time.sleep(PAGE_LOAD_WAIT)
        except Exception as e:
            consecutive_failures += 1
            log.error(f"Nav exception: {e} ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.critical("Too many consecutive nav failures — exiting.")
                _set_logged_out(db_path, company)
                exit_code = EXIT_LOGGED_OUT
                break
            # Sharded sleep with watchdog feed — prevent systemd kill
            for _ in range(12):  # 12 × 5s = 60s
                if shutdown_flag["triggered"]:
                    break
                time.sleep(5)
                _sd_notify("WATCHDOG=1", log)
                _touch_health(health_file, "logged_in", tab_id, cycle_count, instance_port, log)
            continue

        # ── step 2: dismiss popups ──
        _dismiss_popup(instance_port, log)

        # ── step 3: verify session (URL must be on vehlist page) ──
        state, detail = _check_page_state(instance_port, platform_url, log)

        if state == "rate_limited":
            log.critical(f"Rate-limited! {detail}")
            _set_logged_out(db_path, company)
            _touch_health(health_file, "rate_limited", tab_id, cycle_count, instance_port, log)
            exit_code = EXIT_RATE_LIMITED
            break

        elif state == "login_expired":
            log.warning(f"Login expired: {detail}")
            _set_logged_out(db_path, company)
            _touch_health(health_file, "login_expired", tab_id, cycle_count, instance_port, log)
            if auto_recover and not recovery_used:
                recovery_used = True
                log.info("Auto-recover: attempting ONE-TIME QR re-login...")
                recovered = _auto_recover_login(
                    company, instance_port, platform_url, project_root, log,
                    notify=notify, shutdown_flag=shutdown_flag,
                    health_file=health_file, tab_id=tab_id, cycle_count=cycle_count
                )
                if recovered:
                    log.info("Recovery successful — resuming keepalive cycle")
                    consecutive_failures = 0
                    recovery_used = True  # already used the one opportunity
                    # Persist cookies immediately after recovery
                    _persist_cookies(
                        _get_profile_dir(profile.get("profile_name")), log
                    )
                    continue
                else:
                    log.critical("Auto-recovery failed — exiting.")
            exit_code = EXIT_LOGGED_OUT
            break

        elif state == "logged_in":
            log.info(f"Page state OK: {detail}")
            consecutive_unknown = 0
            # Persist cookies every cycle so session cookies survive Chrome restart
            _persist_cookies(
                _get_profile_dir(profile.get("profile_name")), log
            )

        else:
            # "unknown" — transient CDP failure (timeout, instance busy).
            # Skip this cycle, but if it happens too many times in a row,
            # the CDP/instance is likely broken and we should exit.
            consecutive_unknown += 1
            log.warning(f"Transient page state check failure ({consecutive_unknown}/{MAX_CONSECUTIVE_UNKNOWN}): {detail}")
            if consecutive_unknown >= MAX_CONSECUTIVE_UNKNOWN:
                log.critical("Too many consecutive unknown states — CDP likely broken, exiting.")
                exit_code = EXIT_LOGGED_OUT
                break

        # ── step 4: sleep + interleaved heartbeats until next cycle ──
        elapsed = time.time() - cycle_start
        sleep_time = max(10, INTERVAL_SECONDS - elapsed)

        # Touch health file + notify systemd watchdog after each cycle
        _touch_health(health_file, state, tab_id, cycle_count, instance_port, log)
        # N2: write session state for query/keepalive coordination (informational only)
        _write_session_state(project_root, company, instance_port, tab_id, cycle_count,
                             state == "logged_in", log)
        _sd_notify("WATCHDOG=1", log)

        log.info(f"Cycle {cycle_count} done ({elapsed:.0f}s). "
                 f"Next reload in {sleep_time / 60:.1f} min. "
                 f"Heartbeat every {HEARTBEAT_MIN_SEC}-{HEARTBEAT_MAX_SEC}s.")

        # Determine next heartbeat delay (random within [min, max])
        next_heartbeat = random.randint(HEARTBEAT_MIN_SEC, HEARTBEAT_MAX_SEC)
        heartbeat_fail_streak = 0
        heartbeat_count = 0

        # Watchdog notification timer — must fire before systemd WatchdogSec=120
        # Independent of heartbeat success/failure so a stuck page doesn't
        # cause systemd to SIGABRT a perfectly healthy daemon loop.
        WATCHDOG_INTERVAL = 60  # half of WatchdogSec, fire-and-forget margin
        next_watchdog = WATCHDOG_INTERVAL

        while sleep_time > 0 and not shutdown_flag["triggered"]:
            # Cap sleep at 30s per chunk so a stuck sleep() doesn't
            # freeze the daemon indefinitely.  At worst we lose 30s.
            # Include watchdog timer in the min() so we always wake in time.
            chunk = min(30, next_heartbeat, next_watchdog, sleep_time)
            # Safety floor: chunk must be >= 1s to prevent zero-sleep tight loop
            # that would burn 100% CPU.  Worst case this adds 1s latency to
            # heartbeat / watchdog timers, which is negligible.
            if chunk < 1:
                chunk = 1
            time.sleep(chunk)
            sleep_time -= chunk
            next_heartbeat -= chunk
            next_watchdog -= chunk

            # ── systemd watchdog keepalive (fires every 60s) ──
            if next_watchdog <= 0:
                _sd_notify("WATCHDOG=1", log)
                _touch_health(health_file, state, tab_id, cycle_count, instance_port, log)
                next_watchdog = WATCHDOG_INTERVAL

            if next_heartbeat <= 0:
                if sleep_time > 10 and state == "logged_in":
                    # Perform a light heartbeat: random scroll + ping
                    heartbeat_count += 1
                    hb_start = time.time()
                    ok = _heartbeat(instance_port, log)
                    hb_elapsed = time.time() - hb_start

                    if ok:
                        heartbeat_fail_streak = 0
                        # Notify systemd watchdog on successful heartbeats too
                        _sd_notify("WATCHDOG=1", log)
                        # Update health file on heartbeats so stall detection is
                        # granular (60-120s) instead of only at cycle boundaries (18min).
                        _touch_health(health_file, state, tab_id, cycle_count, instance_port, log)
                        # Log first heartbeat + every ~5th to avoid noise
                        if heartbeat_count == 1 or heartbeat_count % 5 == 0:
                            log.info(f"Heartbeat #{heartbeat_count} OK "
                                     f"(scroll in {hb_elapsed:.1f}s)")
                    else:
                        heartbeat_fail_streak += 1
                        log.warning(f"Heartbeat #{heartbeat_count} FAILED "
                                    f"({heartbeat_fail_streak}/"
                                    f"{MAX_CONSECUTIVE_HEARTBEAT_FAILS})")
                        if heartbeat_fail_streak >= MAX_CONSECUTIVE_HEARTBEAT_FAILS:
                            log.error("Too many consecutive heartbeat failures "
                                      "— page may be stalled. Triggering early reload.")
                            break  # break inner sleep loop → trigger next full reload cycle
                else:
                    # State is not "logged_in" (e.g. transient CDP failure).
                    # Reset heartbeat timer to avoid a tight loop:
                    # when next_heartbeat stays at 0, chunk=min(30,0,...)=0
                    # → time.sleep(0) spins forever at 100% CPU.
                    pass
                # Always reschedule next heartbeat to prevent zero-chunk tight loop.
                next_heartbeat = random.randint(HEARTBEAT_MIN_SEC, HEARTBEAT_MAX_SEC)

    # ── cleanup ──
    log.info(f"Keepalive daemon exiting (code={exit_code}).")

    # Close stale login-page tabs on this instance to prevent tab accumulation.
    # Keep the platform-page tab (fj/sc/gd.122.gov.cn/*) — closing it would
    # lose the session (verified: 12123 session breaks when all tabs closed).
    _cleanup_stale_tabs(instance_port, tab_id, log)

    # Clean up tab_registry.json entry so stale entries don't accumulate
    _cleanup_tab_registry(project_root, company, log)

    _touch_health(health_file, "exited", tab_id, cycle_count, instance_port, log)
    _remove_pid(pid_file)
    # Note: we do NOT remove the tab_file — the tab persists in Chrome
    # and can be reused if the daemon is restarted.
    sys.exit(exit_code)


# ── status command ─────────────────────────────────────────────

def _cmd_status(company, project_root):
    """Print daemon status as JSON."""
    pid_file = _get_pid_file(project_root, company)
    db_path = _get_db_path(project_root, company)
    log_file = _get_log_file(project_root, company)
    tab_file = _get_tab_file(project_root, company)

    running, pid = _check_running(pid_file)

    profile = _read_profile(db_path, company)
    is_logged_in = profile["is_logged_in"] if profile else False

    tab_id = _load_tab_id(tab_file)

    last_log_lines = []
    if os.path.exists(log_file):
        try:
            with open(log_file, encoding="utf-8") as f:
                lines = f.readlines()
                last_log_lines = [l.strip() for l in lines[-5:]]
        except Exception:
            pass

    status = {
        "running": running,
        "pid": pid,
        "company": company,
        "is_logged_in": is_logged_in,
        "tab_id": tab_id,
        "instance_port": profile.get("instance_port") if profile else None,
        "platform_url": profile.get("platform_url") if profile else None,
        "pid_file": pid_file,
        "log_file": log_file,
        "last_log": last_log_lines,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))


# ── stop command ───────────────────────────────────────────────

def _cmd_stop(company, project_root):
    """Stop a running daemon."""
    pid_file = _get_pid_file(project_root, company)
    running, pid = _check_running(pid_file)

    if not running:
        print(json.dumps({"ok": False, "error": "daemon not running"}, ensure_ascii=False))
        _remove_pid(pid_file)
        sys.exit(1)

    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            time.sleep(1)
            still_running, _ = _check_running(pid_file)
            if not still_running:
                print(json.dumps({"ok": True, "stopped": True, "pid": pid}, ensure_ascii=False))
                sys.exit(0)
        os.kill(pid, signal.SIGKILL)
        _remove_pid(pid_file)
        print(json.dumps({"ok": True, "stopped": True, "pid": pid, "forced": True}, ensure_ascii=False))
        sys.exit(0)
    except OSError as e:
        _remove_pid(pid_file)
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)


# ── entry point ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="12123 Keepalive Daemon — 独立保活守护进程（每公司一个实例）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  启动保活（带自动恢复，推荐）:
    nohup python3 keepalive_daemon.py --company "北京安桉" --project-root /home/user/project --auto-recover &
    disown

  启动保活（systemd 服务，推荐生产环境）:
    systemctl --user start keepalive@北京安桉

  查看状态:
    python3 keepalive_daemon.py --company "北京安桉" --project-root /home/user/project --status

  停止保活:
    python3 keepalive_daemon.py --company "北京安桉" --project-root /home/user/project --stop

多公司同时保活:
    nohup python3 keepalive_daemon.py --company "北京安桉" --project-root /home/user/project --auto-recover &
    nohup python3 keepalive_daemon.py --company "成都某某" --project-root /home/user/project --auto-recover &
    disown -a
        """,
    )
    parser.add_argument("--company", required=True, help="公司名称（必填，每个公司一个守护进程）")
    parser.add_argument("--project-root", required=True, help="项目根目录（必填）")
    parser.add_argument("--auto-recover", action="store_true",
                        help="启用自动恢复：检测到登录过期时触发一次 QR 重新登录（最多 3 次二维码发送）")
    parser.add_argument("--notify-user",
                        help="通知对象姓名（自动恢复时通过飞书搜索并发送QR码给此人，需 --auto-recover）")
    parser.add_argument("--notify-phone",
                        help="通知对象手机号（自动恢复时通过飞书查找并发送QR码给此人，需 --auto-recover）")
    parser.add_argument("--notify-chat",
                        help="通知群名（自动恢复时搜索飞书群并发送QR码到此群，需 --auto-recover）")
    parser.add_argument("--lark-chat-id",
                        help="飞书群聊原始ID，直接发送无需搜索（向后兼容，优先于 --notify-*）")
    parser.add_argument("--status", action="store_true", help="查看保活状态")
    parser.add_argument("--stop", action="store_true", help="停止保活守护进程")
    args = parser.parse_args()

    if args.status:
        _cmd_status(args.company, args.project_root)
    elif args.stop:
        _cmd_stop(args.company, args.project_root)
    else:
        _run_daemon(args.company, args.project_root,
                    auto_recover=args.auto_recover,
                    notify_user=args.notify_user,
                    notify_phone=args.notify_phone,
                    notify_chat=args.notify_chat,
                    lark_chat_id=args.lark_chat_id)


if __name__ == "__main__":
    main()

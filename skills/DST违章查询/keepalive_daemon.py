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
  3. 每 18 分钟执行一个保活周期：reload + dismiss popup
  4. 每个周期开始前检查 is_logged_in，若变为 0 则自动退出
  5. reload 连续失败 → profile-logout → 退出
  6. 检测到登录页或风控关键词 → profile-logout → 退出（若启用 auto-recover 则尝试一次 QR 恢复）
  7. 收到 SIGTERM/SIGINT → 清理 PID 文件后退出

自动恢复策略（--auto-recover）:
  - 每次保活会话最多触发 **一次** 自动恢复
  - 那次恢复内最多发送 **3 次** QR 码（应对二维码过期自动刷新）
  - 3 次 QR 均超时或恢复失败 → 静默退出，等待下次查询任务自然触发重新登录

多公司隔离:
  - PID 文件: 违章查询/data/keepalive_<公司>.pid
  - 日志文件: 违章查询/data/keepalive_<公司>.log
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

# ── constants ──────────────────────────────────────────────────
INTERVAL_SECONDS = 18 * 60          # 18 minutes (full page reload cycle)
HEARTBEAT_MIN_SEC = 60               # min seconds between light heartbeats
HEARTBEAT_MAX_SEC = 120              # max seconds between light heartbeats
PAGE_LOAD_WAIT = 5                   # wait after reload
POPUP_DISMISS_WAIT = 3               # wait after dismiss
MAX_CONSECUTIVE_FAILURES = 3         # consecutive reload failures → exit
MAX_CONSECUTIVE_HEARTBEAT_FAILS = 5  # consecutive heartbeat fails → treat as potential stall
MAX_RECOVERY_ATTEMPTS = 1            # one recovery opportunity per keepalive session
MAX_QR_REFRESHES = 3                 # within the one recovery, up to 3 QR sends (过期刷新)
RECOVERY_POLL_INTERVAL = 10          # seconds between login checks during recovery
RECOVERY_TIMEOUT = 300               # 5 minutes per QR code before refresh
LOGIN_PAGE_WAIT = 3                  # wait after clicking login button before QR appears

UNIT_LOGIN_URL = "https://gab.122.gov.cn/m/login?t=2"

# Paths for external tools (resolved at runtime)
_HELPER_PATH = "/tmp/violation_helper.py"
_COOKIE_PERSIST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "cookie_persist.py")
_LARK_CLI = "lark-cli"

RATE_LIMIT_KEYWORDS = [
    "频繁", "异常操作", "黑名单", "第三方软件",
    "访问受限", "操作过于频繁", "已被限制",
]

LOGIN_PAGE_INDICATORS = [
    "单位用户登录", "个人用户登录", "扫码登录",
    "请使用交管12123", "请打开交管12123",
]

# ── helpers ────────────────────────────────────────────────────

def _resolve_pinchtab():
    """Find pinchtab binary on PATH."""
    for name in ["pinchtab", "pinchtab.exe"]:
        for d in os.environ.get("PATH", "").split(os.pathsep):
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return "pinchtab"


def _run_pinchtab(args, instance_port=None, timeout=30):
    """Run pinchtab command, return CompletedProcess.
    If instance_port is set, uses PINCHTAB_PORT env var to target
    the correct PinchTab daemon instance."""
    cmd = [_resolve_pinchtab()] + args
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if instance_port:
        env["PINCHTAB_PORT"] = str(instance_port)
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=timeout
    )


def _get_db_path(project_root, company=None):
    # Use the same DB path as violation_helper.py (data dir under project root)
    data_dir = os.path.join(project_root, "data")
    if not os.path.exists(os.path.join(data_dir, "violations.db")):
        # Fallback to old path
        data_dir = os.path.join(project_root, "违章查询", "data")
    return os.path.join(data_dir, "violations.db")


def _get_pid_file(project_root, company):
    safe = _safe_name(company)
    data_dir = os.path.join(project_root, "违章查询", "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, f"keepalive_{safe}.pid")


def _get_log_file(project_root, company):
    safe = _safe_name(company)
    data_dir = os.path.join(project_root, "违章查询", "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, f"keepalive_{safe}.log")


def _get_tab_file(project_root, company):
    """File to persist the keepalive tab ID across daemon restarts."""
    safe = _safe_name(company)
    data_dir = os.path.join(project_root, "违章查询", "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, f"keepalive_tab_{safe}.txt")


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


def _create_keepalive_tab(instance_port, platform_url, log):
    """Create a new browser tab for keepalive and navigate to the platform.
    Returns the new tab_id, or None on failure."""
    try:
        # List existing tabs
        before = _run_pinchtab(["tab"], instance_port=instance_port, timeout=15)
        before_ids = set(re.findall(r'\b(\d+)\b', before.stdout))

        # Open new tab via JS
        _run_pinchtab(["eval", "window.open('about:blank')"],
                      instance_port=instance_port, timeout=15)
        time.sleep(1)

        # Find the new tab
        after = _run_pinchtab(["tab"], instance_port=instance_port, timeout=15)
        after_ids = set(re.findall(r'\b(\d+)\b', after.stdout))

        new_ids = after_ids - before_ids
        if new_ids:
            tab_id = sorted(new_ids, key=int)[-1]
        else:
            all_ids = sorted(after_ids, key=int)
            tab_id = all_ids[-1] if all_ids else "0"

        # Switch to new tab and navigate to platform
        _run_pinchtab(["tab", tab_id], instance_port=instance_port, timeout=15)
        log.info(f"Created keepalive tab {tab_id}, navigating to {platform_url}")

        _run_pinchtab(["nav", platform_url], instance_port=instance_port, timeout=30)
        time.sleep(PAGE_LOAD_WAIT)

        return tab_id
    except Exception as e:
        log.error(f"Failed to create keepalive tab: {e}")
        return None


def _switch_to_tab(tab_id, instance_port, log):
    """Switch to the keepalive tab."""
    try:
        result = _run_pinchtab(["tab", tab_id], instance_port=instance_port, timeout=15)
        if result.returncode == 0:
            log.debug(f"Switched to tab {tab_id}")
            return True
        else:
            log.warning(f"Failed to switch to tab {tab_id}: {result.stderr[:200]}")
            return False
    except Exception as e:
        log.warning(f"Tab switch exception: {e}")
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


def _check_page_state(instance_port, log):
    """Check page state after reload: normal / login-page / rate-limited.

    Returns: ("ok"|"login_expired"|"rate_limited"|"unknown", detail_text)
    """
    try:
        text_result = _run_pinchtab(["text"], instance_port=instance_port, timeout=15)
        page_text = text_result.stdout
    except Exception as e:
        log.warning(f"PinchTab text failed: {e}")
        return ("unknown", f"text command failed: {e}")

    # Check rate-limit keywords first (highest priority)
    for kw in RATE_LIMIT_KEYWORDS:
        if kw in page_text:
            log.warning(f"Rate-limit keyword detected: '{kw}'")
            return ("rate_limited", f"found keyword: {kw}")

    # Check for normal logged-in state FIRST: "退出" button or business menus.
    # These take priority over login-page indicators because nav bars on
    # logged-in pages often still display "单位用户登录"/"个人用户登录" as
    # account-switch links.
    if "退出" in page_text:
        return ("ok", "logged in (退出 button found)")

    if any(kw in page_text for kw in ["车辆管理", "租赁车辆", "业务办理", "违法查询"]):
        return ("ok", "logged in (business menus found)")

    # Check for login page — only if NOT clearly logged in
    login_signals = [s for s in LOGIN_PAGE_INDICATORS if s in page_text]
    if login_signals:
        log.warning(f"Login page detected: {login_signals}")
        return ("login_expired", f"login indicators: {login_signals}")

    # Neither clearly logged in nor clearly expired — log and continue
    text_preview = page_text[:200].replace("\n", " ")
    log.info(f"Ambiguous page state, text preview: {text_preview}")
    return ("unknown", text_preview[:100])


# ── heartbeat ──────────────────────────────────────────────────

def _heartbeat(instance_port, log):
    """Light touch to simulate user activity without a full page reload.

    Performs a random scroll + lightweight JS ping to keep the server-side
    session warm and detect page stalls early (60-120s vs 18 min).

    Returns:
        True  — heartbeat succeeded (page responsive, JS executed)
        False — heartbeat failed (pinchtab timeout / eval error)
    """
    try:
        # Random scroll: pick a y-offset in [100, 1200] to simulate reading
        scroll_y = random.randint(100, 1200)
        scroll_js = f"(function(){{window.scrollTo(0, {scroll_y}); return 'ok'}})()"

        scroll_result = _run_pinchtab(
            ["eval", scroll_js], instance_port=instance_port, timeout=10
        )
        if scroll_result.returncode != 0:
            log.warning(f"Heartbeat scroll failed: {scroll_result.stderr[:100]}")
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
        if ping_result.returncode != 0:
            log.warning(f"Heartbeat ping failed: {ping_result.stderr[:100]}")
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
            capture_output=True, text=True, encoding="utf-8", timeout=15
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
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=timeout, cwd=cwd
    )


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


def _click_unit_login_button(instance_port, log):
    """Click the '单位用户登录'/'单位用户扫码登录' tab/button on the login page."""
    js = """
(function() {
  var all = document.querySelectorAll('*');
  for (var i = 0; i < all.length; i++) {
    var el = all[i];
    var text = (el.textContent || '').trim();
    // Match both "单位用户登录" and "单位用户扫码登录"
    if (text.indexOf('单位用户') !== -1 && (text.indexOf('登录') !== -1 || text.indexOf('扫码') !== -1)) {
      // Only click if this is likely the tab/button itself (not a large container)
      if (text.length < 30) {
        // Try to click the closest clickable ancestor
        var target = el;
        while (target && target.tagName && target.tagName.toLowerCase() !== 'button'
               && target.tagName.toLowerCase() !== 'a'
               && target.tagName.toLowerCase() !== 'li'
               && target.tagName.toLowerCase() !== 'span') {
          target = target.parentElement;
        }
        if (target) {
          target.click();
          return 'clicked:' + target.tagName + ':' + text;
        }
        el.click();
        return 'clicked-element:' + el.tagName + ':' + text;
      }
    }
  }
  return 'not-found';
})()
"""
    try:
        result = _run_pinchtab(["eval", js], instance_port=instance_port, timeout=15)
        log.info(f"Click unit login result: {result.stdout.strip()[:100]}")
        return "clicked" in result.stdout.lower()
    except Exception as e:
        log.error(f"Click unit login error: {e}")
        return False


def _poll_until_logged_in(instance_port, timeout_seconds, poll_interval, log):
    """Poll page state until logged in or timeout. Uses keyword match on
    post-login indicators (公司列表, 退出, 车辆管理 etc.) — this is the
    intended use of text matching: detecting the transition after QR scan.

    Returns True if logged in."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            text_result = _run_pinchtab(["text"], instance_port=instance_port, timeout=15)
            page_text = text_result.stdout

            if "退出" in page_text:
                log.info("Login detected: '退出' button found")
                return True
            if any(kw in page_text for kw in ["车辆管理", "租赁车辆", "业务办理", "违法查询"]):
                log.info(f"Login detected: business menus found")
                return True
            # Also check for company list page (post-QR landing)
            if any(kw in page_text for kw in ["公司列表", "公司名称", "请选择", "选择单位"]):
                log.info("Login detected: company list page")
                return True
        except Exception as e:
            log.warning(f"Poll check error: {e}")

        remaining = int(deadline - time.time())
        log.info(f"Waiting for scan... ({remaining}s remaining)")
        time.sleep(poll_interval)

    log.warning(f"Poll timeout after {timeout_seconds}s")
    return False


def _get_data_dir(project_root):
    """Get the data directory for keepalive files."""
    data_dir = os.path.join(project_root, "违章查询", "data")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def _auto_recover_login(company, instance_port, platform_url, project_root, log,
                        lark_chat_id=None):
    """Attempt to recover login via QR code when session expires.

    At most ONE recovery attempt per invocation. Within that attempt,
    up to MAX_QR_REFRESHES (3) QR codes are sent if they expire.

    Flow:
    1. Navigate to UNIT_LOGIN_URL
    2. Click '单位用户登录'
    3. Screenshot QR code
    4. Send to Lark (if chat_id configured)
    5. Poll for login success (5 min timeout per QR)
    6. On QR expiry: reload page → new QR → re-send (up to 3 total)
    7. On success: navigate to platform_url, mark is_logged_in=1
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
                log.error(f"Navigation failed: {nav_result.stderr[:200]}")
                time.sleep(10)
                continue
            time.sleep(PAGE_LOAD_WAIT)

            # Step 2: Click "单位用户登录"
            log.info("Clicking '单位用户登录'...")
            _click_unit_login_button(instance_port, log)
            time.sleep(LOGIN_PAGE_WAIT)

            # Step 3: Take QR screenshot
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            data_dir = _get_data_dir(project_root)
            qr_file = os.path.join(
                data_dir,
                f"recovery_qr_{_safe_name(company)}_{timestamp}.png"
            )

            screenshot_ok = _take_qr_screenshot(instance_port, qr_file, log)

            # Step 4: Send to Lark if configured
            if lark_chat_id and screenshot_ok:
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
                        recovery_msg = json.dumps({
                            "zh_cn": {
                                "title": "\U0001f504 12123登录已过期 - 需要重新扫码",
                                "content": [
                                    [{"tag": "text", "text": (
                                        f"⚠️ 保活程序检测到登录已过期\n"
                                        f"\U0001f3e2 公司：{company}\n"
                                        f"\U0001f550 时间：{now_str}\n"
                                        f"\U0001f504 恢复尝试：第 {qr_attempt}/{max_qr_sends} 次\n\n"
                                        f"\U0001f4f1 请使用「交管12123」APP 扫描下方二维码重新登录\n\n"
                                        f"\U0001f4dd 登录步骤：\n"
                                        f"① 打开交管12123 APP\n"
                                        f"② 扫一扫下方二维码\n"
                                        f"③ 完成人脸识别\n"
                                        f"④ 登录成功后系统将自动恢复保活"
                                    )}],
                                    [{"tag": "img", "image_key": image_key}],
                                ]
                            }
                        }, ensure_ascii=False)

                        _run_lark(
                            ["im", "+messages-send", "--as", "bot",
                             "--msg-type", "post", "--content", recovery_msg,
                             "--chat-id", lark_chat_id],
                            timeout=20
                        )
                        log.info(f"Recovery QR sent to chat {lark_chat_id}")
                    else:
                        log.warning("Image upload succeeded but no image_key returned")
                except Exception as e:
                    log.warning(f"Lark notification failed (non-fatal): {e}")

            # Step 5: Poll for login
            log.info(f"Polling for login (timeout={RECOVERY_TIMEOUT}s, interval={RECOVERY_POLL_INTERVAL}s)...")
            logged_in = _poll_until_logged_in(
                instance_port, RECOVERY_TIMEOUT, RECOVERY_POLL_INTERVAL, log
            )

            if logged_in:
                # Step 6: Success! Navigate to platform URL and mark logged in
                log.info("Login recovered successfully!")
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
                lark_chat_id=None):
    """Run the keepalive daemon loop. Does not return until exit signal."""
    db_path = _get_db_path(project_root)
    pid_file = _get_pid_file(project_root, company)
    log_file = _get_log_file(project_root, company)
    tab_file = _get_tab_file(project_root, company)

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
        sys.exit(1)

    # Write PID file
    _write_pid(pid_file)

    # Signal handlers for graceful shutdown
    shutdown_flag = {"triggered": False}

    def _on_signal(signum, frame):
        log.info(f"Received signal {signum}, shutting down...")
        shutdown_flag["triggered"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # Verify login state at startup
    profile = _read_profile(db_path, company)
    if profile is None:
        log.error(f"Company '{company}' not found in profiles table.")
        _remove_pid(pid_file)
        print(json.dumps({"ok": False, "error": "company not found in profiles"}))
        sys.exit(1)

    if not profile["is_logged_in"]:
        if auto_recover:
            log.warning(f"Company '{company}' is_logged_in=0. "
                        f"Auto-recover enabled — attempting QR re-login...")
            instance_port = profile.get("instance_port")
            platform_url = profile.get("platform_url", "")

            # Need a tab for recovery — create one if not persisted
            tab_id = _load_tab_id(tab_file)
            if not tab_id or not _switch_to_tab(tab_id, instance_port, log):
                tab_id = _create_keepalive_tab(instance_port, platform_url or UNIT_LOGIN_URL, log)
                if tab_id:
                    _save_tab_id(tab_file, tab_id)

            if tab_id:
                recovered = _auto_recover_login(
                    company, instance_port, platform_url, project_root, log,
                    lark_chat_id=lark_chat_id
                )
                if recovered:
                    log.info("Startup recovery successful! Proceeding with normal keepalive.")
                    profile = _read_profile(db_path, company)  # re-read updated profile
                    if not profile or not profile.get("platform_url"):
                        log.error("Profile missing platform_url after recovery.")
                        _remove_pid(pid_file)
                        sys.exit(1)
                    # Fall through to normal keepalive loop below
                else:
                    log.error("Startup auto-recovery failed — exiting.")
                    _remove_pid(pid_file)
                    sys.exit(1)
            else:
                log.error("Cannot create tab for recovery — exiting.")
                _remove_pid(pid_file)
                sys.exit(1)
        else:
            log.error(f"Company '{company}' is_logged_in=0. Nothing to keep alive.")
            _remove_pid(pid_file)
            print(json.dumps({"ok": False, "error": "is_logged_in is already 0"}))
            sys.exit(1)

    instance_port = profile.get("instance_port")
    platform_url = profile.get("platform_url", "")
    log.info(f"Profile: is_logged_in=1, platform={platform_url}, "
             f"instance_port={instance_port or 'default'}, profile={profile.get('profile_name', '?')}")

    # Persist cookies on startup so they survive any Chrome restart
    _persist_cookies(os.path.expanduser("~/.pinchtab/profiles/default"), log)

    # ── resolve keepalive tab ──
    tab_id = _load_tab_id(tab_file)

    if tab_id:
        # Existing tab — verify it's still valid by switching to it
        log.info(f"Found persisted tab {tab_id}, verifying...")
        if _switch_to_tab(tab_id, instance_port, log):
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
        tab_id = _create_keepalive_tab(instance_port, platform_url, log)
        if not tab_id:
            log.critical("Failed to create keepalive tab.")
            # NOTE: do NOT set is_logged_in=0 here — this could be pinchtab not found,
            # Chrome crash, etc. The session cookies may still be valid.
            _remove_pid(pid_file)
            print(json.dumps({"ok": False, "error": "failed to create tab"}))
            sys.exit(1)
        _save_tab_id(tab_file, tab_id)
        log.info(f"Created and persisted new keepalive tab {tab_id}")

    consecutive_failures = 0
    cycle_count = 0
    recovery_used = False  # only ONE auto-recovery opportunity per keepalive session

    while not shutdown_flag["triggered"]:
        cycle_count += 1
        cycle_start = time.time()
        log.info(f"=== Keepalive cycle #{cycle_count} (tab={tab_id}) ===")

        # ── pre-flight: check is_logged_in ──
        profile = _read_profile(db_path, company)
        if profile is None or not profile["is_logged_in"]:
            log.info("is_logged_in is 0 or profile missing — exiting.")
            break

        # Update instance_port in case it changed (e.g. PinchTab restarted on new port)
        instance_port = profile.get("instance_port")

        # ── step 0: switch to keepalive tab ──
        if not _switch_to_tab(tab_id, instance_port, log):
            log.warning("Tab switch failed — tab may have been closed. Creating new tab.")
            tab_id = _create_keepalive_tab(instance_port, platform_url, log)
            if not tab_id:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.critical("Cannot create/recover tab after failures — exiting.")
                    _set_logged_out(db_path, company)
                    break
                time.sleep(INTERVAL_SECONDS)
                continue
            _save_tab_id(tab_file, tab_id)

        # ── step 1: reload page ──
        try:
            reload_result = _run_pinchtab(["reload"], instance_port=instance_port, timeout=30)
            if reload_result.returncode != 0:
                consecutive_failures += 1
                log.error(f"Reload failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): "
                          f"{reload_result.stderr[:200]}")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.critical("Too many consecutive reload failures — exiting.")
                    _set_logged_out(db_path, company)
                    break
                time.sleep(INTERVAL_SECONDS)
                continue
            consecutive_failures = 0
            time.sleep(PAGE_LOAD_WAIT)
        except Exception as e:
            consecutive_failures += 1
            log.error(f"Reload exception: {e} ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.critical("Too many consecutive reload failures — exiting.")
                _set_logged_out(db_path, company)
                break
            time.sleep(INTERVAL_SECONDS)
            continue

        # ── step 2: dismiss popups ──
        _dismiss_popup(instance_port, log)

        # ── step 3: check page state ──
        state, detail = _check_page_state(instance_port, log)

        if state == "rate_limited":
            log.critical(f"Rate-limited! {detail}")
            _set_logged_out(db_path, company)
            break

        elif state == "login_expired":
            log.warning(f"Login expired: {detail}")
            _set_logged_out(db_path, company)
            if auto_recover and not recovery_used:
                recovery_used = True
                log.info("Auto-recover: attempting ONE-TIME QR re-login...")
                recovered = _auto_recover_login(
                    company, instance_port, platform_url, project_root, log,
                    lark_chat_id=lark_chat_id
                )
                if recovered:
                    log.info("Recovery successful — resuming keepalive cycle")
                    consecutive_failures = 0
                    recovery_used = True  # already used the one opportunity
                    # Persist cookies immediately after recovery
                    _persist_cookies(
                        os.path.expanduser("~/.pinchtab/profiles/default"), log
                    )
                    continue
                else:
                    log.critical("Auto-recovery failed — exiting.")
            break

        elif state == "ok":
            log.info(f"Page state OK: {detail}")
            # Persist cookies every cycle so session cookies survive Chrome restart
            _persist_cookies(
                os.path.expanduser("~/.pinchtab/profiles/default"), log
            )

        else:
            # "unknown" — try snap to get more info
            log.warning(f"Unknown page state, checking snap: {detail}")
            try:
                snap_result = _run_pinchtab(["snap"], instance_port=instance_port, timeout=15)
                snap_text = snap_result.stdout
                if any(kw in snap_text for kw in RATE_LIMIT_KEYWORDS):
                    log.critical("Rate-limit keyword found in snap!")
                    _set_logged_out(db_path, company)
                    break
                if any(kw in snap_text for kw in LOGIN_PAGE_INDICATORS):
                    log.warning("Login page detected in snap!")
                    _set_logged_out(db_path, company)
                    if auto_recover and not recovery_used:
                        recovery_used = True
                        log.info("Auto-recover: attempting ONE-TIME QR re-login...")
                        recovered = _auto_recover_login(
                            company, instance_port, platform_url, project_root, log,
                            lark_chat_id=lark_chat_id
                        )
                        if recovered:
                            log.info("Recovery successful — resuming keepalive cycle")
                            consecutive_failures = 0
                            continue
                        else:
                            log.critical("Auto-recovery failed — exiting.")
                    break
                if "退出" in snap_text or "车辆管理" in snap_text:
                    log.info("Snap confirms logged-in state.")
                else:
                    log.warning(f"Snap inconclusive, continuing anyway. "
                                f"Preview: {snap_text[:200].replace(chr(10), ' ')}")
            except Exception as e:
                log.error(f"Snap failed: {e}")

        # ── step 4: sleep + interleaved heartbeats until next cycle ──
        elapsed = time.time() - cycle_start
        sleep_time = max(10, INTERVAL_SECONDS - elapsed)
        log.info(f"Cycle {cycle_count} done ({elapsed:.0f}s). "
                 f"Next reload in {sleep_time / 60:.1f} min. "
                 f"Heartbeat every {HEARTBEAT_MIN_SEC}-{HEARTBEAT_MAX_SEC}s.")

        # Determine next heartbeat delay (random within [min, max])
        next_heartbeat = random.randint(HEARTBEAT_MIN_SEC, HEARTBEAT_MAX_SEC)
        heartbeat_fail_streak = 0
        heartbeat_count = 0

        while sleep_time > 0 and not shutdown_flag["triggered"]:
            chunk = min(next_heartbeat, sleep_time)
            time.sleep(chunk)
            sleep_time -= chunk
            next_heartbeat -= chunk

            if next_heartbeat <= 0 and sleep_time > 10 and state == "ok":
                # Perform a light heartbeat: random scroll + ping
                heartbeat_count += 1
                hb_start = time.time()
                ok = _heartbeat(instance_port, log)
                hb_elapsed = time.time() - hb_start

                if ok:
                    heartbeat_fail_streak = 0
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

                # Schedule next heartbeat with random interval
                next_heartbeat = random.randint(HEARTBEAT_MIN_SEC, HEARTBEAT_MAX_SEC)

    # ── cleanup ──
    log.info("Keepalive daemon exiting.")
    _remove_pid(pid_file)
    # Note: we do NOT remove the tab_file — the tab persists in Chrome
    # and can be reused if the daemon is restarted.


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
    parser.add_argument("--lark-chat-id",
                        help="飞书群聊ID（自动恢复时发送QR码到此群，需要 --auto-recover）")
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
                    lark_chat_id=args.lark_chat_id)


if __name__ == "__main__":
    main()

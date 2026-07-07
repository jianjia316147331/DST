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
  3. 每 18 分钟执行一个保活周期
  4. 每个周期开始前检查 is_logged_in，若变为 0 则自动退出
  5. 检测到登录页或风控关键词 → profile-logout → 退出
  6. 收到 SIGTERM/SIGINT → 清理 PID 文件后退出

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
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

# ── constants ──────────────────────────────────────────────────
INTERVAL_SECONDS = 18 * 60          # 18 minutes
PAGE_LOAD_WAIT = 5                   # wait after reload
POPUP_DISMISS_WAIT = 3               # wait after dismiss
MAX_CONSECUTIVE_FAILURES = 3         # consecutive reload failures → exit

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


def _get_db_path(project_root):
    return os.path.join(project_root, "违章查询", "data", "violations.db")


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
  var closeTexts = ['关闭', '×', '取消', 'close', 'x', '本人已知晓',
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

    # Check for login page
    login_signals = [s for s in LOGIN_PAGE_INDICATORS if s in page_text]
    if login_signals:
        log.warning(f"Login page detected: {login_signals}")
        return ("login_expired", f"login indicators: {login_signals}")

    # Check for normal logged-in state: has "退出" or business menus
    if "退出" in page_text:
        return ("ok", "logged in (退出 button found)")

    if any(kw in page_text for kw in ["车辆管理", "租赁车辆", "业务办理", "违法查询"]):
        return ("ok", "logged in (business menus found)")

    # Neither clearly logged in nor clearly expired — log and continue
    text_preview = page_text[:200].replace("\n", " ")
    log.info(f"Ambiguous page state, text preview: {text_preview}")
    return ("unknown", text_preview[:100])


# ── main daemon loop ───────────────────────────────────────────

def _run_daemon(company, project_root):
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
        log.error(f"Company '{company}' is_logged_in=0. Nothing to keep alive.")
        _remove_pid(pid_file)
        print(json.dumps({"ok": False, "error": "is_logged_in is already 0"}))
        sys.exit(1)

    instance_port = profile.get("instance_port")
    platform_url = profile.get("platform_url", "")
    log.info(f"Profile: is_logged_in=1, platform={platform_url}, "
             f"instance_port={instance_port or 'default'}, profile={profile.get('profile_name', '?')}")

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
            _set_logged_out(db_path, company)
            _remove_pid(pid_file)
            print(json.dumps({"ok": False, "error": "no platform_url configured"}))
            sys.exit(1)
        tab_id = _create_keepalive_tab(instance_port, platform_url, log)
        if not tab_id:
            log.critical("Failed to create keepalive tab.")
            _set_logged_out(db_path, company)
            _remove_pid(pid_file)
            print(json.dumps({"ok": False, "error": "failed to create tab"}))
            sys.exit(1)
        _save_tab_id(tab_file, tab_id)
        log.info(f"Created and persisted new keepalive tab {tab_id}")

    consecutive_failures = 0
    cycle_count = 0

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
            break

        elif state == "ok":
            log.info(f"Page state OK: {detail}")

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
                    break
                if "退出" in snap_text or "车辆管理" in snap_text:
                    log.info("Snap confirms logged-in state.")
                else:
                    log.warning(f"Snap inconclusive, continuing anyway. "
                                f"Preview: {snap_text[:200].replace(chr(10), ' ')}")
            except Exception as e:
                log.error(f"Snap failed: {e}")

        # ── step 4: sleep until next cycle ──
        elapsed = time.time() - cycle_start
        sleep_time = max(10, INTERVAL_SECONDS - elapsed)
        log.info(f"Cycle {cycle_count} done ({elapsed:.0f}s). Next in {sleep_time / 60:.1f} min.")

        while sleep_time > 0 and not shutdown_flag["triggered"]:
            chunk = min(30, sleep_time)
            time.sleep(chunk)
            sleep_time -= chunk

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
  启动保活（后台）:
    nohup python3 keepalive_daemon.py --company "北京安桉" --project-root /home/user/project &
    disown

  查看状态:
    python3 keepalive_daemon.py --company "北京安桉" --project-root /home/user/project --status

  停止保活:
    python3 keepalive_daemon.py --company "北京安桉" --project-root /home/user/project --stop

多公司同时保活:
    nohup python3 keepalive_daemon.py --company "北京安桉" --project-root /home/user/project &
    nohup python3 keepalive_daemon.py --company "成都某某" --project-root /home/user/project &
    disown -a
        """,
    )
    parser.add_argument("--company", required=True, help="公司名称（必填，每个公司一个守护进程）")
    parser.add_argument("--project-root", required=True, help="项目根目录（必填）")
    parser.add_argument("--status", action="store_true", help="查看保活状态")
    parser.add_argument("--stop", action="store_true", help="停止保活守护进程")
    args = parser.parse_args()

    if args.status:
        _cmd_status(args.company, args.project_root)
    elif args.stop:
        _cmd_stop(args.company, args.project_root)
    else:
        _run_daemon(args.company, args.project_root)


if __name__ == "__main__":
    main()

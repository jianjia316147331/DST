#!/usr/bin/env python3
"""lib/profiles.py — Profile management and keepalive daemon control."""
import json, os, sqlite3, sys, time, subprocess
from datetime import datetime

from .core import (_run, _run_silent, _read_stdin_text, _read_stdin_json,
                   _pinchtab_base_cmd, _find_project_root, _get_data_dir,
                   _get_query_dir, _pinchtab_path, _lark_cli_path,
                   _ensure_subdirs)
from .db import _init_db, _get_db_path, _get_db_conn

def cmd_keepalive_health():
    """Check keepalive daemon health for a company.
    Args: --company "公司名"
    Returns JSON: {alive: bool, state: str, last_check: str, cycle_count: int, pid: int}
    alive=true only if: health file exists AND < 5 min stale AND a process with
    the matching PID is running.
    """
    p = {"company": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        else:
            i += 1

    if not p["company"]:
        print(json.dumps({"alive": False, "error": "missing --company"}, ensure_ascii=False))
        sys.exit(1)

    # Build health file path (same convention as keepalive daemon)
    safe = p["company"].replace("/", "_").replace(" ", "_")
    data_dir = _get_data_dir()
    health_file = os.path.join(data_dir, f"keepalive_health_{safe}.json")

    if not os.path.exists(health_file):
        # Fuzzy fallback: scan data dir for matching health files
        data_dir = _get_data_dir()
        candidates = []
        try:
            for fname in os.listdir(data_dir):
                if fname.startswith("keepalive_health_") and p["company"] in fname:
                    candidates.append(os.path.join(data_dir, fname))
        except OSError:
            pass
        if len(candidates) == 1:
            health_file = candidates[0]
        elif len(candidates) > 1:
            print(json.dumps({
                "alive": False,
                "reason": "ambiguous health file",
                "company": p["company"],
                "candidates": [os.path.basename(c) for c in candidates],
                "hint": f"找到 {len(candidates)} 个匹配的 health file，请用完整公司名重试"
            }, ensure_ascii=False))
            return
        else:
            print(json.dumps({"alive": False, "reason": "no health file", "company": p["company"],
                "hint": "用 profile-lookup --company 或 profile-list 确认完整公司名"}, ensure_ascii=False))
            return

    try:
        with open(health_file, "r", encoding="utf-8") as f:
            health = json.load(f)
    except Exception:
        print(json.dumps({"alive": False, "reason": "health file unreadable", "company": p["company"]}, ensure_ascii=False))
        return

    state = health.get("state", "unknown")
    last_check = health.get("last_check", "")

    # Freshness check: health file must be < 5 min stale
    try:
        last_dt = datetime.strptime(last_check, "%Y-%m-%d %H:%M:%S")
        stale_seconds = (datetime.now() - last_dt).total_seconds()
    except Exception:
        stale_seconds = 9999

    alive = stale_seconds < 300  # 5 minutes

    # Also check PID if available (daemon may have died without updating health)
    pid_file = os.path.join(data_dir, f"keepalive_{safe}.pid")
    pid_from_file = None
    if os.path.exists(pid_file):
        try:
            with open(pid_file, "r") as f:
                pid_from_file = int(f.read().strip())
            # Check if process is running
            os.kill(pid_from_file, 0)
        except (ValueError, OSError):
            alive = False

    print(json.dumps({
        "alive": alive,
        "state": state,
        "last_check": last_check,
        "stale_seconds": round(stale_seconds, 1),
        "cycle_count": health.get("cycle_count", 0),
        "tab_id": health.get("tab_id", ""),
        "instance_port": health.get("instance_port"),
        "pid": pid_from_file,
        "company": p["company"]
    }, ensure_ascii=False))


def cmd_ensure_keepalive():
    """Auto-start keepalive daemon for a company if not already running.
    Designed to be called after successful login (profile-register).
    Prevents duplicate keepalive processes.

    Args:
      --company NAME          Company name (required)
      --project-root DIR      Project root directory (required)
      --notify-user NAME       (optional) Person to notify for QR recovery
      --notify-phone PHONE     (optional) Phone number for QR recovery
      --notify-chat CHAT       (optional) Group name for QR recovery

    Returns JSON:
      {ok: true, action: "started"|"already_running"|"skipped", ...}
    """
    p = {"company": "", "project_root": "", "notify_user": "", "notify_phone": "", "notify_chat": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        elif args[i] == "--project-root" and i + 1 < len(args):
            p["project_root"] = args[i + 1]; i += 2
        elif args[i] == "--notify-user" and i + 1 < len(args):
            p["notify_user"] = args[i + 1]; i += 2
        elif args[i] == "--notify-phone" and i + 1 < len(args):
            p["notify_phone"] = args[i + 1]; i += 2
        elif args[i] == "--notify-chat" and i + 1 < len(args):
            p["notify_chat"] = args[i + 1]; i += 2
        else:
            i += 1

    company = p["company"]
    project_root = p["project_root"]

    if not company or not project_root:
        print(json.dumps({"ok": False, "error": "missing --company or --project-root"}, ensure_ascii=False))
        sys.exit(1)

    # Step 1: Check is_logged_in + get profile_name for ASCII-safe service name
    _init_db()
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "SELECT is_logged_in, profile_name FROM profiles WHERE company_name = ?", (company,))
    row = cur.fetchone()
    conn.close()

    if not row or not row[0]:
        print(json.dumps({
            "ok": False, "action": "skipped",
            "reason": "is_logged_in is 0 or company not found — not starting keepalive"
        }, ensure_ascii=False))
        return

    profile_name = row[1] if row[1] else "default"

    # Generate ASCII-safe identifier for systemd service name.
    # systemd does NOT support non-ASCII characters in unit names.
    # Use profile_name (e.g. "profile_002") which is always ASCII.
    safe = profile_name  # "profile_002" → keepalive-profile_002.service

    # Step 2: Check if keepalive daemon is already running
    # 2a: Check PID file + process
    # Note: PID/health files still use company-based naming (safe_for_file)
    # to remain human-readable; only service name uses profile_name
    safe_for_file = company.replace("/", "_").replace(" ", "_")
    data_dir = _get_data_dir()
    pid_file = os.path.join(data_dir, f"keepalive_{safe_for_file}.pid")
    already_running = False
    existing_pid = None

    if os.path.exists(pid_file):
        try:
            with open(pid_file, "r") as f:
                existing_pid = int(f.read().strip())
            os.kill(existing_pid, 0)  # signal 0 = check if process exists
            already_running = True
        except (ValueError, OSError):
            pass

    # 2b: Check systemd user service
    if not already_running:
        service_name = f"keepalive-{safe}.service"
        try:
            result = _run(["systemctl", "--user", "is-active", service_name], timeout=5)
            if result.stdout.strip() == "active":
                already_running = True
        except Exception:
            pass

    if already_running:
        # Already running — just update notify config if provided
        if p["notify_user"] or p["notify_phone"] or p["notify_chat"]:
            _save_notify_config(company, project_root, p)
        print(json.dumps({
            "ok": True, "action": "already_running",
            "pid": existing_pid, "company": company
        }, ensure_ascii=False))
        return

    # Step 3: Start systemd service
    service_name = f"keepalive-{safe}.service"

    # Persist notify config before starting (so daemon picks it up)
    if p["notify_user"] or p["notify_phone"] or p["notify_chat"]:
        _save_notify_config(company, project_root, p)

    try:
        # Enable linger (one-time setup, idempotent)
        _run_silent(["loginctl", "enable-linger"], timeout=5)

        # Start and enable the service
        start_result = _run(["systemctl", "--user", "start", service_name], timeout=10)
        if start_result.returncode != 0:
            print(json.dumps({
                "ok": False, "action": "failed",
                "reason": f"systemctl start failed: {start_result.stderr.strip()[:200]}",
                "service": service_name
            }, ensure_ascii=False))
            sys.exit(1)

        enable_result = _run(["systemctl", "--user", "enable", service_name], timeout=10)
        if enable_result.returncode != 0:
            print(json.dumps({
                "ok": True, "action": "started",
                "warning": f"service started but enable failed: {enable_result.stderr.strip()[:200]}",
                "service": service_name, "company": company
            }, ensure_ascii=False))
            return

        print(json.dumps({
            "ok": True, "action": "started",
            "service": service_name, "company": company
        }, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({
            "ok": False, "action": "failed",
            "reason": str(e), "service": service_name
        }, ensure_ascii=False))
        sys.exit(1)


def _save_notify_config(company, project_root, p):
    """Persist notify target config for keepalive daemon auto-recovery.
    Writes keepalive_notify_<company>.json in violation_query/data/."""
    safe = company.replace("/", "_").replace(" ", "_")
    data_dir = os.path.join(project_root, "violation_query", "data")
    os.makedirs(data_dir, exist_ok=True)
    notify_file = os.path.join(data_dir, f"keepalive_notify_{safe}.json")

    notify = {}
    if p.get("notify_chat"):
        # For chat notify, we'd need to search for chat_id. For now, save raw info.
        notify["type"] = "chat"
        notify["chat_name"] = p["notify_chat"]
        notify["label"] = p["notify_chat"]
    elif p.get("notify_user"):
        notify["type"] = "user"
        notify["label"] = p["notify_user"]
    elif p.get("notify_phone"):
        notify["type"] = "phone"
        notify["label"] = p["notify_phone"]

    if notify:
        notify["company"] = company
        notify["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(notify_file, "w", encoding="utf-8") as f:
            json.dump(notify, f, ensure_ascii=False, indent=2)

def _build_profile_result(row):
    """Build profile result dict from a DB row. Also attaches keepalive health."""
    company_name = row[0]
    result = {
        "found": True,
        "company_name": company_name,
        "profile_name": row[1],
        "profile_id": row[2],
        "platform_url": row[3],
        "instance_port": row[4],
        "last_login": row[5],
        "is_logged_in": bool(row[6])
    }
    # Check keepalive health file freshness
    safe = company_name.replace("/", "_").replace(" ", "_")
    health_file = os.path.join(_get_data_dir(), f"keepalive_health_{safe}.json")
    keepalive_alive = False
    keepalive_state = "unknown"
    if os.path.exists(health_file):
        try:
            with open(health_file, "r", encoding="utf-8") as f:
                health = json.load(f)
            last_check = health.get("last_check", "")
            if last_check:
                last_dt = datetime.strptime(last_check, "%Y-%m-%d %H:%M:%S")
                stale_seconds = (datetime.now() - last_dt).total_seconds()
                keepalive_alive = stale_seconds < 300
                keepalive_state = health.get("state", "unknown")
        except Exception:
            pass

    # Verify PinchTab instance is actually running (when instance_port is set)
    instance_running = False
    instance_port = row[4]
    if instance_port:
        try:
            pt = _pinchtab_base_cmd()
            r = _run([pt[0], "instances", "--json"], timeout=10)
            instances = json.loads(r.stdout)
            for inst in instances:
                if (str(inst.get("port", "")) == str(instance_port)
                        and inst.get("status") == "running"):
                    instance_running = True
                    break
        except Exception:
            pass

    # Instance down overrides keepalive health
    if instance_port and not instance_running:
        keepalive_alive = False
        keepalive_state = "instance_down"

    result["keepalive_alive"] = keepalive_alive
    result["keepalive_state"] = keepalive_state
    result["instance_running"] = instance_running
    return result


def cmd_profile_lookup():
    """Look up a company's profile mapping with fuzzy fallback.
    Args: --company "公司名"
    - First tries exact match (WHERE company_name = ?)
    - If not found, tries fuzzy match (WHERE company_name LIKE '%keyword%')
    - Single fuzzy result: auto-returns with match_type: "fuzzy"
    - Multiple fuzzy results: returns candidates for user confirmation
    Returns: JSON {found: true, match_type: "exact"|"fuzzy", ...profile fields}
             or {found: false, candidates: [...], need_confirm: true} when ambiguous
             or {found: false} when no match at all.
    """
    p = {"company": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        else:
            i += 1

    keyword = p["company"]
    if not keyword:
        print(json.dumps({"found": False, "error": "missing --company"}, ensure_ascii=False))
        sys.exit(1)

    _init_db()
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)

    # --- Tier 1: exact match ---
    cur = conn.execute(
        "SELECT company_name, profile_name, profile_id, platform_url, instance_port, last_login, is_logged_in FROM profiles WHERE company_name = ?",
        (keyword,))
    row = cur.fetchone()
    if row:
        conn.close()
        result = _build_profile_result(row)
        result["match_type"] = "exact"
        print(json.dumps(result, ensure_ascii=False))
        return

    # --- Tier 2: fuzzy match (LIKE) ---
    cur = conn.execute(
        "SELECT company_name, profile_name, profile_id, platform_url, instance_port, last_login, is_logged_in FROM profiles WHERE company_name LIKE ?",
        (f"%{keyword}%",))
    rows = cur.fetchall()
    conn.close()

    if len(rows) == 0:
        # No match at all — include diagnostic hints
        print(json.dumps({"found": False}, ensure_ascii=False))
    elif len(rows) == 1:
        # Single fuzzy match — auto-select
        result = _build_profile_result(rows[0])
        result["match_type"] = "fuzzy"
        print(json.dumps(result, ensure_ascii=False))
    else:
        # Multiple fuzzy matches — return candidates for user confirmation
        candidates = []
        for r in rows:
            candidates.append({
                "company_name": r[0],
                "profile_name": r[1],
                "platform_url": r[3],
                "is_logged_in": bool(r[6])
            })
        print(json.dumps({
            "found": False,
            "need_confirm": True,
            "candidates": candidates,
            "hint": f"找到 {len(candidates)} 家含「{keyword}」的公司，请确认是哪一家"
        }, ensure_ascii=False))


def cmd_profile_list():
    """List all registered company profiles.
    Returns: JSON {profiles: [{company_name, profile_name, platform_url, is_logged_in, last_login}, ...], count: N}
    """
    _init_db()
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "SELECT company_name, profile_name, platform_url, is_logged_in, last_login FROM profiles ORDER BY company_name")
    rows = cur.fetchall()
    conn.close()
    profiles = []
    for r in rows:
        profiles.append({
            "company_name": r[0],
            "profile_name": r[1],
            "platform_url": r[2],
            "is_logged_in": bool(r[3]),
            "last_login": r[4]
        })
    print(json.dumps({"profiles": profiles, "count": len(profiles)}, ensure_ascii=False))

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

    # 🔴 Sync to companies table (db-insert-company upsert logic)
    # Ensures company_id exists for db-insert-vehicle references.
    # Previously this was a separate manual step — profile-register
    # now does it automatically so company_id is never stale.
    _init_db()
    conn2 = sqlite3.connect(db_path)
    cur = conn2.execute("SELECT id FROM companies WHERE name = ?", (p["company"],))
    row = cur.fetchone()
    if row:
        conn2.execute("UPDATE companies SET query_date = ? WHERE id = ?",
                      (time.strftime('%Y-%m-%d'), row[0]))
    else:
        conn2.execute("INSERT INTO companies (name, query_date) VALUES (?, ?)",
                      (p["company"], time.strftime('%Y-%m-%d')))
    conn2.commit()
    conn2.close()

    # Auto-discover instance port (unless already explicitly provided)
    session_mgr = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "session_manager.py")
    # Also check in skill directory
    if not os.path.exists(session_mgr):
        skill_dir = os.path.join(os.path.expanduser("~"), ".claude", "skills",
                                 "DST违章查询", "session_manager.py")
        if os.path.exists(skill_dir):
            session_mgr = skill_dir

    discover_result = {"ran": False}
    if os.path.exists(session_mgr) and not p.get("instance_port"):
        try:
            r = subprocess.run(
                [sys.executable, session_mgr, "instance-discover"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=15
            )
            if r.returncode == 0:
                discover_result = json.loads(r.stdout)
                discover_result["ran"] = True
        except Exception:
            pass

    result = {"ok": True, "company": p["company"], "profile_name": p["profile_name"]}
    if discover_result.get("ran"):
        result["instance_discover"] = discover_result
    print(json.dumps(result, ensure_ascii=False))

def cmd_profile_logout():
    """Mark a company profile as logged out and clean up all associated resources.

    Args: --company "公司名"
    Called when: user explicitly logs out, keep-alive detects session expired,
    or get-login-type detects page returned to login screen.

    Cleanup steps:
      1. Stop + disable systemd keepalive service
      2. Remove .service file
      3. Close keepalive browser tab
      4. Remove pid/health/tab/notify files
      5. SET is_logged_in = 0
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

    # Read profile info before updating
    cur = conn.execute(
        "SELECT profile_name, instance_port FROM profiles WHERE company_name = ?",
        (p["company"],))
    row = cur.fetchone()
    profile_name = row[0] if row else None
    instance_port = row[1] if row else None

    company = p["company"]
    safe = company.replace("/", "_").replace(" ", "_") if company else ""
    data_dir = _get_data_dir()
    cleanup_results = {}

    # ── Step 1: Stop + disable systemd keepalive service ──
    if profile_name:
        service_name = f"keepalive-{profile_name}.service"
        try:
            _run(["systemctl", "--user", "disable", "--now", service_name], timeout=10)
            cleanup_results["service_stopped"] = service_name
        except Exception as e:
            cleanup_results["service_stop_error"] = str(e)[:200]

        # ── Step 2: Remove .service file ──
        service_path = os.path.join(
            os.path.expanduser("~"), ".config", "systemd", "user", service_name)
        try:
            if os.path.exists(service_path):
                os.unlink(service_path)
                cleanup_results["service_file_removed"] = service_path
                _run(["systemctl", "--user", "daemon-reload"], timeout=5)
        except Exception as e:
            cleanup_results["service_file_error"] = str(e)[:200]

    # ── Step 3: Close keepalive browser tab ──
    tab_file = os.path.join(data_dir, f"keepalive_tab_{safe}.txt")
    if os.path.exists(tab_file):
        try:
            with open(tab_file, "r", encoding="utf-8") as f:
                tab_id = f.read().strip()
            if tab_id and instance_port:
                subprocess.run(
                    [_pinchtab_path(), "--server",
                     f"http://127.0.0.1:{instance_port}",
                     "close", tab_id],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10)
                cleanup_results["tab_closed"] = tab_id[:12] + "..."
        except Exception as e:
            cleanup_results["tab_close_error"] = str(e)[:200]

    # ── Step 4: Remove pid/health/tab/notify files ──
    for suffix in [".pid", "_health", ".json", ".txt"]:
        patterns = [
            os.path.join(data_dir, f"keepalive_{safe}{suffix}"),
            os.path.join(data_dir, f"keepalive_health_{safe}.json"),
            os.path.join(data_dir, f"keepalive_notify_{safe}.json"),
        ]
        for fp in patterns:
            try:
                if os.path.exists(fp):
                    os.unlink(fp)
            except OSError:
                pass
    # Also try tab file explicitly (already handled above but double-check)
    try:
        if os.path.exists(tab_file):
            os.unlink(tab_file)
    except OSError:
        pass

    # ── Step 5: Mark logged out ──
    conn.execute(
        "UPDATE profiles SET is_logged_in = 0 WHERE company_name = ?",
        (company,))
    updated = conn.total_changes
    conn.commit()
    conn.close()

    result = {"ok": True, "company": company, "logged_out": updated > 0}
    if cleanup_results:
        result["cleanup"] = cleanup_results
    print(json.dumps(result, ensure_ascii=False))

def cmd_save_notify():
    """Persist auto-recovery notify target for keepalive daemon.
    Called by the query flow after successful login so the daemon knows
    who to notify for future auto-recovery QR codes.

    When type=chat, optional --at-user-id and --at-user-name can be provided
    so the keepalive recovery QR @mentions the same person as the query flow.

    Args: --company "公司名" --project-root DIR --type user|chat --id <open_id|chat_id> --label "姓名/群名"
          [--at-user-id ou_xxx] [--at-user-name "姓名"]  (group @mention target)
    """
    p = {"company": "", "project_root": "", "type": "", "id": "", "label": "",
         "at_user_id": "", "at_user_name": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        elif args[i] == "--project-root" and i + 1 < len(args):
            p["project_root"] = args[i + 1]; i += 2
        elif args[i] == "--type" and i + 1 < len(args):
            p["type"] = args[i + 1]; i += 2
        elif args[i] == "--id" and i + 1 < len(args):
            p["id"] = args[i + 1]; i += 2
        elif args[i] == "--label" and i + 1 < len(args):
            p["label"] = args[i + 1]; i += 2
        elif args[i] == "--at-user-id" and i + 1 < len(args):
            p["at_user_id"] = args[i + 1]; i += 2
        elif args[i] == "--at-user-name" and i + 1 < len(args):
            p["at_user_name"] = args[i + 1]; i += 2
        else:
            i += 1

    if not p["company"] or not p["project_root"] or not p["type"] or not p["id"]:
        print(json.dumps({"ok": False, "error": "missing required args (--company, --project-root, --type, --id)"}))
        sys.exit(1)

    safe = p["company"].replace("/", "_").replace(" ", "_")
    data_dir = os.path.join(p["project_root"], "violation_query", "data")
    os.makedirs(data_dir, exist_ok=True)
    notify_file = os.path.join(data_dir, f"keepalive_notify_{safe}.json")

    notify = {"type": p["type"], "id": p["id"], "label": p["label"]}
    if p["at_user_id"]:
        notify["at_user_id"] = p["at_user_id"]
    if p["at_user_name"]:
        notify["at_user_name"] = p["at_user_name"]
    try:
        with open(notify_file, "w", encoding="utf-8") as f:
            json.dump(notify, f, ensure_ascii=False)
        print(json.dumps({"ok": True, "file": notify_file}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)



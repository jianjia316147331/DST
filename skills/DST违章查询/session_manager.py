#!/usr/bin/env python3
"""
session_manager.py — PinchTab session + instance binding manager.

Manages tab lifecycle AND instance routing, maintaining a registry mapping
session labels to browser tab IDs and instance ports.  Outputs shell-eval-able
environment commands so that any process can set VIOLATION_TAB_ID and
VIOLATION_INSTANCE_PORT and have all PinchTab operations in violation_helper.py
and keepalive_daemon.py automatically scoped to the correct tab + instance.

Usage:
  eval $(python3 session_manager.py init --label "深圳查询" --instance-port 9871)
  eval $(python3 session_manager.py bind --tab-id <id> --label "深圳查询")
  python3 session_manager.py list
  python3 session_manager.py current
  eval $(python3 session_manager.py release --label "深圳查询")
  python3 session_manager.py instance-discover
  python3 session_manager.py instance-status --company "公司名"

Registry:  violation_query/data/tab_registry.json
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime

_REGISTRY_DIR = os.path.join(os.getcwd(), "violation_query", "data")
_REGISTRY_PATH = os.path.join(_REGISTRY_DIR, "tab_registry.json")

# ── helpers ─────────────────────────────────────────────────────

def _read_config():
    """Read PinchTab server config."""
    cfg_path = os.path.join(os.path.expanduser("~"), ".pinchtab", "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_registry():
    os.makedirs(_REGISTRY_DIR, exist_ok=True)
    if not os.path.exists(_REGISTRY_PATH):
        return {}
    try:
        with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _write_registry(data):
    os.makedirs(_REGISTRY_DIR, exist_ok=True)
    with open(_REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_db_path():
    return os.path.join(os.getcwd(), "violation_query", "data", "violations.db")


def _create_tab(instance_port=None):
    """Create a new browser tab via PinchTab HTTP API (POST /tab).

    If instance_port is provided, POSTs directly to that instance's port.
    Otherwise uses the default server port from config.

    Returns tab_id (hex string) or exits with error."""
    cfg = _read_config()
    port = instance_port if instance_port else cfg["server"]["port"]
    data = json.dumps({"action": "new", "focus": False}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/tab",
        data=data,
        headers={
            "Authorization": f"Bearer {cfg['server']['token']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read().decode("utf-8"))
        tab_id = result.get("tabId", "")
        if not tab_id or not re.match(r'^[0-9A-F]{32}$', tab_id):
            print(f"error: invalid tabId: {tab_id!r}", file=sys.stderr)
            sys.exit(1)
        return tab_id
    except Exception as e:
        print(f"error: failed to create tab: {e}", file=sys.stderr)
        sys.exit(1)


def _export_env(tab_id, instance_port=None):
    """Print shell command to set VIOLATION_TAB_ID and optionally VIOLATION_INSTANCE_PORT."""
    print(f"export VIOLATION_TAB_ID={tab_id}")
    if instance_port:
        print(f"export VIOLATION_INSTANCE_PORT={instance_port}")


def _unset_env():
    """Print shell command to unset VIOLATION_TAB_ID and VIOLATION_INSTANCE_PORT."""
    print("unset VIOLATION_TAB_ID")
    print("unset VIOLATION_INSTANCE_PORT")


# ── PinchTab instance helpers ────────────────────────────────────

def _get_pinchtab_binary():
    """Resolve pinchtab binary path."""
    for name in ["pinchtab", "pinchtab.exe"]:
        for d in os.environ.get("PATH", "").split(os.pathsep):
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return "pinchtab"


def _get_active_tab_ids(instance_port=None):
    """Query PinchTab for active tab IDs on a specific instance.

    Returns a set of tab_id strings, or empty set on failure.
    """
    try:
        pt = _get_pinchtab_binary()
        cmd = [pt, "tab"]
        if instance_port:
            cmd.insert(1, f"http://127.0.0.1:{instance_port}")
            cmd.insert(1, "--server")
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                encoding="utf-8", timeout=10)
        data = json.loads(result.stdout)
        tabs = data.get("tabs", [])
        return {t["id"] for t in tabs if t.get("id")}
    except Exception:
        return set()


def _clean_ghost_entries(registry, instance_port=None):
    """Remove registry entries whose browser tabs no longer exist.

    Returns (cleaned_registry, ghost_labels).
    """
    active_ids = _get_active_tab_ids(instance_port=instance_port)
    ghosts = []
    for label, info in list(registry.items()):
        tab_id = info.get("tab_id", "")
        if not tab_id:
            continue
        entry_port = info.get("instance_port")
        # Only check entries on the same instance
        if instance_port is not None and entry_port is not None:
            try:
                if int(entry_port) != int(instance_port):
                    continue
            except (ValueError, TypeError):
                pass
        elif instance_port is None and entry_port is not None:
            # Can't reliably check cross-instance; skip
            continue
        if tab_id not in active_ids:
            ghosts.append(label)
            del registry[label]
    return registry, ghosts


def _get_running_instances():
    """Call 'pinchtab instances --json' and return parsed list.
    Returns empty list on any error."""
    try:
        pt = _get_pinchtab_binary()
        result = subprocess.run(
            [pt, "instances", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=10
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception as e:
        print(f"error: failed to query instances: {e}", file=sys.stderr)
    return []


def _read_profiles_table():
    """Read all rows from profiles table. Returns list of dicts."""
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT company_name, profile_name, profile_id, platform_url, "
            "instance_port, is_logged_in FROM profiles"
        )
        rows = []
        for row in cur.fetchall():
            rows.append({
                "company_name": row[0],
                "profile_name": row[1],
                "profile_id": row[2],
                "platform_url": row[3],
                "instance_port": row[4],
                "is_logged_in": row[5],
            })
        conn.close()
        return rows
    except Exception as e:
        print(f"error: failed to read profiles table: {e}", file=sys.stderr)
        return []


def _update_instance_port(company_name, instance_port):
    """Update instance_port for a company in the profiles table."""
    db_path = _get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE profiles SET instance_port = ? WHERE company_name = ?",
            (str(instance_port), company_name)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"error: failed to update instance_port: {e}", file=sys.stderr)
        return False


def _start_instance_for_profile(profile_name, profile_id):
    """Start a PinchTab instance for a profile via Server API.
    Returns the allocated port number, or None on failure."""
    cfg = _read_config()
    token = cfg["server"]["token"]
    server_port = cfg["server"]["port"]

    # Use profile_id if known, otherwise try to look it up
    pid = profile_id
    if not pid:
        # Query profiles API to find the profile ID
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{server_port}/profiles",
                headers={"Authorization": f"Bearer {token}"}
            )
            resp = urllib.request.urlopen(req, timeout=10)
            profiles = json.loads(resp.read().decode("utf-8"))
            for p in profiles:
                if p.get("name") == profile_name:
                    pid = p.get("id")
                    break
        except Exception:
            pass

    if not pid:
        print(f"error: cannot find profile_id for '{profile_name}'", file=sys.stderr)
        return None

    try:
        data = json.dumps({"headless": True}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{server_port}/profiles/{pid}/start",
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read().decode("utf-8"))
        # Response may contain port or instance info
        port = result.get("port") or result.get("instancePort")
        if port:
            return int(port)
    except Exception as e:
        print(f"error: failed to start instance for '{profile_name}': {e}", file=sys.stderr)

    return None


# ── subcommands ─────────────────────────────────────────────────

def cmd_init():
    """Create a new tab, register it, and output export commands.
    With --json: output JSON for programmatic use.
    With --project-root <dir>: use <dir> as registry root instead of cwd.
    With --instance-port <port>: create tab on a specific PinchTab instance."""
    label = ""
    as_json = False
    project_root = None
    instance_port = None
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--label" and i + 1 < len(args):
            label = args[i + 1]; i += 2
        elif args[i] == "--project-root" and i + 1 < len(args):
            project_root = args[i + 1]; i += 2
        elif args[i] == "--instance-port" and i + 1 < len(args):
            instance_port = args[i + 1]; i += 2
        elif args[i] == "--json":
            as_json = True; i += 1
        else:
            i += 1

    if not label:
        label = f"session_{os.getpid()}"

    # Override registry path if --project-root is given
    if project_root:
        global _REGISTRY_DIR, _REGISTRY_PATH
        _REGISTRY_DIR = os.path.join(project_root, "violation_query", "data")
        _REGISTRY_PATH = os.path.join(_REGISTRY_DIR, "tab_registry.json")

    tab_id = _create_tab(instance_port=instance_port)
    registry = _read_registry()

    # Clean ghost entries (tabs closed externally, e.g. browser crash)
    registry, ghosts = _clean_ghost_entries(registry, instance_port=instance_port)
    if ghosts:
        _write_registry(registry)

    # Remove any existing entry with same label
    registry.pop(label, None)

    entry = {
        "tab_id": tab_id,
        "pid": os.getpid(),
        "created_at": datetime.now().isoformat(),
    }
    if instance_port:
        entry["instance_port"] = instance_port

    registry[label] = entry
    _write_registry(registry)

    if as_json:
        result = {"tab_id": tab_id, "ok": True}
        if instance_port:
            result["instance_port"] = instance_port
        print(json.dumps(result, ensure_ascii=False))
    else:
        _export_env(tab_id, instance_port=instance_port)


def cmd_bind():
    """Bind current process to an existing tab."""
    tab_id = ""
    label = ""
    instance_port = None
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--tab-id" and i + 1 < len(args):
            tab_id = args[i + 1]; i += 2
        elif args[i] == "--label" and i + 1 < len(args):
            label = args[i + 1]; i += 2
        elif args[i] == "--instance-port" and i + 1 < len(args):
            instance_port = args[i + 1]; i += 2
        else:
            i += 1

    if not tab_id:
        print("error: --tab-id is required", file=sys.stderr)
        sys.exit(1)
    if not label:
        label = f"session_{os.getpid()}"

    registry = _read_registry()
    entry = {
        "tab_id": tab_id,
        "pid": os.getpid(),
        "bound_at": datetime.now().isoformat(),
    }
    if instance_port:
        entry["instance_port"] = instance_port

    registry[label] = entry
    _write_registry(registry)
    _export_env(tab_id, instance_port=instance_port)


def cmd_release():
    """Remove a session from the registry and close its browser tab."""
    label = ""
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--label" and i + 1 < len(args):
            label = args[i + 1]; i += 2
        else:
            i += 1

    if not label:
        label = f"session_{os.getpid()}"

    registry = _read_registry()
    if label in registry:
        entry = registry[label]
        tab_id = entry.get("tab_id", "")
        instance_port = entry.get("instance_port")

        # Close the browser tab
        if tab_id:
            try:
                pt = _get_pinchtab_binary()
                cmd = [pt, "close", tab_id]
                if instance_port:
                    cmd.insert(1, f"http://127.0.0.1:{instance_port}")
                    cmd.insert(1, "--server")
                subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10)
            except Exception:
                pass

        del registry[label]
        _write_registry(registry)
    _unset_env()


def cmd_list():
    """List all registered sessions."""
    registry = _read_registry()
    if not registry:
        print("(no sessions registered)")
        return
    print(f"{'LABEL':<30} {'TAB_ID':<34} {'INSTANCE':<10} {'PID':<8} {'CREATED'}")
    print("-" * 110)
    for label, info in sorted(registry.items()):
        tab_id = info.get("tab_id", "")[:12] + "..."
        pid = info.get("pid", "?")
        instance = info.get("instance_port", "default")
        created = info.get("created_at", info.get("bound_at", ""))[:19]
        print(f"{label:<30} {tab_id:<34} {str(instance):<10} {str(pid):<8} {created}")


def cmd_cleanup_stale():
    """Garbage-collect zombie tabs: close tabs whose task is done or timed out.

    Three-tier detection (NO PID check — Claude sessions live forever):

      Tier 1  Completion marker:  .task_done_<tab_id>.json exists
              → task explicitly ended, safe to clean.

      Tier 2  Progress file active:  details_progress_*.json modified
              within --idle-hours (default 2h)
              → batch task still running, KEEP ALL tabs (conservative —
                a single active batch job blocks GC for every tab).

      Tier 3  Created-at timeout:  tab older than --max-age-hours (default 6h)
              with no completion marker and no active progress
              → zombie, safe to clean.

    Options:
      --idle-hours <n>       Progress-file idle threshold (default 2)
      --max-age-hours <n>    Absolute age threshold (default 6)
      --instance-port <p>    Only clean tabs on a specific instance
      --project-root <dir>   Use <dir> as project root instead of cwd
      --dry-run              Report what would be cleaned without doing it

    Outputs JSON: {ok, cleaned: [...], kept: [...], summary}
    """
    idle_hours = 2.0
    max_age_hours = 6.0
    instance_filter = None
    dry_run = False
    project_root = None
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--idle-hours" and i + 1 < len(args):
            try: idle_hours = float(args[i + 1])
            except ValueError: pass
            i += 2
        elif args[i] == "--max-age-hours" and i + 1 < len(args):
            try: max_age_hours = float(args[i + 1])
            except ValueError: pass
            i += 2
        elif args[i] == "--instance-port" and i + 1 < len(args):
            try: instance_filter = int(args[i + 1])
            except ValueError: pass
            i += 2
        elif args[i] == "--project-root" and i + 1 < len(args):
            project_root = args[i + 1]; i += 2
        elif args[i] == "--dry-run":
            dry_run = True; i += 1
        else:
            i += 1

    # Override registry/data paths if --project-root given
    root = project_root or os.getcwd()
    data_dir = os.path.join(root, "violation_query", "data")
    registry_path = os.path.join(data_dir, "tab_registry.json")

    if project_root:
        os.makedirs(data_dir, exist_ok=True)
        if os.path.exists(registry_path):
            try:
                with open(registry_path, "r", encoding="utf-8") as f:
                    registry = json.load(f)
            except Exception:
                registry = {}
        else:
            registry = {}
    else:
        registry = _read_registry()

    if not registry:
        print(json.dumps({"ok": True, "cleaned": [], "kept": [],
                          "summary": "empty registry"},
                         ensure_ascii=False))
        return

    now = datetime.now()
    cleaned = []
    kept = []

    # ── Pre-scan: find newest progress-file mtime, scoped to instance ──
    progress_newest_mtime = 0.0
    try:
        if instance_filter is not None:
            # Only consider progress files for this instance
            port_suffix = f"_{instance_filter}.json"
            for fname in os.listdir(data_dir):
                if fname.startswith("details_progress_") and fname.endswith(port_suffix):
                    fp = os.path.join(data_dir, fname)
                    try:
                        mtime = os.path.getmtime(fp)
                        if mtime > progress_newest_mtime:
                            progress_newest_mtime = mtime
                    except OSError:
                        pass
        else:
            # No instance filter — scan all (backward compat)
            for fname in os.listdir(data_dir):
                if fname.startswith("details_progress_") and fname.endswith(".json"):
                    fp = os.path.join(data_dir, fname)
                    try:
                        mtime = os.path.getmtime(fp)
                        if mtime > progress_newest_mtime:
                            progress_newest_mtime = mtime
                    except OSError:
                        pass
    except OSError:
        pass

    progress_idle_sec = idle_hours * 3600
    max_age_sec = max_age_hours * 3600
    pt_bin = _get_pinchtab_binary()

    for label, info in list(registry.items()):
        tab_id = info.get("tab_id", "")
        instance_port = info.get("instance_port")
        created_str = info.get("created_at", info.get("bound_at", ""))

        # ── Instance filter ──
        if instance_filter is not None:
            entry_port = instance_port
            if entry_port is not None:
                try: entry_port = int(entry_port)
                except (ValueError, TypeError): pass
            if entry_port != instance_filter:
                kept.append({"label": label, "reason": "instance_mismatch"})
                continue

        # ── Tier 1: Completion marker ──
        marker_file = os.path.join(data_dir, f".task_done_{tab_id}.json")
        if os.path.exists(marker_file):
            if not dry_run and tab_id:
                _close_tab(pt_bin, tab_id, instance_port)
                del registry[label]
            cleaned.append({
                "label": label, "tab_id": tab_id, "instance_port": instance_port,
                "reason": "completion_marker",
            })
            continue

        # ── Tier 2: Active progress file ──
        if progress_newest_mtime > 0:
            idle_sec = now.timestamp() - progress_newest_mtime
            if idle_sec < progress_idle_sec:
                kept.append({
                    "label": label,
                    "tab_id": tab_id[:12] + "..." if tab_id else "",
                    "reason": f"progress_active (idle {idle_sec/60:.0f}m < {idle_hours}h)",
                })
                continue

        # ── Tier 3: Created-at timeout ──
        age_sec = None
        if created_str:
            try:
                created = datetime.strptime(created_str[:19], "%Y-%m-%dT%H:%M:%S")
                age_sec = (now - created).total_seconds()
            except (ValueError, TypeError):
                pass

        if age_sec is not None and age_sec > max_age_sec:
            if not dry_run and tab_id:
                _close_tab(pt_bin, tab_id, instance_port)
                del registry[label]
            cleaned.append({
                "label": label, "tab_id": tab_id, "instance_port": instance_port,
                "reason": f"age_timeout ({age_sec/3600:.1f}h > {max_age_hours}h)",
            })
        else:
            age_str = f"{age_sec/3600:.1f}h" if age_sec is not None else "unknown"
            kept.append({
                "label": label, "tab_id": tab_id[:12] + "..." if tab_id else "",
                "reason": f"too_young (age {age_str} < {max_age_hours}h, no marker, no progress)",
            })

    # ── Write back registry ──
    if cleaned and not dry_run:
        _write_registry_data(data_dir, registry)

    print(json.dumps({
        "ok": True,
        "dry_run": dry_run,
        "idle_hours": idle_hours,
        "max_age_hours": max_age_hours,
        "cleaned": cleaned,
        "kept": kept,
        "summary": f"{len(cleaned)} stale tabs cleaned, {len(kept)} active tabs kept",
    }, ensure_ascii=False))


def _close_tab(pt_bin, tab_id, instance_port):
    """Close a single browser tab via pinchtab. Best-effort, never throws."""
    try:
        cmd = [pt_bin]
        if instance_port:
            cmd += ["--server", f"http://127.0.0.1:{instance_port}"]
        cmd += ["close", tab_id]
        # Use subprocess directly — NO _run() to avoid double --server injection
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        pass


def _write_registry_data(data_dir, registry):
    """Write registry dict to tab_registry.json under data_dir. Best-effort."""
    try:
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, "tab_registry.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(registry, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def cmd_current():
    """Print current VIOLATION_TAB_ID and VIOLATION_INSTANCE_PORT env values,
    or lookup by PID."""
    current_tab = os.environ.get("VIOLATION_TAB_ID", "")
    current_instance = os.environ.get("VIOLATION_INSTANCE_PORT", "")
    if current_tab:
        print(f"VIOLATION_TAB_ID={current_tab}")
        if current_instance:
            print(f"VIOLATION_INSTANCE_PORT={current_instance}")
        return

    # Try to find by PID in registry
    registry = _read_registry()
    pid = str(os.getpid())
    for label, info in registry.items():
        if str(info.get("pid", "")) == pid:
            print(f"VIOLATION_TAB_ID={info.get('tab_id', '')}  # {label}")
            ip = info.get("instance_port", "")
            if ip:
                print(f"VIOLATION_INSTANCE_PORT={ip}")
            return
    print("VIOLATION_TAB_ID=(not set)")
    print("VIOLATION_INSTANCE_PORT=(not set)")


# ── instance management subcommands ──────────────────────────────

def cmd_instance_discover():
    """Discover running PinchTab instances and sync to profiles table.

    Matches running instances to profiles by profile_name, updates
    instance_port in the SQLite profiles table.  For profiles without
    a running instance, attempts to start one via the PinchTab Server API.

    Outputs JSON with bound/unbound/errors lists."""
    instances = _get_running_instances()
    profiles = _read_profiles_table()

    # Build map: profile_name -> port for running instances
    running_map = {}
    for inst in instances:
        pname = inst.get("profileName", "")
        port = inst.get("port", "")
        status = inst.get("status", "")
        if pname and port and status == "running":
            running_map[pname] = int(port)

    bound = []
    unbound = []
    errors = []

    for prof in profiles:
        pname = prof["profile_name"]
        company = prof["company_name"]

        if pname in running_map:
            port = running_map[pname]
            if _update_instance_port(company, port):
                bound.append({
                    "company": company,
                    "instance_port": port,
                    "profile_name": pname,
                })
            else:
                errors.append({
                    "company": company,
                    "error": f"failed to update instance_port={port} in DB",
                })
        else:
            # No running instance — try to start one
            new_port = _start_instance_for_profile(pname, prof["profile_id"])
            if new_port:
                if _update_instance_port(company, new_port):
                    bound.append({
                        "company": company,
                        "instance_port": new_port,
                        "profile_name": pname,
                        "started": True,
                    })
                else:
                    errors.append({
                        "company": company,
                        "error": f"instance started on {new_port} but DB update failed",
                    })
            else:
                unbound.append({
                    "company": company,
                    "profile_name": pname,
                    "platform_url": prof["platform_url"],
                    "error": "no running instance and auto-start failed",
                })

    print(json.dumps({
        "bound": bound,
        "unbound": unbound,
        "errors": errors,
    }, ensure_ascii=False))


def cmd_instance_status():
    """Check if a company's PinchTab instance is running.

    --company <name>: look up from profiles table
    --instance-port <port>: check directly

    Outputs JSON: {company, instance_port, running, profile_name}"""
    company = ""
    instance_port = None
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            company = args[i + 1]; i += 2
        elif args[i] == "--instance-port" and i + 1 < len(args):
            instance_port = args[i + 1]; i += 2
        else:
            i += 1

    profile_name = None

    if company and not instance_port:
        profiles = _read_profiles_table()
        for p in profiles:
            if p["company_name"] == company:
                instance_port = p["instance_port"]
                profile_name = p["profile_name"]
                break
        if not instance_port:
            print(json.dumps({
                "company": company,
                "instance_port": None,
                "running": False,
                "error": "company not found or instance_port not set",
            }, ensure_ascii=False))
            return

    if not instance_port:
        print(json.dumps({
            "running": False,
            "error": "no instance_port or company specified",
        }, ensure_ascii=False))
        return

    instances = _get_running_instances()
    running = False
    for inst in instances:
        if str(inst.get("port", "")) == str(instance_port) and inst.get("status") == "running":
            running = True
            if not profile_name:
                profile_name = inst.get("profileName", "")
            break

    result = {
        "running": running,
        "instance_port": int(instance_port) if instance_port else None,
    }
    if company:
        result["company"] = company
    if profile_name:
        result["profile_name"] = profile_name

    print(json.dumps(result, ensure_ascii=False))


def cmd_profile_create():
    """Create a new PinchTab Profile + start Instance for a new company.

    Auto-generates numeric profile names (profile_001, profile_002, ...)
    that are independent of the company name (which is only known after
    scanning the QR code and reading the platform's company list).

    Writes a placeholder row to SQLite profiles table with company_name=NULL.

    Options:
      --company <name>       Optional: pre-fill company name hint
      --platform-url <url>   Optional: 12123 platform URL for this company

    Outputs JSON: {ok, profile_id, profile_name, instance_port}"""
    company = ""
    platform_url = ""
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            company = args[i + 1]; i += 2
        elif args[i] == "--platform-url" and i + 1 < len(args):
            platform_url = args[i + 1]; i += 2
        else:
            i += 1

    cfg = _read_config()
    token = cfg["server"]["token"]
    server_port = cfg["server"]["port"]

    # ── Determine next numeric profile name ──
    # Query both PinchTab profiles + SQLite profiles to find max number
    max_n = 0
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server_port}/profiles",
            headers={"Authorization": f"Bearer {token}"}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        pt_profiles = json.loads(resp.read().decode("utf-8"))
        for p in pt_profiles:
            name = p.get("name", "")
            if name.startswith("profile_"):
                try:
                    n = int(name.split("_")[1])
                    if n > max_n:
                        max_n = n
                except (ValueError, IndexError):
                    pass
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"failed to list PinchTab profiles: {e}"},
                         ensure_ascii=False))
        sys.exit(1)

    # Also check SQLite profiles for any profile_NNN names
    try:
        db_path = _get_db_path()
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT profile_name FROM profiles WHERE profile_name LIKE 'profile_%'"
            ).fetchall()
            for (name,) in rows:
                try:
                    n = int(name.split("_")[1])
                    if n > max_n:
                        max_n = n
                except (ValueError, IndexError):
                    pass
            conn.close()
    except Exception:
        pass

    profile_name = f"profile_{max_n + 1:03d}"

    # ── Step 1: Create PinchTab profile ──
    try:
        data = json.dumps({
            "name": profile_name,
            "path": os.path.join(
                os.path.expanduser("~"), ".pinchtab", "profiles", profile_name
            ),
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{server_port}/profiles",
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read().decode("utf-8"))
        profile_id = result.get("id", "")
        if not profile_id:
            print(json.dumps({"ok": False, "error": f"no profile id in response: {result}"},
                             ensure_ascii=False))
            sys.exit(1)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"failed to create profile '{profile_name}': {e}"},
                         ensure_ascii=False))
        sys.exit(1)

    # ── Step 2: Start instance (headless) ──
    try:
        data = json.dumps({"headless": True}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{server_port}/profiles/{profile_id}/start",
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read().decode("utf-8"))
        instance_port = result.get("port", "")
        if not instance_port:
            print(json.dumps({"ok": False, "error": f"no port in start response: {result}"},
                             ensure_ascii=False))
            sys.exit(1)
    except Exception as e:
        # Clean up the profile we just created
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{server_port}/profiles/{profile_id}",
                headers={"Authorization": f"Bearer {token}"},
                method="DELETE",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass
        print(json.dumps({"ok": False, "error": f"failed to start instance: {e}"},
                         ensure_ascii=False))
        sys.exit(1)

    # ── Step 3: Write placeholder to SQLite profiles table ──
    try:
        db_path = _get_db_path()
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO profiles (company_name, profile_name, profile_id,
               platform_url, instance_port, is_logged_in, last_login)
               VALUES (?, ?, ?, ?, ?, 0, NULL)""",
            (company or None, profile_name, profile_id,
             platform_url or "", str(instance_port))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"profile created but DB write failed: {e}"},
                         ensure_ascii=False))
        sys.exit(1)

    print(json.dumps({
        "ok": True,
        "profile_id": profile_id,
        "profile_name": profile_name,
        "instance_port": int(instance_port),
    }, ensure_ascii=False))


# ── main ────────────────────────────────────────────────────────

COMMANDS = {
    "init": cmd_init,
    "bind": cmd_bind,
    "release": cmd_release,
    "list": cmd_list,
    "cleanup-stale": cmd_cleanup_stale,
    "current": cmd_current,
    "instance-discover": cmd_instance_discover,
    "instance-status": cmd_instance_status,
    "profile-create": cmd_profile_create,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: {sys.argv[0]} <{'|'.join(COMMANDS)}> [options]", file=sys.stderr)
        print(f"  init          --label <name> [--instance-port <port>]  Create tab + register + output env", file=sys.stderr)
        print(f"  bind          --tab-id <id> --label <name>             Bind to existing tab", file=sys.stderr)
        print(f"  release       --label <name>                           Remove from registry + unset env", file=sys.stderr)
        print(f"  list                                                    List all sessions", file=sys.stderr)
        print(f"  cleanup-stale [--idle-hours <n>] [--max-age-hours <n>] [--dry-run]  GC zombie tabs", file=sys.stderr)
        print(f"  current                                                 Show current bindings", file=sys.stderr)
        print(f"  instance-discover                                       Discover instances → sync to profiles DB", file=sys.stderr)
        print(f"  instance-status   --company <name>                       Check if company's instance is running", file=sys.stderr)
        print(f"  profile-create    [--company <name>] [--platform-url <url>]  Create new Profile + Instance (new company)", file=sys.stderr)
        sys.exit(1)
    COMMANDS[sys.argv[1]]()

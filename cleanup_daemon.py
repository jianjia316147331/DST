#!/usr/bin/env python3
"""
cleanup_daemon.py — Independent cleanup daemon for DST violation query skill.

Runs as systemd oneshot service (via cleanup-dst.timer, hourly).
Four-phase cleanup: zombie tab GC → stale file cleanup → ghost registry cleanup → health report.

Usage:
  python3 cleanup_daemon.py --project-root <dir> [--dry-run] [--status]

Safety:
  - Tabs with last_activity < 2h are preserved (active query task)
  - Keepalive tabs with last_check < 2h are preserved (active keepalive daemon)
  - --dry-run mode reports only, no actual deletion
  - Each cleanup item is independently try/except wrapped
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

# ── Constants ──────────────────────────────────────────────────────

IDLE_HOURS = 2          # Tab inactivity threshold
LOG_MAX_LINES = 3000    # Log truncation size (~15-30h of keepalive logs)
LOG_MAX_BYTES = 1_048_576  # Log size threshold (1 MB)
SCREENSHOT_MAX_AGE_DAYS = 1
TASK_DONE_MAX_AGE_DAYS = 7
RECOVERY_QR_MAX_AGE_DAYS = 1
DIALOG_DEBUG_MAX_AGE_DAYS = 7
LOG_MAX_AGE_DAYS = 30
PROGRESS_KEEP_COUNT = 3

# ── Helpers ─────────────────────────────────────────────────────────

def _now():
    return datetime.now()


def _ts():
    return _now().strftime("%Y-%m-%dT%H:%M:%S")


def _file_age_hours(path):
    """Return file age in hours, or None if file doesn't exist."""
    try:
        mtime = os.path.getmtime(path)
        return (_now() - datetime.fromtimestamp(mtime)).total_seconds() / 3600
    except OSError:
        return None


def _file_age_days(path):
    """Return file age in days, or None if file doesn't exist."""
    h = _file_age_hours(path)
    return h / 24 if h is not None else None


def _file_size(path):
    """Return file size in bytes, or 0 if file doesn't exist."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _read_json(path):
    """Read a JSON file, return dict/list or None on failure."""
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return None


def _write_json(path, data):
    """Write data as JSON, atomically via temp file."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _glob_files(data_dir, pattern):
    """Yield (filename, full_path) tuples matching a glob-like prefix pattern.
    pattern: literal prefix like 'details_progress_' or 'keepalive_health_'."""
    try:
        entries = os.listdir(data_dir)
    except OSError:
        return
    for name in sorted(entries):
        if name.startswith(pattern):
            yield name, os.path.join(data_dir, name)


def _safe_name(company):
    """Sanitize company name for use in filenames."""
    return re.sub(r"[^a-zA-Z0-9一-鿿_-]", "_", company)


def _extract_company_from_filename(name, prefix):
    """Extract company name from keepalive filename pattern.
    e.g. keepalive_health_成都新创绿能.json → 成都新创绿能"""
    # Remove prefix and extension
    core = name[len(prefix):]
    # Remove common extensions
    for ext in [".json", ".txt", ".pid", ".log"]:
        if core.endswith(ext):
            core = core[: -len(ext)]
            break
    return core


def _pid_alive(pid):
    """Check if a process with given PID exists."""
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, ProcessLookupError):
        return False


def _run_pinchtab(args, instance_port=None, timeout=15):
    """Run a pinchtab command, return subprocess.CompletedProcess."""
    pt_path = None
    for name in ["pinchtab", "pinchtab.exe"]:
        for d in os.environ.get("PATH", "").split(os.pathsep):
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                pt_path = p
                break
        if pt_path:
            break
    if not pt_path:
        pt_path = "pinchtab"

    cmd = [pt_path]
    if instance_port:
        cmd += ["--server", f"http://127.0.0.1:{instance_port}"]
    cmd += list(args)

    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding="utf-8", timeout=timeout)
    except Exception as e:
        result = subprocess.CompletedProcess(cmd, -1, "", str(e))
        return result


def _get_active_tab_ids(instance_port=None):
    """Query PinchTab for active tab IDs."""
    result = _run_pinchtab(["tab"], instance_port=instance_port)
    try:
        data = json.loads(result.stdout)
        return {t["id"] for t in data.get("tabs", []) if t.get("id")}
    except Exception:
        return set()


def _close_tab(tab_id, instance_port=None):
    """Close a browser tab via PinchTab DELETE /tab/<id>."""
    return _run_pinchtab(["close", tab_id], instance_port=instance_port)


# ── Phase 1: Zombie Tab GC ──────────────────────────────────────────

def _phase1_zombie_tabs(data_dir, profiles_db, dry_run, log):
    """Detect and close zombie tabs (both query and keepalive).

    Returns dict with counts: {query_cleaned, query_kept, keepalive_cleaned, keepalive_kept, errors}
    """
    result = {"query_cleaned": 0, "query_kept": 0,
              "keepalive_cleaned": 0, "keepalive_kept": 0,
              "errors": []}

    # ── Query tabs (tab_registry.json) ──
    registry_path = os.path.join(data_dir, "tab_registry.json")
    registry = _read_json(registry_path) or {}
    cleaned_registry = {}

    for label, info in registry.items():
        tab_id = info.get("tab_id", "")
        instance_port = info.get("instance_port")
        last_activity = info.get("last_activity")
        created_at = info.get("created_at")

        # Determine if active
        is_active = False
        if last_activity:
            try:
                last_dt = datetime.strptime(last_activity, "%Y-%m-%dT%H:%M:%S")
                is_active = (_now() - last_dt).total_seconds() < IDLE_HOURS * 3600
            except ValueError:
                pass
        elif created_at:
            try:
                # Fallback: use created_at if no last_activity
                last_dt = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S")
                is_active = (_now() - last_dt).total_seconds() < IDLE_HOURS * 3600
            except ValueError:
                pass

        if is_active:
            result["query_kept"] += 1
            cleaned_registry[label] = info
            log(f"  [Phase 1] query tab '{label}' active (last_activity={last_activity}), keeping")
            continue

        # Zombie: close tab and remove from registry
        log(f"  [Phase 1] query tab '{label}' zombie (last_activity={last_activity}, created_at={created_at}), closing")
        if tab_id and not dry_run:
            r = _close_tab(tab_id, instance_port=instance_port)
            if r.returncode != 0:
                result["errors"].append(f"Failed to close query tab {tab_id}: {r.stderr}")
        result["query_cleaned"] += 1
        # Not added to cleaned_registry → removed

    # Write back cleaned registry
    if result["query_cleaned"] > 0:
        if not dry_run:
            _write_json(registry_path, cleaned_registry)
        log(f"  [Phase 1] registry updated: {result['query_cleaned']} tabs removed")

    # ── Keepalive tabs (keepalive_tab_*.txt) ──
    profiles = _read_profiles(profiles_db)

    for name, path in _glob_files(data_dir, "keepalive_tab_"):
        if not name.endswith(".txt"):
            continue
        company = _extract_company_from_filename(name, "keepalive_tab_")
        try:
            with open(path, "r", encoding="utf-8") as f:
                tab_id = f.read().strip()
        except OSError:
            continue

        if not tab_id:
            continue

        # Check if company is still active
        profile = profiles.get(company)
        is_logged_in = profile["is_logged_in"] if profile else False
        instance_port = profile["instance_port"] if profile else None

        # Check keepalive health for last_check
        safe = _safe_name(company)
        health_path = os.path.join(data_dir, f"keepalive_health_{safe}.json")
        health = _read_json(health_path)
        last_check = health.get("last_check") if health else None

        # ── Check logged-out first (highest priority) ──
        if not is_logged_in:
            log(f"  [Phase 1] keepalive tab '{company}' logged out, closing")
            if tab_id and not dry_run:
                r = _close_tab(tab_id, instance_port=instance_port)
                if r.returncode != 0:
                    result["errors"].append(f"Failed to close keepalive tab {tab_id}: {r.stderr}")
            if not dry_run:
                try:
                    os.remove(path)
                except OSError as e:
                    result["errors"].append(f"Failed to remove {path}: {e}")
            # Clean all residue files for logged-out company
            for prefix in ["keepalive_", "keepalive_health_", "keepalive_notify_"]:
                suffix_map = {"keepalive_": ".pid", "keepalive_health_": ".json",
                              "keepalive_notify_": ".json"}
                fp = os.path.join(data_dir, f"{prefix}{safe}{suffix_map[prefix]}")
                if os.path.exists(fp) and fp != health_path:
                    try:
                        if not dry_run:
                            os.remove(fp)
                        log(f"  [Phase 1] cleaned logged-out residue: {os.path.basename(fp)}")
                    except OSError:
                        pass
            result["keepalive_cleaned"] += 1
            continue

        # ── Then check keepalive health ──
        keepalive_active = False
        if last_check:
            try:
                # last_check format: "2026-07-14 15:30:00"
                last_dt = datetime.strptime(last_check, "%Y-%m-%d %H:%M:%S")
                keepalive_active = (_now() - last_dt).total_seconds() < IDLE_HOURS * 3600
            except ValueError:
                pass

        # Also check PID
        pid_path = os.path.join(data_dir, f"keepalive_{safe}.pid")
        pid_alive = False
        try:
            with open(pid_path, "r", encoding="utf-8") as f:
                pid_alive = _pid_alive(int(f.read().strip()))
        except (OSError, ValueError):
            pass

        if keepalive_active and pid_alive:
            result["keepalive_kept"] += 1
            log(f"  [Phase 1] keepalive tab '{company}' active (last_check={last_check}), keeping")
            continue

        # Zombie
        if not pid_alive:
            reason = "pid_dead"
        else:
            reason = f"idle > {IDLE_HOURS}h"

        log(f"  [Phase 1] keepalive tab '{company}' zombie ({reason}), closing")
        if tab_id and not dry_run:
            r = _close_tab(tab_id, instance_port=instance_port)
            if r.returncode != 0:
                result["errors"].append(f"Failed to close keepalive tab {tab_id}: {r.stderr}")

        # Clean txt file
        if not dry_run:
            try:
                os.remove(path)
            except OSError as e:
                result["errors"].append(f"Failed to remove {path}: {e}")

        result["keepalive_cleaned"] += 1

    return result


# ── Phase 2: Stale File Cleanup ─────────────────────────────────────

def _get_active_companies(data_dir, profiles_db):
    """Return set of company names that have active query or keepalive tabs."""
    active = set()

    # Check query tabs
    registry = _read_json(os.path.join(data_dir, "tab_registry.json")) or {}
    for info in registry.values():
        last_activity = info.get("last_activity")
        if last_activity:
            try:
                last_dt = datetime.strptime(last_activity, "%Y-%m-%dT%H:%M:%S")
                if (_now() - last_dt).total_seconds() < IDLE_HOURS * 3600:
                    active.add(info.get("company", ""))
            except ValueError:
                pass

    # Check keepalive tabs
    profiles = _read_profiles(profiles_db)
    for name, path in _glob_files(data_dir, "keepalive_health_"):
        if not name.endswith(".json"):
            continue
        company = _extract_company_from_filename(name, "keepalive_health_")
        health = _read_json(path)
        if not health:
            continue
        last_check = health.get("last_check")
        profile = profiles.get(company)
        if last_check and profile and profile["is_logged_in"]:
            try:
                last_dt = datetime.strptime(last_check, "%Y-%m-%d %H:%M:%S")
                if (_now() - last_dt).total_seconds() < IDLE_HOURS * 3600:
                    active.add(company)
            except ValueError:
                pass

    return active


def _phase2_stale_files(data_dir, profiles_db, dry_run, log):
    """Clean stale/expired files.

    Returns dict with per-item counts.
    """
    result = {
        "screenshots": 0,
        "task_done_markers": 0,
        "recovery_qr": 0,
        "progress_files": 0,
        "violation_details": 0,
        "logged_out_residue": 0,
        "logs_truncated": 0,
        "dialog_debug": 0,
        "errors": [],
    }

    active_companies = _get_active_companies(data_dir, profiles_db)
    profiles = _read_profiles(profiles_db)

    # ── (a) screenshots/*.png > 1d ──
    screenshot_dir = os.path.join(data_dir, "screenshots")
    if os.path.isdir(screenshot_dir):
        for name in os.listdir(screenshot_dir):
            if not name.endswith(".png"):
                continue
            fp = os.path.join(screenshot_dir, name)
            age_days = _file_age_days(fp)
            if age_days is not None and age_days > SCREENSHOT_MAX_AGE_DAYS:
                log(f"  [Phase 2a] removing screenshot: {name} ({age_days:.1f}d old)")
                if not dry_run:
                    try:
                        os.remove(fp)
                    except OSError as e:
                        result["errors"].append(f"Failed to remove {fp}: {e}")
                        continue
                result["screenshots"] += 1

    # ── (b) .task_done_*.json > 7d ──
    for name, path in _glob_files(data_dir, ".task_done_"):
        if not name.endswith(".json"):
            continue
        age_days = _file_age_days(path)
        if age_days is not None and age_days > TASK_DONE_MAX_AGE_DAYS:
            log(f"  [Phase 2b] removing task_done marker: {name} ({age_days:.1f}d old)")
            if not dry_run:
                try:
                    os.remove(path)
                except OSError as e:
                    result["errors"].append(f"Failed to remove {path}: {e}")
                    continue
            result["task_done_markers"] += 1

    # ── (c) recovery_qr_*.png > 1d ──
    for name, path in _glob_files(data_dir, "recovery_qr_"):
        if not name.endswith(".png"):
            continue
        age_days = _file_age_days(path)
        if age_days is not None and age_days > RECOVERY_QR_MAX_AGE_DAYS:
            log(f"  [Phase 2c] removing recovery QR: {name} ({age_days:.1f}d old)")
            if not dry_run:
                try:
                    os.remove(path)
                except OSError as e:
                    result["errors"].append(f"Failed to remove {path}: {e}")
                    continue
            result["recovery_qr"] += 1

    # ── (d) details_progress_* keep latest 3 ──
    # Group by company prefix, then keep 3 newest per group
    _clean_grouped_files(data_dir, "details_progress_", PROGRESS_KEEP_COUNT,
                         active_companies, dry_run, log,
                         result, "progress_files")

    # ── (e) violation_details_* keep latest 3 ──
    _clean_grouped_files(data_dir, "violation_details_", PROGRESS_KEEP_COUNT,
                         active_companies, dry_run, log,
                         result, "violation_details")

    # ── (f) keepalive_*.log truncation >30d or >1MB ──
    for name, path in _glob_files(data_dir, "keepalive_"):
        if not name.endswith(".log"):
            continue
        size = _file_size(path)
        age_days = _file_age_days(path)
        needs_truncation = (size > LOG_MAX_BYTES) or (age_days is not None and age_days > LOG_MAX_AGE_DAYS)

        if needs_truncation:
            log(f"  [Phase 2f] truncating log: {name} ({size} bytes, {age_days:.1f}d old)")
            if not dry_run:
                try:
                    with open(path, "rb") as f:
                        # Read last LOG_MAX_LINES lines efficiently
                        f.seek(0, 2)  # end of file
                        fsize = f.tell()
                        # 3000 lines ~= 384KB at 128B/line; use 512KB buffer
                        chunk_start = max(0, fsize - 524288)  # last 512KB
                        f.seek(chunk_start)
                        chunk = f.read().decode("utf-8", errors="replace")
                        lines = chunk.splitlines()
                        if len(lines) > LOG_MAX_LINES:
                            lines = lines[-LOG_MAX_LINES:]
                        content = "\n".join(lines) + "\n"
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                except OSError as e:
                    result["errors"].append(f"Failed to truncate {path}: {e}")
                    continue
            result["logs_truncated"] += 1

    # ── (g) Logged-out company residue ──
    logged_out = set()
    for company, profile in profiles.items():
        if not profile["is_logged_in"]:
            logged_out.add(company)

    for name, path in _glob_files(data_dir, "keepalive_"):
        # Extract company from filename
        for prefix in ["keepalive_health_", "keepalive_notify_", "keepalive_tab_"]:
            if name.startswith(prefix) and name != f"{prefix}.json":
                break
        else:
            if name.startswith("keepalive_") and (name.endswith(".pid") or name.endswith(".log")):
                # keepalive_<company>.pid or keepalive_<company>.log
                pass
            else:
                continue

        # Get company name
        for prefix in ["keepalive_health_", "keepalive_notify_", "keepalive_tab_"]:
            if name.startswith(prefix):
                company = _extract_company_from_filename(name, prefix)
                break
        else:
            if name.endswith(".pid"):
                company = _extract_company_from_filename(name, "keepalive_")
            elif name.endswith(".log"):
                company = _extract_company_from_filename(name, "keepalive_")
            else:
                continue

        if company in logged_out:
            log(f"  [Phase 2g] removing logged-out residue: {name}")
            if not dry_run:
                try:
                    os.remove(path)
                except OSError as e:
                    result["errors"].append(f"Failed to remove {path}: {e}")
                    continue
            result["logged_out_residue"] += 1

    # ── (h) dialog_debug/ > 7d ──
    debug_dir = os.path.join(data_dir, "dialog_debug")
    if os.path.isdir(debug_dir):
        for name in os.listdir(debug_dir):
            fp = os.path.join(debug_dir, name)
            age_days = _file_age_days(fp)
            if age_days is not None and age_days > DIALOG_DEBUG_MAX_AGE_DAYS:
                log(f"  [Phase 2h] removing debug file: {name} ({age_days:.1f}d old)")
                if not dry_run:
                    try:
                        os.remove(fp)
                    except OSError as e:
                        result["errors"].append(f"Failed to remove {fp}: {e}")
                        continue
                result["dialog_debug"] += 1

    return result


def _clean_grouped_files(data_dir, prefix, keep_count, active_companies, dry_run, log, result, result_key):
    """Group files by company prefix, keep `keep_count` newest per group."""
    # Collect files by company
    groups = {}
    for name, path in _glob_files(data_dir, prefix):
        if not name.endswith(".json"):
            continue
        # Extract company: details_progress_<company>_<batch>.json or details_progress_<company>.json
        core = name[len(prefix):]
        # Try to extract company name (before _batch or before .json)
        parts = core.rsplit(".json", 1)[0].split("_")
        company = parts[0] if parts else ""
        if company not in groups:
            groups[company] = []
        mtime = os.path.getmtime(path)
        groups[company].append((mtime, path, name))

    for company, files in groups.items():
        files.sort(key=lambda x: x[0], reverse=True)  # newest first
        # Keep files belonging to active companies (if company matches)
        # Actually: skip files from active companies entirely to avoid interfering
        if company in active_companies:
            log(f"  [Phase 2] keeping all {prefix} files for active company '{company}'")
            continue

        # Delete all but the newest keep_count
        to_delete = files[keep_count:]
        for mtime, path, name in to_delete:
            age_days = (_now() - datetime.fromtimestamp(mtime)).total_seconds() / 86400
            log(f"  [Phase 2] removing {prefix}file: {name} ({age_days:.1f}d old)")
            if not dry_run:
                try:
                    os.remove(path)
                except OSError as e:
                    result["errors"].append(f"Failed to remove {path}: {e}")
                    continue
            result[result_key] += 1


# ── Phase 3: Ghost Registry Cleanup ─────────────────────────────────

def _phase3_ghost_registry(data_dir, profiles_db, dry_run, log):
    """Remove registry entries whose browser tabs no longer exist."""
    result = {"removed": 0, "errors": []}

    registry_path = os.path.join(data_dir, "tab_registry.json")
    registry = _read_json(registry_path)
    if not registry:
        return result

    # Get all active instance ports from profiles DB
    profiles = _read_profiles(profiles_db)
    ports = set()
    for profile in profiles.values():
        port = profile.get("instance_port")
        if port:
            ports.add(str(port))

    cleaned = {}
    for label, info in registry.items():
        tab_id = info.get("tab_id", "")
        instance_port = info.get("instance_port")
        if not tab_id:
            continue

        # Determine which port to check on
        port = str(instance_port) if instance_port else None
        if port is None and ports:
            # Try all known ports
            found = False
            for p in ports:
                active_ids = _get_active_tab_ids(instance_port=p)
                if tab_id in active_ids:
                    found = True
                    break
            if not found:
                log(f"  [Phase 3] ghost entry: '{label}' tab {tab_id} not found on any instance")
                result["removed"] += 1
                continue
        elif port:
            active_ids = _get_active_tab_ids(instance_port=port)
            if tab_id not in active_ids:
                log(f"  [Phase 3] ghost entry: '{label}' tab {tab_id} not found on port {port}")
                result["removed"] += 1
                continue

        cleaned[label] = info

    if result["removed"] > 0:
        if not dry_run:
            _write_json(registry_path, cleaned)
        log(f"  [Phase 3] cleaned {result['removed']} ghost entries")

    return result


# ── Profiles DB ─────────────────────────────────────────────────────

def _read_profiles(profiles_db):
    """Read all profiles from the DB. Returns {company_name: {is_logged_in, instance_port, ...}}."""
    profiles = {}
    try:
        if not os.path.exists(profiles_db):
            return profiles
        conn = sqlite3.connect(profiles_db)
        cur = conn.execute(
            "SELECT company_name, instance_port, is_logged_in FROM profiles")
        for row in cur.fetchall():
            profiles[row[0]] = {
                "instance_port": row[1],
                "is_logged_in": bool(row[2]),
            }
        conn.close()
    except Exception:
        pass
    return profiles


# ── Main ────────────────────────────────────────────────────────────

def main():
    project_root = None
    dry_run = False
    show_status = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--project-root" and i + 1 < len(args):
            project_root = args[i + 1]
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        elif args[i] == "--status":
            show_status = True
            i += 1
        else:
            i += 1

    if not project_root:
        print(json.dumps({"ok": False, "error": "--project-root is required"}, ensure_ascii=False))
        sys.exit(1)

    data_dir = os.path.join(project_root, "violation_query", "data")
    profiles_db = os.path.join(data_dir, "violations.db")
    health_path = os.path.join(data_dir, "cleanup_health.json")

    # ── Status mode: just print last health report ──
    if show_status:
        health = _read_json(health_path)
        if health:
            print(json.dumps(health, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"ok": True, "message": "no previous cleanup data"}, ensure_ascii=False))
        return

    # ── Log accumulator ──
    log_lines = []

    def log(msg):
        log_lines.append(msg)

    log(f"=== Cleanup daemon started at {_ts()} === (dry_run={dry_run})")
    log(f"  data_dir={data_dir}")

    # Ensure data_dir exists
    if not os.path.isdir(data_dir):
        log("  data_dir does not exist — nothing to clean")
        report = {"ok": True, "dry_run": dry_run, "timestamp": _ts(),
                  "phases": {}, "message": "data_dir not found"}
        _write_json(health_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    report = {
        "ok": True,
        "dry_run": dry_run,
        "timestamp": _ts(),
        "phases": {},
        "errors": [],
    }

    # ── Phase 1: Zombie Tab GC ──
    log("\n-- Phase 1: Zombie Tab GC --")
    p1 = _phase1_zombie_tabs(data_dir, profiles_db, dry_run, log)
    report["phases"]["zombie_tabs"] = p1

    # ── Phase 2: Stale File Cleanup ──
    log("\n-- Phase 2: Stale File Cleanup --")
    p2 = _phase2_stale_files(data_dir, profiles_db, dry_run, log)
    report["phases"]["stale_files"] = p2

    # ── Phase 3: Ghost Registry Cleanup ──
    log("\n-- Phase 3: Ghost Registry Cleanup --")
    p3 = _phase3_ghost_registry(data_dir, profiles_db, dry_run, log)
    report["phases"]["ghost_registry"] = p3

    # ── Collect errors ──
    for phase in [p1, p2, p3]:
        for err in phase.pop("errors", []):
            report["errors"].append(err)

    # ── Phase 4: Write health report ──
    log(f"\n=== Cleanup complete at {_ts()} ===")
    report["_log"] = log_lines
    _write_json(health_path, report)
    # Remove log from output for cleanliness
    del report["_log"]
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

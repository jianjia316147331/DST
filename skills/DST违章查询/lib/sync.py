#!/usr/bin/env python3
"""lib/sync.py — Export local SQLite data for sync to central console."""
import hashlib
import json
import os
import sqlite3
import sys

from .core import _get_data_dir, _read_stdin_json
from .db import _init_db, _get_db_path


def export_for_sync(company_name=None, query_date=None, since=None, node_id=None):
    """Export companies, vehicles, violations from local SQLite.

    Returns dict: { ok, node_id, companies, vehicles, violations }
    Companies/vehicles/violations are referenced by name (not numeric ID)
    since the IDs differ between local SQLite and central MySQL.
    Natural key hashes are pre-computed for MySQL upsert.
    """
    _init_db()
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        return {"ok": False, "error": "DB not found", "path": db_path}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Export companies
    conditions = []
    params = []
    if company_name:
        conditions.append("name = ?")
        params.append(company_name)
    if query_date:
        conditions.append("query_date = ?")
        params.append(query_date)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    cur = conn.execute(f"SELECT * FROM companies {where}", params)
    companies = [dict(r) for r in cur.fetchall()]

    if not companies:
        conn.close()
        return {"ok": True, "node_id": node_id, "companies": [], "vehicles": [], "violations": []}

    company_names = [c["name"] for c in companies]
    placeholders = ",".join("?" * len(company_names))

    # Export vehicles for these companies
    cur = conn.execute(
        f"SELECT v.*, c.name as company_name FROM vehicles v "
        f"JOIN companies c ON v.company_id = c.id "
        f"WHERE c.name IN ({placeholders})",
        company_names
    )
    vehicles = [dict(r) for r in cur.fetchall()]

    # Export violations — by company_name (join through vehicles)
    all_plates = [v["plate_number"] for v in vehicles]
    violations_out = []
    if all_plates:
        ph2 = ",".join("?" * len(all_plates))
        cur = conn.execute(
            f"SELECT vl.*, c.name as company_name "
            f"FROM violations vl "
            f"JOIN vehicles v ON vl.vehicle_id = v.id "
            f"JOIN companies c ON v.company_id = c.id "
            f"WHERE vl.plate_number IN ({ph2})",
            all_plates
        )
        for r in cur.fetchall():
            rec = dict(r)
            nk = f"{rec.get('plate_number','')}_{rec.get('violation_time','')}_{rec.get('violation_location','')}_{rec.get('violation_behavior','')}"
            rec["natural_key_hash"] = hashlib.md5(nk.encode("utf-8")).hexdigest()
            rec["node_id_str"] = node_id
            violations_out.append(rec)

    conn.close()
    return {
        "ok": True,
        "node_id": node_id,
        "companies": companies,
        "vehicles": vehicles,
        "violations": violations_out,
    }


def cmd_db_export_sync():
    """CLI entry: export local data as JSON for central console sync.

    Args (CLI): --company NAME [--query-date YYYY-MM-DD] [--since TS] [--node-id ID]
    Output: JSON on stdout.
    """
    p = {"company": "", "query_date": "", "since": "", "node_id": ""}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        elif args[i] == "--query-date" and i + 1 < len(args):
            p["query_date"] = args[i + 1]; i += 2
        elif args[i] == "--since" and i + 1 < len(args):
            p["since"] = args[i + 1]; i += 2
        elif args[i] == "--node-id" and i + 1 < len(args):
            p["node_id"] = args[i + 1]; i += 2
        else:
            i += 1

    result = export_for_sync(
        company_name=p["company"] or None,
        query_date=p["query_date"] or None,
        since=p["since"] or None,
        node_id=p["node_id"] or None,
    )
    print(json.dumps(result, ensure_ascii=False, default=str))


# ── Config management ──

def _get_config_path():
    return os.path.join(_get_data_dir(), "node_config.json")


def read_node_config():
    """Read node_config.json. Returns dict with keys: node_id, cloud_ws_url, version, setup_at."""
    config_path = _get_config_path()
    if not os.path.exists(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def write_node_config(node_id, cloud_ws_url):
    """Write node_config.json. Returns True on success."""
    from datetime import datetime
    config_path = _get_config_path()
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    config = {
        "node_id": node_id,
        "cloud_ws_url": cloud_ws_url,
        "version": "1.0.0",
        "setup_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    return True


def check_cloud_connectivity(cloud_ws_url, timeout=5):
    """Try a quick WebSocket/TCP check to the cloud server. Returns (ok, error_message)."""
    import socket
    try:
        # Parse host:port from ws:// URL
        url = cloud_ws_url.replace("ws://", "").replace("wss://", "")
        url = url.split("/")[0]  # strip path
        if ":" in url:
            host, port = url.rsplit(":", 1)
        else:
            host, port = url, 80
        port = int(port)
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True, None
    except Exception as e:
        return False, str(e)


def cmd_check_config():
    """Check if node is configured and cloud server is reachable.

    Output JSON: { configured, node_id, cloud_ws_url, connected, missing_fields, error }
    """
    config = read_node_config()
    if config is None:
        print(json.dumps({
            "configured": False,
            "node_id": None,
            "cloud_ws_url": None,
            "connected": False,
            "missing": ["node_config.json not found"],
            "error": "请先执行 python3 node_agent.py --setup 完成设备配置",
        }))
        return

    missing = []
    if not config.get("node_id"):
        missing.append("node_id")
    if not config.get("cloud_ws_url"):
        missing.append("cloud_ws_url")

    if missing:
        print(json.dumps({
            "configured": False,
            "node_id": config.get("node_id"),
            "cloud_ws_url": config.get("cloud_ws_url"),
            "connected": False,
            "missing": missing,
            "error": f"配置不完整，缺少: {', '.join(missing)}",
        }))
        return

    # Check connectivity
    ok, err = check_cloud_connectivity(config["cloud_ws_url"])
    print(json.dumps({
        "configured": True,
        "node_id": config["node_id"],
        "cloud_ws_url": config["cloud_ws_url"],
        "connected": ok,
        "missing": [],
        "error": None if ok else f"无法连接控制台: {err}",
    }))


def cmd_setup_config():
    """Write node configuration from CLI args.

    Args: --node-id <id> --cloud-ws-url <ws://host:port/ws?client=node>
    Output JSON: { ok, node_id, cloud_ws_url, connected, error }
    """
    p = {"node_id": "", "cloud_ws_url": ""}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--node-id" and i + 1 < len(args):
            p["node_id"] = args[i + 1]; i += 2
        elif args[i] == "--cloud-ws-url" and i + 1 < len(args):
            p["cloud_ws_url"] = args[i + 1]; i += 2
        else:
            i += 1

    if not p["node_id"]:
        print(json.dumps({"ok": False, "error": "缺少 --node-id"}))
        return
    if not p["cloud_ws_url"]:
        print(json.dumps({"ok": False, "error": "缺少 --cloud-ws-url"}))
        return

    write_node_config(p["node_id"], p["cloud_ws_url"])
    ok, err = check_cloud_connectivity(p["cloud_ws_url"])

    print(json.dumps({
        "ok": True,
        "node_id": p["node_id"],
        "cloud_ws_url": p["cloud_ws_url"],
        "connected": ok,
        "error": None if ok else f"配置已保存，但无法连接控制台: {err}。请检查地址是否正确，控制台是否已启动。",
    }))

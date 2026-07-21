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

    # Export violations — join through vehicles to companies.
    # Use company_names (typically 1-5) in the IN clause instead of all_plates
    # (which can be 2000+ for large companies, exceeding SQLite's 999 limit).
    violations_out = []
    if company_names:
        ph2 = ",".join("?" * len(company_names))
        cur = conn.execute(
            f"SELECT vl.*, c.name as company_name "
            f"FROM violations vl "
            f"JOIN vehicles v ON vl.vehicle_id = v.id "
            f"JOIN companies c ON v.company_id = c.id "
            f"WHERE c.name IN ({ph2})",
            company_names
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


# ── WebSocket push client (zero deps, sync) ──

def push_sync_to_console(sync_data, timeout=30):
    """Push sync_data to central console via WebSocket, return (ok, message).

    Opens a short-lived WebSocket connection, sends the sync_data message,
    waits for sync_ack response, then closes.  No asyncio dependency —
    pure synchronous socket I/O with select for timeout.
    """
    import base64 as b64
    import select
    import socket
    import struct
    import time

    config = read_node_config()
    if not config:
        return False, "node_config.json not found, 请先执行 --setup"

    ws_url = config.get("cloud_ws_url", "")
    if not ws_url:
        return False, "cloud_ws_url not configured"

    node_id = config.get("node_id", "")

    # Parse URL
    url = ws_url.replace("ws://", "").replace("wss://", "")
    if "/" in url:
        host_part, path = url.split("/", 1)
        path = "/" + path
    else:
        host_part = url
        path = "/"

    if ":" in host_part:
        host, port_str = host_part.rsplit(":", 1)
        port = int(port_str)
    else:
        host = host_part
        port = 80

    sock = None
    try:
        # Connect
        sock = socket.create_connection((host, port), timeout=10)

        # WebSocket handshake
        ws_key_bytes = os.urandom(16)
        ws_key = b64.b64encode(ws_key_bytes).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode())

        # Read handshake response
        sock.settimeout(5)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                return False, "Handshake: connection closed"
            response += chunk

        if b"101" not in response:
            return False, f"Handshake rejected: {response[:200]}"

        # Build and send sync_data frame (client → server, MUST mask)
        build_n_send = _build_and_send_frame(sock, {
            "type": "sync_data",
            "node_id": node_id,
            **{k: v for k, v in sync_data.items() if k != "ok" and k != "node_id"},
        })

        # Wait for sync_ack with timeout
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            ready, _, _ = select.select([sock], [], [], 1.0)
            if ready:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                # Try parsing complete frame(s)
                while len(buf) >= 2:
                    payload = _decode_ws_frame(buf)
                    if payload is None:
                        # Frame not complete yet, wait for more
                        break
                    buf = buf[_ws_frame_consumed(buf):]
                    try:
                        resp = json.loads(payload.decode("utf-8"))
                        if resp.get("type") == "sync_ack":
                            ok = resp.get("ok", False)
                            stats = resp.get("stats", "")
                            return ok, stats if ok else f"sync rejected: {stats}"
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
            # Timeout
            if time.time() >= deadline:
                return False, "Timed out waiting for sync_ack"

        return False, "Connection closed without sync_ack"

    except socket.timeout:
        return False, "Connection timed out"
    except ConnectionRefusedError:
        return False, f"Cannot connect to {host}:{port}"
    except Exception as e:
        return False, f"Push error: {e}"
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def _build_and_send_frame(sock, msg):
    """Build and send a single masked WebSocket text frame."""
    payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    frame = bytearray()
    frame.append(0x81)  # FIN + text opcode
    length = len(payload)
    if length < 126:
        frame.append(length | 0x80)  # MASK bit set
    elif length < 65536:
        frame.append(126 | 0x80)
        frame.extend(length.to_bytes(2, "big"))
    else:
        frame.append(127 | 0x80)
        frame.extend(length.to_bytes(8, "big"))
    mask_key = os.urandom(4)
    frame.extend(mask_key)
    frame.extend(bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload)))
    sock.sendall(bytes(frame))


# Track consumed bytes after frame decode for buffer management
_ws_last_consumed = 0


def _ws_frame_consumed(buf):
    return _ws_last_consumed


def _decode_ws_frame(buf):
    """Decode one WebSocket frame from buf. Returns payload bytes or None if incomplete."""
    global _ws_last_consumed
    _ws_last_consumed = 0

    if len(buf) < 2:
        return None

    opcode = buf[0] & 0x0F
    length = buf[1] & 0x7F
    offset = 2

    if length == 126:
        if len(buf) < 4:
            return None
        length = int.from_bytes(buf[2:4], "big")
        offset += 2
    elif length == 127:
        if len(buf) < 10:
            return None
        length = int.from_bytes(buf[2:10], "big")
        offset += 8

    if len(buf) < offset + length:
        return None

    payload = buf[offset:offset + length]
    _ws_last_consumed = offset + length

    if opcode == 0x08:  # Close
        return None
    if opcode == 0x09:  # Ping
        return None

    return bytes(payload)


# ── CLI entries ──

def cmd_sync_now():
    """CLI: trigger immediate sync for a company via Node Agent trigger file.

    Usage: python3 violation_helper.py sync-now --company <name>
    Writes a trigger file that Node Agent picks up within 60s and syncs
    through its existing long-lived WebSocket connection.
    Output JSON: {ok, sync_triggered, company, trigger_file, via}
    """
    from datetime import datetime

    company = None
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            company = args[i + 1]; i += 2
        else:
            i += 1

    if not company:
        print(json.dumps({"ok": False, "error": "缺少 --company 参数"}))
        return

    data_dir = _get_data_dir()
    triggers_dir = os.path.join(data_dir, "sync_triggers")
    os.makedirs(triggers_dir, exist_ok=True)

    trigger_file = os.path.join(triggers_dir, f"{company}.json")
    trigger_data = {
        "company": company,
        "triggered_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(trigger_file, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, ensure_ascii=False)

    print(json.dumps({
        "ok": True,
        "sync_triggered": True,
        "company": company,
        "trigger_file": trigger_file,
        "via": "node_agent",
    }, ensure_ascii=False))


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

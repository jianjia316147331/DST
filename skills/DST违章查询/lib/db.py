#!/usr/bin/env python3
"""lib/db.py — SQLite database operations for DST violation query tool."""
import json, os, sqlite3, sys
from datetime import datetime

# Import shared infrastructure
from .core import _find_project_root, _get_data_dir, _run, _read_stdin_text, _read_stdin_json

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
ALTER TABLE vehicles ADD COLUMN tag TEXT DEFAULT '';
ALTER TABLE vehicles ADD COLUMN tag_batch_id TEXT DEFAULT '';
"""
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

def cmd_init_db():
    """Initialize SQLite database and return path."""
    db_path = _init_db()
    print(db_path)

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

def cmd_db_insert_vehicle():
    """Upsert a vehicle record. If plate_number + company_id exists, update; else insert.
    Args (stdin JSON or CLI):
    --company-id --plate-number --plate-type --plate-type-label --status-code
    --status-label --inspection-date --unprocessed-count --query-date
    --tag --tag-batch-id
    Returns JSON with vehicle_id."""
    p = {"company_id": 0, "plate_number": "", "plate_type": "", "plate_type_label": "",
         "status_code": "", "status_label": "", "inspection_date": "",
         "unprocessed_count": 0, "query_date": "",
         "tag": "", "tag_batch_id": ""}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        for key in ["company-id", "plate-number", "plate-type", "plate-type-label",
                     "status-code", "status-label", "inspection-date",
                     "unprocessed-count", "query-date",
                     "tag", "tag-batch-id"]:
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
               unprocessed_count=?, query_date=?,
               tag=?, tag_batch_id=?
               WHERE id=?""",
            (p["plate_type"], p["plate_type_label"],
             p["status_code"], p["status_label"], p["inspection_date"],
             int(p["unprocessed_count"]), p["query_date"],
             p["tag"], p["tag_batch_id"], vehicle_id))
    else:
        cur = conn.execute(
            """INSERT INTO vehicles (company_id, plate_number, plate_type, plate_type_label,
               status_code, status_label, inspection_date, unprocessed_count, query_date,
               tag, tag_batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (p["company_id"], p["plate_number"], p["plate_type"], p["plate_type_label"],
             p["status_code"], p["status_label"], p["inspection_date"],
             int(p["unprocessed_count"]), p["query_date"],
             p["tag"], p["tag_batch_id"]))
        vehicle_id = cur.lastrowid
    conn.commit()
    conn.close()
    print(json.dumps({"vehicle_id": vehicle_id}))

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


def _collect_detail_to_db_record(detail, plate, query_date, db_conn=None):
    """Map a collect-violations detail dict (from _parse_detail_popup) to DB schema dict.
    If db_conn is provided, resolves vehicle_id from the vehicles table by plate_number."""
    # Use plate from detail dict if available (extracted from page), fall back to --plate arg
    actual_plate = detail.get("plate", "") or plate
    # Resolve vehicle_id from DB
    vehicle_id = 0
    if db_conn:
        try:
            cur = db_conn.execute(
                "SELECT id FROM vehicles WHERE plate_number = ? ORDER BY query_date DESC LIMIT 1",
                (actual_plate,))
            row = cur.fetchone()
            if row:
                vehicle_id = row[0]
        except Exception:
            pass
    return {
        "vehicle_id": vehicle_id,
        "plate_number": actual_plate,
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




# ── Optimization: combined init+connect ──
def _get_db_conn():
    """Initialize DB if needed and return an open sqlite3.Connection.
    Merges _init_db() + _get_db_path() + sqlite3.connect() into one call."""
    _init_db()
    return sqlite3.connect(_get_db_path())

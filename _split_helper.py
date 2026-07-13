#!/usr/bin/env python3
"""One-shot script: read violation_helper.py and split into lib/* modules."""
import re, os

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(SKILL_DIR, "violation_helper.py")
LIB = os.path.join(SKILL_DIR, "lib")

def read_src():
    with open(SRC, "r", encoding="utf-8") as f:
        return f.readlines()

def extract(lines, ranges):
    """Extract line ranges (1-based, inclusive)."""
    result = []
    for r in ranges:
        start, end = r
        result.extend(lines[start-1:end])
        if result and not result[-1].endswith("\n"):
            result.append("\n")
    return "".join(result)

def write_module(name, content):
    path = os.path.join(LIB, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  wrote {path} ({len(content.splitlines())} lines)")

def main():
    lines = read_src()
    total = len(lines)
    print(f"Source: {SRC} ({total} lines)")

    # ── lib/core.py ──
    # Lines to extract (1-based, inclusive ranges)
    core_ranges = [
        # imports (L1-25)
        (1, 25),
        # _OUTPUT_FILE (L27)
        (27, 27),
        # _fix_encoding + call (L31-72)
        (31, 72),
        # _TeeWriter + _setup_output_file (L77-110)
        (77, 110),
        # UNIT_LOGIN_URL, PROVINCE_URL, LICENSE_TO_PROVINCE, LICENSE_TO_URL (L112-150)
        (112, 150),
        # _find_project_root (L222-234)
        (222, 234),
        # _get_query_dir, _get_screenshot_dir, _get_report_dir, _get_data_dir (L235-252)
        (235, 252),
        # _ensure_subdirs (L276-280)
        (276, 280),
        # LOGIN_KEYWORDS, POST_LOGIN_KEYWORDS, LOGIN_PAGE_KEYWORDS,
        # LOGIN_PAGE_URL_PATTERN, QR_EXPIRED_KEYWORDS (L328-363)
        (328, 363),
        # _lark_cli_path (L369-391)
        (369, 391),
        # _pinchtab_path (L392-410)
        (392, 410),
        # _node_path (L411-473)
        (411, 473),
        # _lark_cli_base_cmd (L474-542)
        (474, 542),
        # _pinchtab_base_cmd (L543-548)
        (543, 548),
        # _run (L564-623)
        (564, 623),
        # _parse_tab_ids (L624-631) — needed by keepalive
        (624, 631),
        # _run_silent (L633-639)
        (633, 639),
        # _read_stdin_text, _read_stdin_json (L640-665)
        (640, 665),
        # cmd_get_dir (L670-676)
        (670, 676),
        # cmd_license_lookup (L681-694)
        (681, 694),
        # cmd_province_url (L699-708)
        (699, 708),
        # cmd_province_login_url (L709-722)
        (709, 722),
        # cmd_get_screenshot_dir (L1883-1886)
        (1883, 1886),
        # cmd_get_report_dir (L1891-1894)
        (1891, 1894),
        # cmd_get_data_dir (L1899-1902)
        (1899, 1902),
        # cmd_pt_find (L1907-1912)
        (1907, 1912),
        # cmd_pt_wait (L1917-1922)
        (1917, 1922),
        # cmd_prepare_dir (L2287-2293)
        (2287, 2293),
        # cmd_init (L2298-2341)
        (2298, 2341),
        # cmd_run_js (L2347-2377)
        (2347, 2377),
        # cmd_pinchtab_path (L3652-3655)
        (3652, 3655),
        # cmd_lark_cli_path (L3660-3663)
        (3660, 3663),
        # cmd_get_login_url (L3668-3671)
        (3668, 3671),
        # RATE_LIMIT_KEYWORDS (L4102-4109)
        (4102, 4109),
    ]
    core_body = extract(lines, core_ranges)

    # Add LOGIN_INDICATORS alias
    core_body += "\n# Alias for backward compatibility\nLOGIN_INDICATORS = LOGIN_KEYWORDS\n"

    # Build core.py with header, body, and module-level _fix_encoding() call
    core_content = f'''#!/usr/bin/env python3
"""lib/core.py — shared infrastructure for DST violation query tool.
Constants, path resolution, subprocess runner, output control."""
import json, os, re, io, subprocess, sys, time
from datetime import datetime

{core_body}
# Module-level: fix encoding immediately on import (core is imported first by dispatcher)
_fix_encoding()
'''
    write_module("core.py", core_content)

    # ── lib/db.py ──
    db_ranges = [
        (156, 220),    # DB_SCHEMA
        (253, 275),    # _get_db_path, _init_db
        (1014, 1018),  # cmd_init_db
        (1023, 1054),  # cmd_db_insert_company
        (1059, 1108),  # cmd_db_insert_vehicle
        (1113, 1167),  # _upsert_violation
        (1168, 1195),  # _collect_detail_to_db_record
        (1196, 1214),  # cmd_db_insert_violation
        (3083, 3116),  # _load_violations_from_db
    ]
    db_body = extract(lines, db_ranges)
    db_content = f'''#!/usr/bin/env python3
"""lib/db.py — SQLite database operations for DST violation query tool."""
import json, os, sqlite3, sys
from datetime import datetime

# Import shared infrastructure
from .core import _find_project_root, _get_data_dir, _run, _read_stdin_text, _read_stdin_json

{db_body}

# ── Optimization: combined init+connect ──
def _get_db_conn():
    """Initialize DB if needed and return an open sqlite3.Connection.
    Merges _init_db() + _get_db_path() + sqlite3.connect() into one call."""
    _init_db()
    return sqlite3.connect(_get_db_path())
'''
    write_module("db.py", db_content)

    # ── lib/feishu.py ──
    feishu_ranges = [
        (727, 769),    # cmd_gen_qr_msg
        (770, 800),    # _parse_qr_msg_args
        (806, 835),    # cmd_gen_qr_fallback
        (840, 882),    # cmd_gen_result_msg
        (887, 918),    # cmd_upload_image
        (923, 975),    # cmd_send_msg
        (980, 1009),   # cmd_send_image_msg
        (1747, 1770),  # cmd_search_user
        (1775, 1794),  # cmd_search_chat
        (1799, 1819),  # cmd_batch_get_id
        (2257, 2264),  # cmd_consume_event
        (2268, 2282),  # cmd_extract_message_id
    ]
    feishu_body = extract(lines, feishu_ranges)
    feishu_content = f'''#!/usr/bin/env python3
"""lib/feishu.py — Feishu/Lark message sending, user search, event consumption."""
import json, os, sys, time

from .core import (_run, _run_silent, _read_stdin_text, _read_stdin_json,
                   _lark_cli_path, _lark_cli_base_cmd)

{feishu_body}
'''
    write_module("feishu.py", feishu_content)

    # ── lib/profiles.py ──
    profiles_ranges = [
        (1221, 1315),  # cmd_keepalive_health
        (1320, 1453),  # cmd_ensure_keepalive
        (1454, 1480),  # _save_notify_config
        (1484, 1541),  # _build_profile_result
        (1542, 1615),  # cmd_profile_lookup
        (1616, 1637),  # cmd_profile_list
        (1642, 1708),  # cmd_profile_register
        (1713, 1742),  # cmd_profile_logout
        (1824, 1878),  # cmd_save_notify
    ]
    profiles_body = extract(lines, profiles_ranges)
    profiles_content = f'''#!/usr/bin/env python3
"""lib/profiles.py — Profile management and keepalive daemon control."""
import json, os, sys, time, subprocess
from datetime import datetime

from .core import (_run, _run_silent, _read_stdin_text, _read_stdin_json,
                   _pinchtab_base_cmd, _find_project_root, _get_data_dir,
                   _get_query_dir, _pinchtab_path, _lark_cli_path,
                   _ensure_subdirs)
from .db import _init_db, _get_db_path, _get_db_conn

{profiles_body}
'''
    write_module("profiles.py", profiles_content)

    # ── lib/login.py ──
    login_ranges = [
        (1947, 1950),  # QR_EXPIRED_PAGE_INDICATORS
        (1951, 1983),  # _QR_CHECK_JS
        (1984, 2252),  # cmd_poll_login
        (4060, 4097),  # cmd_get_login_type
        (4208, 4323),  # cmd_check_login_state
        (4328, 4356),  # cmd_check_login_valid
    ]
    login_body = extract(lines, login_ranges)
    login_content = f'''#!/usr/bin/env python3
"""lib/login.py — 12123 platform login flow (QR polling, state detection)."""
import json, os, sys, time

from .core import (_run, _run_silent, _read_stdin_text, _read_stdin_json,
                   _lark_cli_path, _pinchtab_path, _pinchtab_base_cmd,
                   LOGIN_KEYWORDS, POST_LOGIN_KEYWORDS, LOGIN_PAGE_KEYWORDS,
                   LOGIN_PAGE_URL_PATTERN, QR_EXPIRED_KEYWORDS, UNIT_LOGIN_URL)
from .feishu import cmd_send_msg, cmd_upload_image

{login_body}
'''
    write_module("login.py", login_content)

    # ── lib/query.py ──
    query_ranges = [
        (2382, 2447),  # cmd_list_vehicles
        (2452, 2538),  # cmd_open_vehicle
        (2539, 2563),  # _dismiss_popup_js + RATE_LIMIT_XHR_PATTERNS
        (2565, 2615),  # _setup_xhr_monitor
        (2616, 2626),  # _check_xhr_rate_limit
        (2627, 2644),  # _check_rate_limit
        (2649, 2929),  # cmd_collect_violations
        (2930, 2979),  # _extract_detail_page_violations
        (2980, 3045),  # _click_detail_page
        (3046, 3082),  # _get_detail_page_state
        (3117, 3261),  # _close_popup
        (3262, 3318),  # _parse_detail_popup
        (3323, 3374),  # cmd_go_back
        (3379, 3494),  # cmd_click_page
        (3495, 3571),  # _click_page_direct
        (3572, 3590),  # _click_page_number
        (3591, 3647),  # _get_pagination_state
        (3676, 3767),  # cmd_save_detail_progress
        (3772, 3837),  # cmd_load_detail_progress
        (3842, 3878),  # cmd_reset_detail_progress
        (3883, 3948),  # cmd_get_page_vehicles
        (3953, 4024),  # cmd_find_plate_page
        (4025, 4055),  # _get_current_page_vehicles
        (4110, 4139),  # cmd_detect_rate_limit
        (4144, 4203),  # cmd_dismiss_popup
    ]
    query_body = extract(lines, query_ranges)
    query_content = f'''#!/usr/bin/env python3
"""lib/query.py — Vehicle and violation query operations on 12123 platform."""
import json, os, sys, time, random

from .core import (_run, _run_silent, _read_stdin_text, _read_stdin_json,
                   _find_project_root, _get_data_dir, _pinchtab_path,
                   _pinchtab_base_cmd, RATE_LIMIT_KEYWORDS)
from .db import (_init_db, _get_db_path, _get_db_conn, _upsert_violation,
                 _collect_detail_to_db_record, _load_violations_from_db)

{query_body}
'''
    write_module("query.py", query_content)

    print("\nAll modules written.")

if __name__ == "__main__":
    main()

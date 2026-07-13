#!/usr/bin/env python3
"""violation_helper.py — thin dispatcher, delegates to lib/* modules.

All subcommand logic has been extracted to tree-structured lib/ modules.
This file is now only ~70 lines of import + dispatch + main boilerplate.
"""
import os, sys

# Ensure lib/ is importable (relative to this file, not /tmp)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.core     import (_fix_encoding, _setup_output_file,
                          cmd_get_dir, cmd_get_screenshot_dir, cmd_get_report_dir,
                          cmd_get_data_dir, cmd_license_lookup, cmd_province_url,
                          cmd_province_login_url, cmd_pinchtab_path, cmd_lark_cli_path,
                          cmd_get_login_url, cmd_pt_find, cmd_pt_wait, cmd_run_js,
                          cmd_prepare_dir, cmd_init)
from lib.db       import (cmd_init_db, cmd_db_insert_company, cmd_db_insert_vehicle,
                          cmd_db_insert_violation)
from lib.feishu   import (cmd_gen_qr_msg, cmd_gen_qr_fallback, cmd_gen_result_msg,
                          cmd_upload_image, cmd_send_msg, cmd_send_image_msg,
                          cmd_search_user, cmd_search_chat, cmd_batch_get_id,
                          cmd_consume_event, cmd_extract_message_id)
from lib.profiles import (cmd_profile_lookup, cmd_profile_list, cmd_profile_register,
                          cmd_profile_logout, cmd_keepalive_health,
                          cmd_ensure_keepalive, cmd_save_notify)
from lib.login    import (cmd_poll_login, cmd_check_login_state, cmd_check_login_valid,
                          cmd_get_login_type)
from lib.query    import (cmd_list_vehicles, cmd_get_page_vehicles, cmd_open_vehicle,
                          cmd_collect_violations, cmd_go_back, cmd_click_page,
                          cmd_save_detail_progress, cmd_load_detail_progress,
                          cmd_reset_detail_progress, cmd_find_plate_page,
                          cmd_detect_rate_limit, cmd_dismiss_popup)

SUBCOMMANDS = {
    # core
    "get-dir": cmd_get_dir, "get-screenshot-dir": cmd_get_screenshot_dir,
    "get-report-dir": cmd_get_report_dir, "get-data-dir": cmd_get_data_dir,
    "license-lookup": cmd_license_lookup, "province-url": cmd_province_url,
    "province-login-url": cmd_province_login_url, "pinchtab-path": cmd_pinchtab_path,
    "lark-cli-path": cmd_lark_cli_path, "get-login-url": cmd_get_login_url,
    "pt-find": cmd_pt_find, "pt-wait": cmd_pt_wait, "run-js": cmd_run_js,
    "prepare-dir": cmd_prepare_dir, "init": cmd_init,
    # db
    "init-db": cmd_init_db, "db-insert-company": cmd_db_insert_company,
    "db-insert-vehicle": cmd_db_insert_vehicle, "db-insert-violation": cmd_db_insert_violation,
    # feishu
    "gen-qr-msg": cmd_gen_qr_msg, "gen-qr-fallback": cmd_gen_qr_fallback,
    "gen-result-msg": cmd_gen_result_msg, "upload-image": cmd_upload_image,
    "send-msg": cmd_send_msg, "send-image-msg": cmd_send_image_msg,
    "search-user": cmd_search_user, "search-chat": cmd_search_chat,
    "batch-get-id": cmd_batch_get_id, "consume-event": cmd_consume_event,
    "extract-message-id": cmd_extract_message_id,
    # profiles
    "profile-lookup": cmd_profile_lookup, "profile-list": cmd_profile_list,
    "profile-register": cmd_profile_register, "profile-logout": cmd_profile_logout,
    "keepalive-health": cmd_keepalive_health, "ensure-keepalive": cmd_ensure_keepalive,
    "save-notify": cmd_save_notify,
    # login
    "poll-login": cmd_poll_login, "check-login-state": cmd_check_login_state,
    "check-login-valid": cmd_check_login_valid, "get-login-type": cmd_get_login_type,
    # query
    "list-vehicles": cmd_list_vehicles, "get-page-vehicles": cmd_get_page_vehicles,
    "open-vehicle": cmd_open_vehicle, "collect-violations": cmd_collect_violations,
    "go-back": cmd_go_back, "click-page": cmd_click_page,
    "save-detail-progress": cmd_save_detail_progress,
    "load-detail-progress": cmd_load_detail_progress,
    "reset-detail-progress": cmd_reset_detail_progress,
    "find-plate-page": cmd_find_plate_page, "detect-rate-limit": cmd_detect_rate_limit,
    "dismiss-popup": cmd_dismiss_popup,
}


# Fix encoding at module level (same as original monolith L72).
# On Linux this is mostly a no-op; on Windows it fixes GBK → UTF-8.
# Important: must be called BEFORE any print(), but AFTER imports complete,
# because TextIOWrapper wrapping from a non-__main__ module context can
# sometimes break output buffering. We keep it here in the __main__ dispatcher.
_fix_encoding()


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 violation_helper.py <subcommand> [args...]", file=sys.stderr)
        print(f"Available: {', '.join(sorted(SUBCOMMANDS))}", file=sys.stderr)
        sys.exit(1)

    # Parse --output/-o before dispatching
    _setup_output_file()

    subcmd = sys.argv[1]
    if subcmd in SUBCOMMANDS:
        SUBCOMMANDS[subcmd]()
    else:
        print(f"Unknown subcommand: {subcmd}", file=sys.stderr)
        print(f"Available: {', '.join(sorted(SUBCOMMANDS))}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

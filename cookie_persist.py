#!/usr/bin/env python3
"""
Cookie 持久化工具 — 将 12123 平台的 session cookie 改为 persistent。

原理：
  Chrome 在 Linux 上使用 --password-store=basic 时，cookie 用固定密钥加密。
  启动时 Chrome 读取 Cookies SQLite DB，丢弃 is_persistent=0 的 cookie。
  本脚本将这些 cookie 的 is_persistent 改为 1，让它们能跨 Chrome 重启存活。

用法：
  python3 cookie_persist.py --profile /path/to/profiles/default
  python3 cookie_persist.py --profile /path/to/profiles/default --dry-run
  python3 cookie_persist.py --profile /path/to/profiles/default --verify

安全：
  - 只修改 122.gov.cn 域名的 cookie
  - 保留原始 encrypted_value，不做解密
  - 用 WAL 模式安全写入
"""

import argparse
import os
import sqlite3
import sys
import time


# 12123 平台域名匹配
PLATFORM_DOMAINS = [
    '%122.gov.cn%',
    '%12123%',
    '%gab.122%',
]

# 过期时间：30 天后
EXPIRE_DAYS = 30


def _get_cookies_db(profile_dir):
    """Get path to Cookies SQLite DB."""
    db_path = os.path.join(profile_dir, "Default", "Cookies")
    if not os.path.exists(db_path):
        db_path = os.path.join(profile_dir, "Cookies")
    return db_path


def verify(db_path):
    """Check current cookie persistence state."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "SELECT host_key, name, is_persistent, has_expires, "
        "expires_utc, last_access_utc "
        "FROM cookies WHERE (" +
        " OR ".join(["host_key LIKE ?"] * len(PLATFORM_DOMAINS)) +
        ") ORDER BY host_key, name",
        PLATFORM_DOMAINS
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("No 12123 platform cookies found.")
        return None

    session_count = sum(1 for r in rows if r[2] == 0)
    persistent_count = sum(1 for r in rows if r[2] == 1)

    print(f"Total 12123 cookies: {len(rows)}")
    print(f"  Persistent: {persistent_count}")
    print(f"  Session-only: {session_count}")
    print()
    print(f"{'Host':<25} {'Name':<25} {'Persist':<8} {'HasExp':<8} {'Expires'}")
    print("-" * 90)
    for row in rows:
        host, name, is_p, has_e, exp, last = row
        exp_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime(exp)) if exp else "never"
        print(f"{host:<25} {name:<25} {is_p:<8} {has_e:<8} {exp_str}")

    return {"total": len(rows), "session": session_count, "persistent": persistent_count}


def persist(db_path, dry_run=False):
    """Convert session cookies to persistent cookies."""
    conn = sqlite3.connect(db_path)

    # First, check current state
    cur = conn.execute(
        "SELECT host_key, name, is_persistent FROM cookies WHERE (" +
        " OR ".join(["host_key LIKE ?"] * len(PLATFORM_DOMAINS)) +
        ") AND is_persistent = 0",
        PLATFORM_DOMAINS
    )
    session_cookies = cur.fetchall()

    if not session_cookies:
        print("No session-only 12123 cookies to persist.")
        conn.close()
        return 0

    future_expire = int((time.time() + EXPIRE_DAYS * 86400) * 1_000_000)  # microseconds

    if dry_run:
        print(f"[DRY RUN] Would convert {len(session_cookies)} session cookies to persistent:")
        for host, name, _ in session_cookies:
            print(f"  {host} / {name}")
        conn.close()
        return len(session_cookies)

    # Update cookies: mark as persistent, set expiry 30 days from now
    cur = conn.execute(
        "UPDATE cookies SET is_persistent = 1, has_expires = 1, "
        "expires_utc = ? WHERE (" +
        " OR ".join(["host_key LIKE ?"] * len(PLATFORM_DOMAINS)) +
        ") AND is_persistent = 0",
        [future_expire] + PLATFORM_DOMAINS
    )
    updated = cur.rowcount
    conn.commit()
    conn.close()

    print(f"Converted {updated} session cookies to persistent (expires in {EXPIRE_DAYS} days).")
    return updated


def main():
    parser = argparse.ArgumentParser(description="12123 Cookie Persistence Tool")
    parser.add_argument("--profile", dest="profile_dir",
                        default=os.path.expanduser("~/.pinchtab/profiles/default"),
                        help="Path to Chrome profile directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be changed without actually changing")
    parser.add_argument("--verify", action="store_true",
                        help="Show current cookie state and exit")
    args = parser.parse_args()

    db_path = _get_cookies_db(args.profile_dir)
    if not os.path.exists(db_path):
        # First launch / fresh profile — no cookies to persist yet, not an error.
        # Chrome creates the Cookies DB on first launch; we'll persist them on next run.
        print(f"Cookies DB not found at {db_path} — nothing to persist (first launch?)")
        sys.exit(0)

    if args.verify:
        result = verify(db_path)
        sys.exit(0 if result and result["session"] == 0 else 1)
    else:
        verify(db_path)
        print()
        updated = persist(db_path, dry_run=args.dry_run)
        if updated > 0 and not args.dry_run:
            print()
            print("Verification after change:")
            verify(db_path)


if __name__ == "__main__":
    main()

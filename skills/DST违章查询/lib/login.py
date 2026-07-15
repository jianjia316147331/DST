#!/usr/bin/env python3
"""lib/login.py — 12123 platform login flow (QR polling, state detection)."""
import json, os, sys, time

from .core import (_run, _run_silent,
                   _pinchtab_path,
                   POST_LOGIN_KEYWORDS, LOGIN_PAGE_KEYWORDS,
                   LOGIN_PAGE_URL_PATTERN, LOGIN_INDICATORS,
                   RATE_LIMIT_KEYWORDS)

_QR_CHECK_JS = """
(function() {
  // Use innerText (not textContent) — innerText respects CSS visibility
  // so "二维码已过期" hidden via display:none won't cause false positives.
  var body = document.body.innerText || '';
  var indicators = ['二维码已过期', '已失效', '请重新刷新', '二维码失效'];
  for (var i = 0; i < indicators.length; i++) {
    if (body.indexOf(indicators[i]) !== -1) return 'expired:' + indicators[i];
  }
  // Check if QR image is still present (base64 data URI is the live QR)
  var imgs = document.querySelectorAll('img');
  var hasQR = false;
  for (var j = 0; j < imgs.length; j++) {
    var src = imgs[j].src || '';
    // Match both named QR images and base64 data URIs (live QR codes)
    if (src.indexOf('qr') !== -1 || src.indexOf('code') !== -1 || src.indexOf('login') !== -1 ||
        src.indexOf('data:image') === 0) {
      hasQR = true;
      break;
    }
  }
  // Also check canvas elements (some QR implementations use canvas)
  if (!hasQR) {
    var canvases = document.querySelectorAll('canvas');
    if (canvases.length > 0) hasQR = true;
  }
  if (!hasQR && imgs.length === 0 && document.querySelectorAll('canvas').length === 0) {
    return 'expired:no_qr_element';
  }
  return 'ok';
})()
"""

def cmd_poll_login():
    """Wait for login completion via browser detection only.

    Detects login by snap keyword matching (POST_LOGIN_KEYWORDS in
    accessibility tree). Detects QR expiry by eval JS (innerText check).
    No Feishu API calls — pure browser-side detection.

    Args:
      --max-duration SECONDS    (default 300 = 5min).
      --qr-refresh-count N      Current QR refresh count (0-indexed). Default 0.
      --max-qr-refreshes N      Max QR refreshes before giving up. Default 3.

    Exit codes:
      0 — login detected (browser)
      1 — timeout or max refreshes reached
      3 — QR expired (caller should refresh and re-poll)
    """
    p = {"max_duration": "300",
         "qr_refresh_count": "0", "max_qr_refreshes": "3"}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--max-duration" and i + 1 < len(args):
            p["max_duration"] = args[i + 1]; i += 2
        elif args[i] == "--max-retries" and i + 1 < len(args):
            retries = int(args[i + 1])
            p["max_duration"] = str(max(30, retries * 10)); i += 2
        elif args[i] == "--qr-refresh-count" and i + 1 < len(args):
            p["qr_refresh_count"] = args[i + 1]; i += 2
        elif args[i] == "--max-qr-refreshes" and i + 1 < len(args):
            p["max_qr_refreshes"] = args[i + 1]; i += 2
        else:
            i += 1

    pt = _pinchtab_path()
    max_duration = int(p["max_duration"])
    qr_refresh_count = int(p["qr_refresh_count"])
    max_qr_refreshes = int(p["max_qr_refreshes"])

    start_time = time.time()
    poll_count = 0

    while True:
        elapsed = time.time() - start_time
        if elapsed > max_duration:
            break

        poll_count += 1
        now = time.strftime("%H:%M:%S")

        # Check login via snap (accessibility tree — visible elements only).
        # snap won't match "二维码已过期" hidden in inactive tabs or
        # "退出"/"我的主页" in CSS-hidden DOM.
        try:
            page_snap = (_run_silent([pt, "snap"]).stdout or "")
            for kw in LOGIN_INDICATORS:
                if kw in page_snap:
                    print(f"  [{now}] browser login detected: {kw}", flush=True)
                    print("LOGIN_DETECTED_BROWSER", flush=True)
                    sys.exit(0)
        except Exception as e:
            print(f"  [{now}] browser check skipped: {e}", flush=True)

        # Check scan detected (deptLoginNext intermediate page).
        # On deptLoginNext, the accessibility tree (snap) shows only ~9 nodes
        # without company names. Company list + "请选择单位" are only in
        # JS innerText. Same pattern as _QR_CHECK_JS below.
        try:
            scan_js = ("(function(){var t=document.body.innerText||'';"
                       "return t.indexOf('请选择单位')!==-1?'dept_next':'no';})()")
            scan_status = (_run_silent([pt, "eval", scan_js]).stdout or "").strip()
            if scan_status == "dept_next":
                print(f"  [{now}] scan detected: deptLoginNext (innerText)", flush=True)
                print("SCAN_DETECTED", flush=True)
                sys.exit(0)
        except Exception as e:
            print(f"  [{now}] scan check skipped: {e}", flush=True)

        # Check QR expiry via browser JS every poll.
        # _QR_CHECK_JS uses innerText (not textContent) — ignores
        # "二维码已过期" hidden in the inactive login tab.
        # Every-poll: login page QR can expire in <60s; catching it fast
        # avoids the user scanning an already-expired QR.
        try:
            qr_result = _run_silent([pt, "eval", _QR_CHECK_JS])
            qr_status = (qr_result.stdout or "").strip()
            print(f"  [{now}] poll#{poll_count} qr={qr_status}", flush=True)
            if qr_status.startswith("expired"):
                if qr_refresh_count >= max_qr_refreshes:
                    print(f"  QR expired, max refreshes ({max_qr_refreshes}) reached", flush=True)
                    print("TIMEOUT", flush=True)
                    sys.exit(1)
                print("QR_EXPIRED_DETECTED", flush=True)
                sys.exit(3)
        except Exception as e:
            print(f"  [{now}] QR check skipped: {e}", flush=True)

        # Dynamic interval
        if elapsed < 60:
            interval = 10
        elif elapsed < 180:
            interval = 5
        else:
            interval = 15
        time.sleep(interval)

    # --- Polling exhausted ---
    # Final QR expiration check before giving up
    try:
        qr_result = _run_silent([pt, "eval", _QR_CHECK_JS])
        qr_status = qr_result.stdout.strip()
        print(f"  [{time.strftime('%H:%M:%S')}] Final QR check: {qr_status}", flush=True)
        if qr_status.startswith("expired"):
            if qr_refresh_count >= max_qr_refreshes:
                print("TIMEOUT", flush=True)
                sys.exit(1)
            print("QR_EXPIRED_DETECTED", flush=True)
            sys.exit(3)
    except Exception:
        pass

    print("TIMEOUT", flush=True)
    sys.exit(1)

def cmd_get_login_type():
    """Detect current login type: unit (单位) or personal (个人).
    Returns JSON: {type: 'unit'|'personal'|'none'}
    Used to verify we're logged in as unit user before proceeding.
    """
    snap = _run(["pinchtab", "snap"]).stdout

    result = {"type": "none", "details": ""}

    # Unified unit indicators (aligned with POST_LOGIN_KEYWORDS)
    unit_indicators = ["公司列表", "公司名称", "单位信息", "租赁车辆", "车辆管理",
                       "企业用户"]
    personal_indicators = ["个人用户", "个人中心", "我的车辆", "驾驶人"]

    for kw in unit_indicators:
        if kw in snap:
            result["type"] = "unit"
            result["details"] = f"found unit indicator: {kw}"
            break

    if result["type"] == "none":
        for kw in personal_indicators:
            if kw in snap:
                result["type"] = "personal"
                result["details"] = f"found personal indicator: {kw}"
                break

    # Check if any text at all (login state detection) — use unified set
    if result["type"] == "none":
        for kw in POST_LOGIN_KEYWORDS:
            if kw in snap:
                result["type"] = "unknown"
                result["details"] = "logged in but cannot determine type"
                break

    print(json.dumps(result, ensure_ascii=False))


def cmd_check_login_state():
    """Unified login state detection combining URL+DOM (Tier 1) with
    keyword matching (Tier 2).

    Tier 1 (URL+DOM) — initial "are we logged in?" check:
      - Gets current URL via JS
      - If on gab.122.gov.cn/m/login → directly returns "login_page"
      - If on xx.122.gov.cn platform → checks DOM for logged-in indicators
      - URL+DOM is the authoritative method for initial state detection

    Tier 2 (keyword fallback) — QR scan poll detection:
      - Uses text+snap keyword matching
      - "退出"/"车辆管理"/"公司列表" etc. = logged in
      - "单位用户登录"/"个人用户登录" etc. = login page (only if NO logged-in keywords)

    Options:
      --mode url        URL+DOM only (default, for initial state check)
      --mode keyword    Keyword matching only (for QR poll detection)
      --mode auto       URL+DOM first, fallback to keyword (most thorough)

    Returns JSON: {
      state: "logged_in" | "login_page" | "rate_limited" | "unknown",
      method: "url" | "keyword" | "url+keyword",
      url: str | null,
      details: str,
      indicators: [str]  // which indicators matched
    }
    Exit code: 0=logged_in, 1=login_page/expired, 2=rate_limited, 3=unknown
    """
    mode = "auto"
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--mode" and i + 1 < len(args):
            mode = args[i + 1]; i += 2
        else:
            i += 1

    result = {
        "state": "unknown",
        "method": mode,
        "url": None,
        "details": "",
        "indicators": []
    }

    # ── Tier 1: URL+DOM ──
    if mode in ("url", "auto"):
        # Get current URL
        url_js = "(function(){return window.location.href;})()"
        url_result = _run(["pinchtab", "eval", url_js])
        current_url = url_result.stdout.strip()
        result["url"] = current_url

        if LOGIN_PAGE_URL_PATTERN in current_url:
            result["state"] = "login_page"
            result["method"] = "url"
            result["details"] = f"on login page: {current_url[:80]}"
            result["indicators"] = ["url:gab.122.gov.cn/m/login"]
            print(json.dumps(result, ensure_ascii=False))
            sys.exit(1)

        if "122.gov.cn" in current_url:
            result["method"] = "url"
            result["details"] = f"on platform: {current_url[:80]}"

    # ── Tier 2: keyword matching ──
    # Use snap (accessibility tree) only — NOT text. text includes
    # CSS-hidden DOM elements ("退出"/"我的主页" exist in HTML source even
    # when logged out), causing false positives.
    if mode == "keyword" or (mode == "auto" and result["state"] == "unknown"):
        snap = _run(["pinchtab", "snap"]).stdout

        # Check rate-limit first (highest priority)
        rate_hits = [kw for kw in RATE_LIMIT_KEYWORDS if kw in snap]
        if rate_hits:
            result["state"] = "rate_limited"
            result["method"] = "keyword" if mode == "keyword" else "url+keyword"
            result["details"] = f"rate-limit keywords: {rate_hits}"
            result["indicators"] = rate_hits
            print(json.dumps(result, ensure_ascii=False))
            sys.exit(2)

        # Check for logged-in indicators
        logged_in_hits = [kw for kw in POST_LOGIN_KEYWORDS if kw in snap]
        login_page_hits = [kw for kw in LOGIN_PAGE_KEYWORDS if kw in snap]

        if logged_in_hits:
            result["state"] = "logged_in"
            result["method"] = "keyword" if mode == "keyword" else "url+keyword"
            result["details"] = f"logged-in indicators: {logged_in_hits}"
            result["indicators"] = logged_in_hits
            print(json.dumps(result, ensure_ascii=False))
            sys.exit(0)

        # Only treat login-page keywords as "login_page" if NO logged-in
        # indicators are present (nav bars may show "单位用户登录" as
        # account-switch links even when logged in)
        if login_page_hits and not logged_in_hits:
            result["state"] = "login_page"
            result["method"] = "keyword" if mode == "keyword" else "url+keyword"
            result["details"] = f"login page indicators: {login_page_hits}"
            result["indicators"] = login_page_hits
            print(json.dumps(result, ensure_ascii=False))
            sys.exit(1)

        result["method"] = "keyword" if mode == "keyword" else "url+keyword"

    # ── Still unknown ──
    text_preview = (text + snap)[:200].replace("\n", " ") if ('text' in dir() and 'snap' in dir()) else ""
    result["details"] = result["details"] or f"no indicators matched. preview: {text_preview}"
    result["state"] = "unknown"
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(3)


def cmd_check_login_valid():
    """Check if current login is valid for the target province/company.
    Legacy wrapper — delegates to the unified check-login-state.
    Does NOT logout - only verifies. Returns JSON with login state.
    """
    snap = _run(["pinchtab", "snap"]).stdout

    # Use snap (accessibility tree) only — text includes hidden DOM
    # elements causing false positives for "退出"/"我的主页" when logged out.
    logged_in_hits = [kw for kw in POST_LOGIN_KEYWORDS if kw in snap]
    login_page_hits = [kw for kw in LOGIN_PAGE_KEYWORDS if kw in snap]

    has_logout = "退出" in snap
    has_unit = "公司列表" in snap or "租赁车" in snap or "租赁车辆" in snap
    has_personal = "个人用户" in snap
    is_logged_in = bool(logged_in_hits) and not (
        bool(login_page_hits) and not logged_in_hits
    )

    result = {
        "logged_in": is_logged_in,
        "is_unit": has_unit or (is_logged_in and not has_personal),
        "has_logout_btn": has_logout,
        "action": "continue" if is_logged_in and (has_unit or has_logout) else "login_required"
    }
    print(json.dumps(result, ensure_ascii=False))





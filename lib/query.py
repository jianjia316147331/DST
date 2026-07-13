#!/usr/bin/env python3
"""lib/query.py — Vehicle and violation query operations on 12123 platform."""
import json, os, re, sys, time, random
from datetime import datetime

from .core import (_run, _run_silent, _read_stdin_text, _read_stdin_json,
                   _find_project_root, _get_data_dir, _pinchtab_path,
                   _pinchtab_base_cmd, RATE_LIMIT_KEYWORDS)
from .db import (_init_db, _get_db_path, _get_db_conn, _upsert_violation,
                 _collect_detail_to_db_record, _load_violations_from_db)

def cmd_list_vehicles():
    """Extract vehicle list + pagination info from current page as JSON."""
    js = """
(function() {
  var vehicles = [];
  var table = document.querySelector('table');
  if (!table) { return JSON.stringify({error: 'no table found'}); }

  var rows = table.querySelectorAll('tr');
  for (var r = 0; r < rows.length; r++) {
    var tds = rows[r].querySelectorAll('td');
    if (tds.length >= 5) {
      var vals = [];
      for (var c = 0; c < tds.length; c++) {
        vals.push(tds[c].textContent.trim());
      }
      var first = vals[0] || '';
      if (first.length >= 7 && first.length <= 8) {
        vehicles.push({
          plate: vals[0] || '',
          type: vals[1] || '',
          status: vals[2] || '',
          inspection_date: vals[3] || '',
          scrap: vals[4] || '',
          unprocessed: parseInt(vals[5]) || 0
        });
      }
    }
  }

  var pagination = {current: 1, total: 1, has_next: false, has_prev: false};
  var pageLinks = document.querySelectorAll('a');
  var maxPage = 1;
  for (var p = 0; p < pageLinks.length; p++) {
    var num = parseInt(pageLinks[p].textContent.trim());
    if (num > maxPage) maxPage = num;
  }
  pagination.total = maxPage;

  var allPageElements = document.querySelectorAll('a, span, li');
  for (var q = 0; q < allPageElements.length; q++) {
    var t = allPageElements[q].textContent.trim();
    if (/^\\d+$/.test(t) && allPageElements[q].tagName !== 'A') {
      pagination.current = parseInt(t);
      break;
    }
  }

  for (var s = 0; s < pageLinks.length; s++) {
    if (pageLinks[s].textContent.trim() === '下一页' || pageLinks[s].textContent.trim().includes('next')) {
      pagination.has_next = true;
      break;
    }
  }

  return JSON.stringify({vehicles: vehicles, pagination: pagination});
})()
"""
    result = _run(["pinchtab", "eval", js])
    out = result.stdout.strip()
    m = re.search(r'\{.*\}', out, re.DOTALL)
    if m:
        print(m.group(0))
    else:
        print(out)

def cmd_open_vehicle():
    """Double-click the Nth vehicle row on the list page to open its detail.
    Args: --index N (1-based)

    Features:
    - Dismiss popup before attempting
    - Triple retry with exponential backoff (2s, 4s, 8s)
    - URL verification (must navigate to vehdetail.html)
    - Rate-limit detection on repeated failures
    """
    p = {"index": "1"}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--index" and i + 1 < len(args):
            p["index"] = args[i + 1]; i += 2
        else:
            i += 1

    idx = int(p["index"])

    # Dismiss any popup first
    _dismiss_popup_js()

    # Vehicle plate pattern: province prefix + letter
    plate_js = f"""
(function() {{
  var rows = document.querySelectorAll('table tr');
  var count = 0;
  for (var r = 0; r < rows.length; r++) {{
    var tds = rows[r].querySelectorAll('td');
    if (tds.length >= 1) {{
      var t = tds[0].textContent.trim();
      if (/^[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁][A-Z]/.test(t)) {{
        count++;
        if (count === {idx}) {{
          tds[0].dispatchEvent(new MouseEvent('dblclick', {{bubbles: true, cancelable: true, view: window}}));
          return JSON.stringify({{ok: true, plate: t, row: count}});
        }}
      }}
    }}
  }}
  return JSON.stringify({{ok: false, error: 'index {idx} not found', rows_found: count}});
}})()
"""
    # Triple retry with exponential backoff
    max_retries = 3
    for attempt in range(max_retries):
        if attempt > 0:
            backoff = 2 ** attempt  # 2, 4, 8 seconds
            time.sleep(backoff)
            _dismiss_popup_js()  # Re-dismiss any popup that appeared
            time.sleep(random.uniform(1, 2))

        result = _run(["pinchtab", "eval", plate_js])
        try:
            info = json.loads(result.stdout.strip())
        except (json.JSONDecodeError, ValueError):
            info = {"ok": False, "error": result.stdout.strip()}

        if info.get("ok"):
            time.sleep(random.uniform(2, 5))
            # Verify navigation to detail page
            check = _run(["pinchtab", "eval",
                "(function(){return window.location.href.indexOf('vehdetail')!==-1?'detail':'other'})()"])
            if 'detail' in check.stdout:
                print(json.dumps({"ok": True, "plate": info.get("plate", ""),
                                  "attempt": attempt + 1}, ensure_ascii=False))
                return
            else:
                # Double-click didn't navigate - retry
                if attempt < max_retries - 1:
                    continue

        if attempt < max_retries - 1:
            continue

    # All retries exhausted - check for rate limiting
    rate_check = _check_rate_limit()
    if rate_check["blocked"]:
        print(json.dumps({"ok": False, "error": "rate_limited",
                          "keywords": rate_check["keywords_found"]}, ensure_ascii=False))
    else:
        print(json.dumps({"ok": False, "error": "max_retries_exhausted",
                          "index": idx}, ensure_ascii=False))


def _dismiss_popup_js():
    """Internal: dismiss system popups via JS. Non-fatal on failure."""
    js = """
(function() {
  var texts = ['本人已知晓', '确定', '知道了', '关闭'];
  var all = document.querySelectorAll('button, a');
  for (var i = 0; i < all.length; i++) {
    var t = (all[i].textContent || '').trim();
    for (var j = 0; j < texts.length; j++) {
      if (t.indexOf(texts[j]) !== -1 && all[i].offsetHeight > 0) {
        all[i].click(); return 'ok';
      }
    }
  }
  return 'none';
})()
"""
    _run(["pinchtab", "eval", js])


# Rate-limit indicators from XHR responses (silent API rate-limiting)
RATE_LIMIT_XHR_PATTERNS = [
    "查询过于频繁", "操作频繁", "请求过于频繁", "访问被限制",
    "rate limit", "too many requests", "try again later",
]
def _setup_xhr_monitor():
    """Inject XHR monitoring JS into the page. Captures rate-limit responses.
    Must be called ONCE per page load. Subsequent XHR calls will be tracked
    in window.__xhrRateLimited."""
    js = """
(function() {
  if (window.__xhrMonitorInstalled) return 'already-installed';
  window.__xhrMonitorInstalled = true;
  window.__xhrRateLimited = false;
  window.__xhrRateLimitReason = '';

  var origOpen = XMLHttpRequest.prototype.open;
  var origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function(method, url) {
    this.__monitorUrl = url;
    return origOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function(body) {
    var self = this;
    var handler = function() {
      if (self.status === 200 && self.responseText) {
        try {
          var resp = JSON.parse(self.responseText);
          if (resp.code === 500 || resp.code === '500') {
            var msg = resp.message || resp.msg || '';
            var patterns = ['查询过于频繁','操作频繁','请求过于频繁','访问被限制','rate limit','too many'];
            for (var i = 0; i < patterns.length; i++) {
              if (msg.indexOf(patterns[i]) !== -1) {
                window.__xhrRateLimited = true;
                window.__xhrRateLimitReason = self.__monitorUrl + ': ' + msg;
                break;
              }
            }
          }
        } catch(e) {}
      }
    };
    this.addEventListener('load', handler);
    // NOTE: Do NOT flag all XHR errors as rate-limiting.
    // Network errors can happen for many reasons (analytics, CORS, etc.)
    // Only actual rate-limit responses (code 500 + specific message) are flagged above.
    return origSend.apply(this, arguments);
  };
  return 'installed';
})()
"""
    _run(["pinchtab", "eval", js])


def _check_xhr_rate_limit():
    """Check if any XHR request was rate-limited. Returns (blocked, reason)."""
    result = _run(["pinchtab", "eval",
        "(function(){return JSON.stringify({blocked:!!window.__xhrRateLimited,reason:window.__xhrRateLimitReason||''})})()"])
    try:
        data = json.loads(result.stdout.strip())
        return data.get("blocked", False), data.get("reason", "")
    except (json.JSONDecodeError, ValueError):
        return False, ""


def _check_rate_limit():
    """Internal: check for rate-limit/feng-kong indicators. Returns dict.
    Checks BOTH page text keywords AND XHR response patterns."""
    text = _run(["pinchtab", "text"]).stdout
    snap = _run(["pinchtab", "snap"]).stdout
    combined = text + " " + snap
    found = [kw for kw in RATE_LIMIT_KEYWORDS if kw in combined]
    has_table = "号牌号码" in snap or "未处理违法" in snap
    on_vehlist = "vehlist" in snap

    # Check XHR rate-limiting
    xhr_blocked, xhr_reason = _check_xhr_rate_limit()
    if xhr_blocked and xhr_reason:
        found.append(f"XHR: {xhr_reason}")

    blocked = len(found) > 0 or (on_vehlist and not has_table)
    return {"blocked": blocked, "keywords_found": found, "xhr_blocked": xhr_blocked}

def cmd_collect_violations():
    """On a vehicle detail page, collect violation details with smart pagination
    and SQLite comparison.

    Features:
    - Dismiss popups before extraction
    - Compare with SQLite DB: skip if already recorded and status unchanged
    - Only click '查看详情' for unprocessed/unpaid violations
    - Support detail page pagination (>10 violations)
    - Rate-limit detection on failure
    - Random delays: 1-2s clicks, 3-8s between violations
    - Resume support: --resume-from N to continue from Nth detail page

    Args: --plate PLATE (for DB lookup), --query-date DATE, --auto-insert (write each violation to SQLite immediately),
          --query-mode auto|full (default auto; full = scan all detail pages without early break)
    """
    p = {"plate": "", "query_date": time.strftime("%Y-%m-%d"), "resume_from": "0", "auto_insert": False, "query_mode": "auto"}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--plate" and i + 1 < len(args):
            p["plate"] = args[i + 1]; i += 2
        elif args[i] == "--query-date" and i + 1 < len(args):
            p["query_date"] = args[i + 1]; i += 2
        elif args[i] == "--resume-from" and i + 1 < len(args):
            p["resume_from"] = args[i + 1]; i += 2
        elif args[i] == "--auto-insert":
            p["auto_insert"] = True; i += 1
        elif args[i] == "--query-mode" and i + 1 < len(args):
            p["query_mode"] = args[i + 1]; i += 2
        else:
            i += 1

    plate = p["plate"]
    query_date = p["query_date"]
    resume_from = int(p["resume_from"])
    auto_insert = p["auto_insert"]
    query_mode = p["query_mode"]

    # Open DB connection if auto-insert mode
    db_conn = None
    if auto_insert:
        _init_db()
        db_conn = _get_db_conn()

    # Dismiss popup
    _dismiss_popup_js()
    time.sleep(0.5)

    # Setup XHR monitor to catch silent API rate-limiting
    _setup_xhr_monitor()

    # Detect Beijing platform - requires clicking a.view element instead of cell ref
    is_beijing = False
    try:
        url_check = _run(["pinchtab", "eval", "(function(){return window.location.hostname})()"])
        is_beijing = 'bj.122.gov.cn' in url_check.stdout
    except Exception:
        pass

    # Load existing violations from DB for comparison
    existing_violations = _load_violations_from_db(plate)

    all_results = []
    detail_page = max(resume_from, 0)

    while True:
        # Extract violations from current detail page
        violations, total_pages = _extract_detail_page_violations()

        if not violations:
            break

        if is_beijing:
            # Beijing: use JS index (a.view), skip snap/refs
            for idx, v in enumerate(violations):
                unique_key = f"{plate}_{v['time']}_{v['location'][:20]}_{v['behavior'][:30]}"
                existing = existing_violations.get(unique_key)
                if existing and not (
                    existing.get("handling_status_label", "") != v['status'] or
                    existing.get("payment_status_label", "") != v['payment']
                ):
                    all_results.append({
                        "time": v['time'], "location": v['location'],
                        "behavior": v['behavior'], "status": v['status'],
                        "payment": v['payment'],
                        "fine": existing.get("fine_amount", 0),
                        "points": existing.get("points", 0),
                        "authority": existing.get("authority", ""),
                        "unprocessed": v['unprocessed'],
                        "from_db": True, "status_changed": False, "_index": idx
                    })
                    continue

                needs_detail = v['unprocessed'] or (v['status'] == '未处理') or (v['payment'] == '未缴费')
                if not needs_detail:
                    all_results.append({
                        "time": v['time'], "location": v['location'],
                        "behavior": v['behavior'], "status": v['status'],
                        "payment": v['payment'], "fine": 0, "points": 0,
                        "authority": "", "unprocessed": False,
                        "skipped": True, "_detail_page": detail_page, "_index": idx
                    })
                    continue

                time.sleep(random.uniform(1, 2))
                _run(["pinchtab", "eval",
                    f"(function(){{var links=document.querySelectorAll('a.view');if(links.length>{idx}){{links[{idx}].click();return'ok'}}return'fail'}})()"])
                time.sleep(random.uniform(2, 3))

                # Check XHR rate-limiting
                xhr_blocked, xhr_reason = _check_xhr_rate_limit()
                if xhr_blocked:
                    all_results.append({"_rate_limited": True, "_reason": xhr_reason})
                    if db_conn:
                        db_conn.close()
                    print(json.dumps(all_results, ensure_ascii=False, indent=2))
                    return

                # Get dialog text via JS with retry (Beijing dialog may load via XHR)
                dialog_text = ""
                for retry in range(3):
                    time.sleep(1)
                    dialog_text = _run(["pinchtab", "eval",
                        """(function(){var d=document.querySelector('.aui_dialog');if(!d||window.getComputedStyle(d).display==='none')return'';var t=d.textContent.trim();return t.length>20?t:'';})()"""]).stdout
                    if dialog_text and len(dialog_text) > 50:
                        break
                detail = _parse_detail_popup(dialog_text)
                detail["_index"] = idx
                detail["time"] = v["time"]
                detail["location"] = v["location"]
                detail["behavior"] = v["behavior"]
                detail["status"] = v["status"]
                detail["payment"] = v["payment"]
                detail["unprocessed"] = v['unprocessed']
                detail["_detail_page"] = detail_page
                detail["from_db"] = False
                all_results.append(detail)

                # Auto-insert to DB immediately (before closing dialog)
                if auto_insert and db_conn:
                    try:
                        record = _collect_detail_to_db_record(detail, plate, query_date, db_conn)
                        _upsert_violation(db_conn, record)
                        db_conn.commit()
                    except Exception as e:
                        print(f"    DB insert warning: {e}", file=sys.stderr)

                # Close dialog via JS
                _run(["pinchtab", "eval",
                    """(function(){var el=document.querySelector('.aui_dialog');if(!el)return'none';var btns=el.querySelectorAll('button,a,span');for(var i=0;i<btns.length;i++){if(btns[i].textContent.trim()==='取消'){btns[i].click();return'closed';}}document.body.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',code:'Escape',keyCode:27}));return'escape';})()"""])
                time.sleep(random.uniform(1, 2))

                if idx < len(violations) - 1:
                    time.sleep(random.uniform(2, 5))

        else:
            # Detect if this platform uses <a class="view"> links (same as Beijing)
            has_view_links = False
            try:
                view_check = _run(["pinchtab", "eval",
                    "(function(){var l=document.querySelectorAll('a.view');return l.length})()"])
                has_view_links = int((view_check.stdout or "0").strip()) > 0
            except Exception:
                pass

            # Snap for refs (fallback if no a.view links)
            snap = _run(["pinchtab", "snap"])
            snap_text = snap.stdout
            detail_refs = []
            for line in snap_text.split('\n'):
                if 'cell "查看详情"' in line:
                    m = re.match(r'e(\d+):cell "查看详情"', line.strip())
                    if m:
                        detail_refs.append(f"e{m.group(1)}")

            for idx, v in enumerate(violations):
                # Build unique key: plate + time + location + behavior[:30]
                unique_key = f"{plate}_{v['time']}_{v['location'][:20]}_{v['behavior'][:30]}"

                # Check if exists in DB
                existing = existing_violations.get(unique_key)

                if existing:
                    # Check status change
                    old_status = existing.get("handling_status_label", "")
                    old_payment = existing.get("payment_status_label", "")
                    new_status = v['status']
                    new_payment = v['payment']

                    status_changed = (old_status != new_status) or (old_payment != new_payment)

                    if not status_changed:
                        # Skip - already recorded, no change
                        all_results.append({
                            "time": v['time'], "location": v['location'],
                            "behavior": v['behavior'], "status": v['status'],
                            "payment": v['payment'],
                            "fine": existing.get("fine_amount", 0),
                            "points": existing.get("points", 0),
                            "authority": existing.get("authority", ""),
                            "unprocessed": v['unprocessed'],
                            "from_db": True, "status_changed": False, "_index": idx
                        })
                        continue
                    # Status changed - re-query
                    v['_status_changed'] = True

                # Determine if we need detail click
                needs_detail = v['unprocessed'] or v.get('_status_changed') or \
                              (v['status'] == '未处理') or (v['payment'] == '未缴费')

                if needs_detail:
                    time.sleep(random.uniform(1, 2))
                    if has_view_links:
                        # Use JS click on a.view (same as Beijing flow)
                        click_r = _run(["pinchtab", "eval",
                            f"(function(){{var links=document.querySelectorAll('a.view');if(links.length>{idx}){{links[{idx}].click();return'ok'}}return'fail'}})()"])
                    elif idx < len(detail_refs):
                        _run(["pinchtab", "click", detail_refs[idx]])
                    else:
                        continue
                    time.sleep(random.uniform(1, 2))

                    # Check for silent XHR rate-limiting
                    xhr_blocked, xhr_reason = _check_xhr_rate_limit()
                    if xhr_blocked:
                        all_results.append({"_rate_limited": True, "_reason": xhr_reason})
                        print(json.dumps(all_results, ensure_ascii=False, indent=2))
                        return

                    # Get dialog text via JS with retry (use same approach as Beijing flow)
                    # pinchtab text captures full page text and misses popup content on some platforms
                    dialog_text = ""
                    for retry in range(3):
                        time.sleep(0.8)
                        dialog_text = _run(["pinchtab", "eval",
                            """(function(){
                              var selectors=['.aui_dialog','.aui_state_focus','.aui_state_highlight',
                                '.el-dialog__wrapper','.el-dialog',
                                '.ant-modal-wrap','[role="dialog"]',
                                'div[class*="dialog"]','div[class*="modal"]',
                                'div[class*="aui_state"]'];
                              for(var i=0;i<selectors.length;i++){
                                try{
                                  var d=document.querySelector(selectors[i]);
                                  if(!d)continue;
                                  if(window.getComputedStyle(d).display==='none')continue;
                                  var t=d.textContent.trim();
                                  if(t.indexOf('号牌号码')!==-1||t.indexOf('罚款')!==-1)return t;
                                }catch(e){}
                              }
                              var all=document.body.querySelectorAll('*');
                              for(var j=0;j<all.length;j++){
                                try{
                                  var el=all[j],cs=window.getComputedStyle(el);
                                  if(cs.display==='none'||cs.visibility==='hidden')continue;
                                  if(cs.position==='fixed'||cs.position==='absolute'){
                                    if((parseInt(cs.zIndex)||0)>=100){
                                      var txt=el.textContent.trim();
                                      if(txt.length>30&&(txt.indexOf('号牌号码')!==-1||txt.indexOf('罚款')!==-1))return txt;
                                    }
                                  }
                                }catch(e){}
                        }
                        return'';
                        })()"""]).stdout
                        if dialog_text and len(dialog_text) > 50:
                            break
                    detail = _parse_detail_popup(dialog_text)
                    detail["_index"] = idx
                    detail["time"] = v["time"]
                    detail["location"] = v["location"]
                    detail["behavior"] = v["behavior"]
                    detail["status"] = v["status"]
                    detail["payment"] = v["payment"]
                    detail["unprocessed"] = v['unprocessed']
                    detail["_detail_page"] = detail_page
                    detail["from_db"] = False
                    all_results.append(detail)

                    # Auto-insert to DB immediately (before closing popup)
                    if auto_insert and db_conn:
                        try:
                            record = _collect_detail_to_db_record(detail, plate, query_date, db_conn)
                            _upsert_violation(db_conn, record)
                            db_conn.commit()
                        except Exception as e:
                            print(f"    DB insert warning: {e}", file=sys.stderr)

                    _close_popup()
                    time.sleep(random.uniform(1, 2))
                else:
                    all_results.append({
                        "time": v['time'], "location": v['location'],
                        "behavior": v['behavior'], "status": v['status'],
                        "payment": v['payment'], "fine": 0, "points": 0,
                        "authority": "", "unprocessed": False,
                        "skipped": True, "_detail_page": detail_page, "_index": idx
                    })

                if idx < len(violations) - 1:
                    time.sleep(random.uniform(2, 5))

        # Check if more detail pages exist.
        # In auto mode: skip remaining detail pages if no unprocessed on this page
        #   (platform sorts unprocessed first — clean page means all remaining clean).
        # In full mode: scan every detail page regardless.
        has_unprocessed = any(v.get('unprocessed') or v.get('status') == '未处理' for v in violations)
        detail_page += 1
        if detail_page >= total_pages or total_pages <= 1:
            break
        if query_mode == 'auto' and not has_unprocessed:
            break

        # Smart pagination: navigate to the next detail page
        time.sleep(random.uniform(2, 5))
        ok = _click_detail_page(str(detail_page + 1))  # 1-based page number
        if not ok:
            break

    # Rate-limit check
    rate = _check_rate_limit()
    if rate["blocked"]:
        all_results.append({"_rate_limited": True, "_keywords": rate["keywords_found"]})

    # Close DB connection if auto-insert was used
    if db_conn:
        db_conn.close()

    print(json.dumps(all_results, ensure_ascii=False, indent=2))


def _extract_detail_page_violations():
    """Extract violation rows from current detail page. Returns (violations, total_pages)."""
    js = """
(function() {
  var rows = document.querySelectorAll('table tr');
  var violations = [];
  for (var r = 0; r < rows.length; r++) {
    var tds = rows[r].querySelectorAll('td');
    if (tds.length >= 9) {
      var action = tds[8].textContent.trim();
      if (action === '查看详情') {
        violations.push({
          plate_type: tds[1].textContent.trim(),
          plate: tds[2].textContent.trim(),
          time: tds[3].textContent.trim(),
          location: tds[4].textContent.trim(),
          behavior: tds[5].textContent.trim(),
          status: tds[6].textContent.trim(),
          payment: tds[7].textContent.trim(),
          unprocessed: tds[6].textContent.trim() === '未处理'
        });
      }
    }
  }

  // Check for detail page pagination
  var links = document.querySelectorAll('a');
  var pages = [];
  for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim();
    if (/^\\d+$/.test(t)) { var n = parseInt(t); if (n <= 200) pages.push(n); }
  }
  pages.sort(function(a,b){return a-b;});
  var total = pages.length > 0 ? pages[pages.length - 1] : 1;

  return JSON.stringify({violations: violations, total_pages: total});
})()
"""
    result = _run(["pinchtab", "eval", js])
    try:
        out = result.stdout.strip()
        m = re.search(r'\{.*\}', out, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            return data.get("violations", []), data.get("total_pages", 1)
    except (json.JSONDecodeError, ValueError):
        pass
    return [], 1


def _click_detail_page(target):
    """Click pagination on the violation detail page. Reuses same smart-pagination
    pattern as vehicle list click-page.

    Args: --target next|prev|N (page number, 1-based)
    For numeric targets, uses smart pagination: if target not visible, navigates
    via max-page hops until target appears in pagination window.
    """
    if target in ("next", "prev"):
        _click_page_direct(target)
        return True

    target_page = int(target)
    visited_pages = set()
    visited_actions = set()
    stale_count = 0

    while True:
        time.sleep(random.uniform(1, 2))
        pi = _get_pagination_state()
        if pi is None:
            return False

        min_p = pi["min_page"]
        max_p = pi["max_page"]

        if min_p <= target_page <= max_p:
            result = _click_page_number(target_page)
            if "clicked" in result:
                return True
            if target_page not in visited_pages:
                visited_pages.add(target_page)
                stale_count = 0
                continue

        progressed = False

        if target_page > max_p:
            if max_p not in visited_pages:
                visited_pages.add(max_p)
                _click_page_number(max_p)
                stale_count = 0; progressed = True; continue
            if "next" not in visited_actions:
                visited_actions.add("next")
                _click_page_direct("next")
                stale_count = 0; progressed = True; continue

        elif target_page < min_p:
            if min_p not in visited_pages:
                visited_pages.add(min_p)
                _click_page_number(min_p)
                stale_count = 0; progressed = True; continue
            if "prev" not in visited_actions:
                visited_actions.add("prev")
                _click_page_direct("prev")
                stale_count = 0; progressed = True; continue

        if not progressed:
            stale_count += 1
            if stale_count >= 3:
                return False
            time.sleep(random.uniform(1, 2))

    return False


def _get_detail_page_state():
    """Extract current page and total pages from the violation detail view."""
    js = """
(function() {
  var links = document.querySelectorAll('a');
  var pages = [];
  for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim();
    if (/^\\d+$/.test(t)) { var n = parseInt(t); if (n <= 200) pages.push(n); }
  }
  pages.sort(function(a,b){return a-b;});
  if (pages.length === 0) return JSON.stringify({current: 1, min_page: 1, max_page: 1, total: 1});
  // Current page: find non-link or highlighted page number near pagination
  var current = 1;
  var all = document.querySelectorAll('a,span,li,strong,b');
  for (var j = 0; j < all.length; j++) {
    var t = all[j].textContent.trim();
    if (/^\\d+$/.test(t) && all[j].tagName !== 'A') { current = parseInt(t); break; }
  }
  return JSON.stringify({
    current: current,
    min_page: pages[0],
    max_page: pages[pages.length - 1],
    total: pages[pages.length - 1]
  });
})()
"""
    result = _run(["pinchtab", "eval", js])
    try:
        m = re.search(r'\{.*\}', result.stdout, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        pass
    return {"current": 1, "min_page": 1, "max_page": 1, "total": 1}


def _close_popup():
    """Try to close a modal/popup dialog. Multi-strategy approach (Issue #4 fix):

    Strategy order (tries next if current fails):
      1. JavaScript dispatchEvent click on close/×/取消 buttons (bypasses pinchtab occlusion check)
      2. PinchTab click on close button refs from snap
      3. JavaScript Escape key event
      4. Direct DOM removal of modal/overlay elements (last resort)

    Returns True if at least one strategy was attempted (not whether it succeeded —
    caller should verify by checking for absence of detail links).
    """
    # Strategy 1: JavaScript click on close buttons (bypasses occlusion check entirely)
    js_find_and_click_close = """
(function() {
  // Find close buttons by text content
  var allElements = document.querySelectorAll('button, a, span, div, i');
  var closeTexts = ['关闭', '×', '取消', 'close', 'x'];
  for (var i = 0; i < allElements.length; i++) {
    var el = allElements[i];
    var text = (el.textContent || '').trim();
    for (var j = 0; j < closeTexts.length; j++) {
      if (text === closeTexts[j] || text.indexOf(closeTexts[j]) !== -1) {
        // Use dispatchEvent to bypass occlusion/visibility checks
        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
        return 'js-clicked:' + text;
      }
    }
  }
  // Try clicking elements with close-related CSS classes
  var closeSelectors = ['.close', '.el-icon-close', '.dialog-close', '.modal-close',
                        '[class*="close"]', '[class*="Close"]', '.cancel-btn',
                        '.ant-modal-close', '.el-dialog__close'];
  for (var k = 0; k < closeSelectors.length; k++) {
    try {
      var els = document.querySelectorAll(closeSelectors[k]);
      for (var m = 0; m < els.length; m++) {
        els[m].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
        return 'js-clicked-selector:' + closeSelectors[k];
      }
    } catch(e) {}
  }
  return 'no-close-element-found';
})()
"""
    js_result = _run(["pinchtab", "eval", js_find_and_click_close])
    time.sleep(0.5)
    if 'js-clicked' in js_result.stdout:
        return True

    # Strategy 2: PinchTab click on found refs (may fail with occlusion — that's expected)
    snap = _run(["pinchtab", "snap"])
    snap_text = snap.stdout

    close_refs = []
    for line in snap_text.split('\n'):
        if any(kw in line for kw in ['button "关闭"', 'button "×"', 'button "close"',
                                        'button "取消"', 'cell "关闭"', 'cell "×"',
                                        'link "关闭"', 'link "×"', 'button "Close"',
                                        'button "X"']):
            m = re.match(r'e(\d+):', line.strip())
            if m:
                close_refs.append(f"e{m.group(1)}")

    if close_refs:
        # For each ref, try both pinchtab click and JS dispatchEvent
        for ref in close_refs:
            # Try pinchtab click first
            result = _run(["pinchtab", "click", ref])
            time.sleep(0.3)
            # If occluded, fall back to JS dispatchEvent on the same element
            if 'occluded' in result.stdout.lower() or 'error' in result.stderr.lower():
                # Use JS to click the same element by ref pattern
                ref_num = ref[1:]  # e123 -> 123
                js_click_by_idx = f"""
(function() {{
  var all = document.querySelectorAll('button, a, span, div, i');
  var closeTexts = ['关闭', '×', '取消', 'close'];
  for (var i = 0; i < all.length; i++) {{
    var t = (all[i].textContent || '').trim();
    for (var j = 0; j < closeTexts.length; j++) {{
      if (t === closeTexts[j] || t.indexOf(closeTexts[j]) !== -1) {{
        all[i].dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true, view: window}}));
        return 'js-fallback-clicked';
      }}
    }}
  }}
  return 'no-match';
}})()
"""
                _run(["pinchtab", "eval", js_click_by_idx])
                time.sleep(0.3)
            return True

    # Strategy 3: Escape key via JavaScript (bypasses pinchtab keyboard which may not reach)
    _run(["pinchtab", "eval",
          "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', keyCode: 27, bubbles: true}))"])
    time.sleep(0.5)
    # Also try programmatic Esc for any focused element
    _run(["pinchtab", "eval",
          "(function(){var e=new KeyboardEvent('keydown',{key:'Escape',keyCode:27,bubbles:true,cancelable:true});document.activeElement&&document.activeElement.dispatchEvent(e);document.body.dispatchEvent(e)})()"])
    time.sleep(0.3)

    # Strategy 4: Direct DOM removal of modal/overlay (last resort)
    js_remove_modal = """
(function() {
  // Try to find and hide/remove modal overlay elements
  var selectors = [
    '.el-dialog__wrapper', '.el-overlay', '.ant-modal-wrap', '.ant-modal-mask',
    '.modal', '.dialog', '.overlay', '.mask', '[role="dialog"]',
    '.v-modal', '.el-message-box__wrapper', '.el-drawer__wrapper',
    'div[class*="dialog"]', 'div[class*="modal"]', 'div[class*="overlay"]',
    'div[class*="mask"]', 'div[class*="popup"]'
  ];
  var removed = 0;
  for (var i = 0; i < selectors.length; i++) {
    try {
      var els = document.querySelectorAll(selectors[i]);
      for (var j = 0; j < els.length; j++) {
        // Only remove if visible (has non-zero dimensions)
        var rect = els[j].getBoundingClientRect();
        if (rect.width > 0 || rect.height > 0) {
          els[j].style.display = 'none';
          removed++;
        }
      }
    } catch(e) {}
  }
  // Also remove fixed position overlays with high z-index
  var allDivs = document.querySelectorAll('div');
  for (var k = 0; k < allDivs.length; k++) {
    var style = window.getComputedStyle(allDivs[k]);
    if (style.position === 'fixed' && parseInt(style.zIndex) > 100 &&
        (allDivs[k].offsetWidth > 100 || allDivs[k].offsetHeight > 100)) {
      allDivs[k].style.display = 'none';
      removed++;
    }
  }
  return 'removed:' + removed;
})()
"""
    _run(["pinchtab", "eval", js_remove_modal])
    time.sleep(0.5)
    return True

def _parse_detail_popup(text):
    """Parse violation detail popup text into structured dict.
    Handles multiple text formats from the 12123 popup.
    """
    data = {
        "plate": "", "type": "", "time": "", "location": "",
        "behavior": "", "authority": "", "points": 0, "fine": 0,
        "_raw_text": text[:500]
    }

    # Normalize text: collapse multiple newlines and spaces
    normalized = re.sub(r'\n\s*\n', '\n', text)

    m = re.search(r'号牌号码[：:]\s*\n?\s*(\S+)', normalized)
    if m: data["plate"] = m.group(1).strip()
    m = re.search(r'号牌种类[：:]\s*\n?\s*(\S+)', normalized)
    if m: data["type"] = m.group(1).strip()
    m = re.search(r'违法时间[：:]\s*\n?\s*([\d\-:\s]+)', normalized)
    if m: data["time"] = m.group(1).strip()
    m = re.search(r'违法地点[：:]\s*\n?\s*(.+?)(?:\n\s*(?:采集机关|记\s*分|罚))', normalized, re.DOTALL)
    if not m:
        m = re.search(r'违法地点[：:]\s*\n?\s*(.+?)$', normalized, re.DOTALL)
    if m: data["location"] = m.group(1).strip()
    m = re.search(r'违法行为[：:]\s*\n?\s*(.+?)(?:\n\s*(?:采集机关|记\s*分|罚))', normalized, re.DOTALL)
    if not m:
        m = re.search(r'违法行为[：:]\s*\n?\s*(.+?)$', normalized, re.DOTALL)
    if m: data["behavior"] = m.group(1).strip()
    m = re.search(r'采集机关[：:]\s*\n?\s*(.+?)(?:\n\s*(?:记\s*分|罚))', normalized, re.DOTALL)
    if not m:
        m = re.search(r'采集机关[：:]\s*\n?\s*(.+?)$', normalized, re.DOTALL)
    if m: data["authority"] = m.group(1).strip()

    # Points: match "记分 值: N" or "记分: N" or "记分值: N" with possible newlines
    m = re.search(r'记\s*分\s*值?\s*[：:]\s*\n?\s*(\d+)', normalized)
    if not m:
        m = re.search(r'记分[：:]\s*\n?\s*(\d+)', normalized)
    if m: data["points"] = int(m.group(1))

    # Fine amount: handle multiple formats
    # Format 1: "罚款金额：200" or "罚款金额: 200"
    # Format 2: "罚款金额（元）：200" or "罚款金额(元):200"
    # Format 3: "罚款金额 200" (no colon)
    # Format 4: "罚款总金额：200.00元"
    # Format 5: "罚款金额：200元"
    m = re.search(r'罚款(?:总)?金额\s*(?:[(（]元[)）])?\s*[：:]\s*\n?\s*(\d+(?:\.\d+)?)', normalized)
    if not m:
        m = re.search(r'罚款(?:总)?金额\s*\n?\s*(\d+(?:\.\d+)?)', normalized)
    if not m:
        m = re.search(r'罚\s*款\s*[：:]\s*\n?\s*(\d+(?:\.\d+)?)', normalized)
    if not m:
        # Try to find "罚款" anywhere followed by a number
        m = re.search(r'罚\s*款.*?(\d+(?:\.\d+)?)\s*元?', normalized)
    if m:
        data["fine"] = int(float(m.group(1)))

    # F3: debug logging when fine and points are both 0
    if data["fine"] == 0 and data["points"] == 0:
        _write_dialog_debug(data)

    return data


def _write_dialog_debug(data):
    """Write raw dialog text to debug file when fine=0 and points=0.
    Helps diagnose why the regex patterns don't match the platform's format."""
    try:
        data_dir = _get_data_dir()
        debug_dir = os.path.join(data_dir, "dialog_debug")
        os.makedirs(debug_dir, exist_ok=True)
        plate = data.get("plate", "unknown").replace("/", "_").replace("\\", "_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"{plate}_{ts}.txt"
        fpath = os.path.join(debug_dir, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(data.get("_raw_text", "(empty)"))
    except Exception:
        pass  # debug logging must never break the main flow

def cmd_go_back():
    """Navigate back from detail page to vehicle list page.
    Uses history.back() as primary method to preserve the original page position.
    Only falls back to the back-link click if history.back() doesn't work."""
    # Primary: use history.back() to return to list at original page position
    _run(["pinchtab", "eval", "history.back()"])
    time.sleep(random.uniform(1, 2))

    # Verify we're back on the list page
    check = _run(["pinchtab", "eval",
        "(function(){var u=window.location.href;return u.indexOf('vehlist')!==-1||u.indexOf('qrl')!==-1?'list':'detail'})()"])
    if 'list' in check.stdout:
        print("ok")
        return

    # Fallback 1: Find and click the back/return link
    js = """
(function() {
  var links = document.querySelectorAll('a');
  for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim();
    if (t.charCodeAt(0) === 36820) { // 返
      links[i].click();
      return 'clicked-back-link';
    }
  }
  // Try common return/back link patterns
  for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim();
    if (t.indexOf('返回') !== -1 || t.indexOf('退') !== -1) {
      links[i].click();
      return 'clicked-return-link';
    }
  }
  return 'no-back-link';
})()
"""
    _run(["pinchtab", "eval", js])
    time.sleep(random.uniform(1, 2))

    # Verify again
    check2 = _run(["pinchtab", "eval",
        "(function(){var u=window.location.href;return u.indexOf('vehlist')!==-1||u.indexOf('qrl')!==-1?'list':'detail'})()"])
    if 'list' in check2.stdout:
        print("ok")
        return

    # Fallback 2: history.go(-1) as last resort
    _run(["pinchtab", "eval", "history.go(-1)"])
    time.sleep(random.uniform(1, 2))
    print("ok")

def cmd_click_page():
    """Click pagination on the vehicle list page.
    Args: --target next|prev|N (page number)
    For page number targets, uses smart pagination:
    - If target > max displayed page, click max page to shift window right
    - If target < min displayed page, click min page to shift window left
    - Repeat until target found, page range stabilizes, or all visible pages visited.

    No hard retry limit: uses visited-set of page numbers to detect loops.
    For 210-page datasets with ~5-link windows, needs ~40 hops (each hop
    shifts the window by the visible page count). The visited-set ensures
    we don't cycle; when all visible pages are visited, we try next/prev,
    and only exit when no new navigation moves remain.
    """
    p = {"target": "next"}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--target" and i + 1 < len(args):
            p["target"] = args[i + 1]; i += 2
        else:
            i += 1

    target = p["target"]

    if target in ("next", "prev"):
        _click_page_direct(target)
        return

    # Smart pagination for numeric targets
    target_page = int(target)
    visited_pages = set()      # page numbers already clicked
    visited_actions = set()    # "next"/"prev" already tried from current position
    stale_count = 0            # consecutive iterations with no progress

    while True:
        # Slow down between hops to avoid rate limiting
        time.sleep(random.uniform(1, 2))

        page_info = _get_pagination_state()
        if page_info is None:
            print("error: cannot read pagination state")
            return

        current = page_info["current"]
        min_page = page_info["min_page"]
        max_page = page_info["max_page"]

        # Check if target is directly clickable
        if min_page <= target_page <= max_page:
            result = _click_page_number(target_page)
            if "clicked" in result:
                print(f"navigated to page {target_page}")
                return
            # Target in range but not clickable - try to get it visible
            # Click the page nearest to target in the visible range
            if target_page not in visited_pages:
                visited_pages.add(target_page)
                stale_count = 0
                continue

        # Target is beyond range - use smart navigation
        progressed = False

        if target_page > max_page:
            if max_page not in visited_pages:
                visited_pages.add(max_page)
                _click_page_number(max_page)
                stale_count = 0
                progressed = True
                continue
            # Max page already visited, try next button
            if "next" not in visited_actions:
                visited_actions.add("next")
                _click_page_direct("next")
                stale_count = 0
                progressed = True
                continue

        elif target_page < min_page:
            if min_page not in visited_pages:
                visited_pages.add(min_page)
                _click_page_number(min_page)
                stale_count = 0
                progressed = True
                continue
            if "prev" not in visited_actions:
                visited_actions.add("prev")
                _click_page_direct("prev")
                stale_count = 0
                progressed = True
                continue

        # If we're in the right range but target isn't clickable,
        # try stepping via next/prev to make it appear
        if min_page <= target_page <= max_page and "next" not in visited_actions:
            visited_actions.add("next")
            _click_page_direct("next")
            stale_count = 0
            progressed = True
            continue
        if min_page <= target_page <= max_page and "prev" not in visited_actions:
            visited_actions.add("prev")
            _click_page_direct("prev")
            stale_count = 0
            progressed = True
            continue

        if not progressed:
            stale_count += 1
            if stale_count >= 3:
                print(f"error: stuck at page {current}, cannot reach target {target_page}")
                return
            time.sleep(random.uniform(1, 2))


def _wait_for_page_change():
    """F5: After clicking pagination, wait for the active page indicator to update.
    Polls li.active a text for up to 5 seconds. Returns True if page changed."""
    js = """
(function() {
  var active = document.querySelector('li.active a');
  return active ? active.textContent.trim() : '';
})()
"""
    try:
        before = _run(["pinchtab", "eval", js], timeout=3).stdout.strip()
        # Give the page a moment to start transitioning
        time.sleep(0.5)
        for _ in range(10):
            after = _run(["pinchtab", "eval", js], timeout=3).stdout.strip()
            if after and after != before:
                return True
            time.sleep(0.5)
    except Exception:
        pass
    return False


def _click_page_direct(target):
    """Click next/prev page button. Uses JavaScript text-matching (ref-independent).

    Issue #5 fix: Prefer clicking page numbers over next/prev.
    When next/prev must be used, match by text content first, then by
    CSS selectors, then by character codes as last resort.
    """
    if target == "next":
        js = r"""
(function() {
  // Strategy 1: Match by text content (most robust)
  var links = document.querySelectorAll('a, button, span[role="button"]');
  for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim();
    if (t === '\u4e0b\u4e00\u9875' || t === '下一页' || t.indexOf('下一页') !== -1 ||
        t === '\u4e0b\u9875' || t === '下页' || t === 'next' || t === 'Next') {
      links[i].click();
      return 'clicked-next(text)';
    }
  }
  // Strategy 2: Match by CSS class or aria-label
  var selectors = ['.next', '.pagination-next', '[aria-label="next"]',
                   '[aria-label="下一页"]', '.el-pagination button:last-child',
                   '.ant-pagination-next'];
  for (var j = 0; j < selectors.length; j++) {
    try {
      var el = document.querySelector(selectors[j]);
      if (el) { el.click(); return 'clicked-next(selector)'; }
    } catch(e) {}
  }
  // Strategy 3: Character code matching (legacy fallback)
  for (var k = 0; k < links.length; k++) {
    var t = links[k].textContent.trim();
    if (t.length >= 2 && t.charCodeAt(0) === 19979) {
      links[k].click();
      return 'clicked-next(charcode)';
    }
  }
  return 'next-link-not-found';
})()
"""
    elif target == "prev":
        js = r"""
(function() {
  var links = document.querySelectorAll('a, button, span[role="button"]');
  for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim();
    if (t === '\u4e0a\u4e00\u9875' || t === '上一页' || t.indexOf('上一页') !== -1 ||
        t === '\u4e0a\u9875' || t === '上页' || t === 'prev' || t === 'Prev') {
      links[i].click();
      return 'clicked-prev(text)';
    }
  }
  var selectors = ['.prev', '.pagination-prev', '[aria-label="prev"]',
                   '[aria-label="上一页"]', '.el-pagination button:first-child',
                   '.ant-pagination-prev'];
  for (var j = 0; j < selectors.length; j++) {
    try {
      var el = document.querySelector(selectors[j]);
      if (el) { el.click(); return 'clicked-prev(selector)'; }
    } catch(e) {}
  }
  for (var k = 0; k < links.length; k++) {
    var t = links[k].textContent.trim();
    if (t.length >= 2 && t.charCodeAt(0) === 19978) {
      links[k].click();
      return 'clicked-prev(charcode)';
    }
  }
  return 'prev-link-not-found';
})()
"""
    result = _run(["pinchtab", "eval", js])
    time.sleep(random.uniform(1, 2))

    # F5: verify page navigation by waiting for active page number to change
    clicked_ok = "clicked" in result.stdout
    if clicked_ok:
        _wait_for_page_change()

    print(result.stdout.strip())


def _click_page_number(page_num):
    """Click a specific page number link."""
    js = f"""
(function() {{
  var links = document.querySelectorAll('a');
  for (var i = 0; i < links.length; i++) {{
    if (links[i].textContent.trim() === '{page_num}') {{
      links[i].click();
      return 'clicked-page-{page_num}';
    }}
  }}
  return 'page-{page_num}-not-found';
}})()
"""
    result = _run(["pinchtab", "eval", js])
    time.sleep(random.uniform(1, 2))
    return result.stdout.strip()


def _get_pagination_state():
    """Extract current pagination state from the page: current, min, max pages."""
    js = """
(function() {
  // Find pagination container
  var pager = document.querySelector('.pagination');
  if (!pager) return JSON.stringify({error: 'no pagination found'});

  // Get current page from li.active > a
  var current = 1;
  var activeLink = pager.querySelector('li.active a');
  if (activeLink) {
    var ct = activeLink.textContent.trim();
    if (/^\\d+$/.test(ct)) current = parseInt(ct);
  }

  // Get visible page numbers from pagination links (exclude 首页/上一页/下一页/末页)
  var pageLinks = pager.querySelectorAll('a[data-page]');
  var pages = [];
  var skipNames = { '首页':1, '上一页':1, '下一页':1, '末页':1 };
  for (var i = 0; i < pageLinks.length; i++) {
    var t = pageLinks[i].textContent.trim();
    if (/^\\d+$/.test(t) && !skipNames[t]) {
      pages.push(parseInt(t));
    }
  }
  pages.sort(function(a,b) { return a - b; });

  // Get total pages from "末页" link's data-page attribute
  var total = pages.length > 0 ? pages[pages.length - 1] : 1;
  var allAnchors = pager.querySelectorAll('a');
  for (var k = 0; k < allAnchors.length; k++) {
    if (allAnchors[k].textContent.trim() === '末页') {
      var dp = allAnchors[k].getAttribute('data-page');
      if (dp) total = parseInt(dp);
    }
  }

  return JSON.stringify({
    current: current,
    min_page: pages.length > 0 ? pages[0] : 1,
    max_page: pages.length > 0 ? pages[pages.length - 1] : 1,
    total_pages: total,
    visible_pages: pages
  });
})()
"""
    result = _run(["pinchtab", "eval", js], timeout=8)
    try:
        out = result.stdout.strip()
        m = re.search(r'\{.*\}', out, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        pass
    return None

def cmd_save_detail_progress():
    """Save/resume detail progress: mark a plate as processed at a given page+index.
    Args (stdin JSON or CLI):
      --page N              Vehicle list page number
      --vehicle-index N     Vehicle index on the page (1-based)
      --plate PLATE         Plate number of last processed vehicle
      --company NAME        Company name (required, used for file isolation)
      --query-date DATE     Query date YYYY-MM-DD (required, used for file isolation)
      --total-violations N  Total violation count so far (optional)
      --detail-page N       Detail page within the vehicle (0-based)
      --violation-index N   Violation index within the detail page (0-based)
      --violation-time T    Timestamp of last processed violation (for cross-ref)
    Writes to details_progress_<company>_<date>.json with resume point.
    """
    p = {"page": "1", "vehicle_index": "0", "plate": "", "company": "",
         "query_date": "", "total_violations": "0",
         "detail_page": "-1", "violation_index": "-1", "violation_time": ""}
    _read_stdin_json(p)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--page" and i + 1 < len(args):
            p["page"] = args[i + 1]; i += 2
        elif args[i] == "--vehicle-index" and i + 1 < len(args):
            p["vehicle_index"] = args[i + 1]; i += 2
        elif args[i] == "--plate" and i + 1 < len(args):
            p["plate"] = args[i + 1]; i += 2
        elif args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        elif args[i] == "--query-date" and i + 1 < len(args):
            p["query_date"] = args[i + 1]; i += 2
        elif args[i] == "--total-violations" and i + 1 < len(args):
            p["total_violations"] = args[i + 1]; i += 2
        elif args[i] == "--detail-page" and i + 1 < len(args):
            p["detail_page"] = args[i + 1]; i += 2
        elif args[i] == "--violation-index" and i + 1 < len(args):
            p["violation_index"] = args[i + 1]; i += 2
        elif args[i] == "--violation-time" and i + 1 < len(args):
            p["violation_time"] = args[i + 1]; i += 2
        else:
            i += 1

    if not p["company"] or not p["query_date"]:
        print(json.dumps({"ok": False, "error": "--company and --query-date are required"}, ensure_ascii=False))
        sys.exit(1)

    data_dir = _get_data_dir()
    safe_company = re.sub(r'[<>:"/\\|?*]', '_', p["company"])
    prog_file = os.path.join(data_dir, f"details_progress_{safe_company}_{p['query_date']}.json")

    # Load existing progress
    progress = {}
    if os.path.exists(prog_file):
        try:
            with open(prog_file, "r", encoding="utf-8") as f:
                progress = json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass

    # Update progress
    progress["last_page"] = int(p["page"])
    progress["last_vehicle_index"] = int(p["vehicle_index"])
    progress["last_plate"] = p["plate"]
    progress["total_violations"] = int(progress.get("total_violations", 0)) + int(p.get("total_violations", 0))

    # Violation-level resume: only save if explicitly provided (>=0)
    detail_page = int(p["detail_page"])
    violation_idx = int(p["violation_index"])
    if detail_page >= 0:
        progress["last_detail_page"] = detail_page
    if violation_idx >= 0:
        progress["last_violation_index"] = violation_idx
    if p["violation_time"]:
        progress["last_violation_time"] = p["violation_time"]

    # Track processed plates
    plates = progress.get("processed_plates", [])
    if p["plate"] and p["plate"] not in plates:
        plates.append(p["plate"])
    progress["processed_plates"] = plates

    with open(prog_file, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)

    print(json.dumps({"ok": True, "resume_page": progress["last_page"],
                       "resume_index": progress["last_vehicle_index"],
                       "resume_plate": progress["last_plate"],
                       "resume_detail_page": progress.get("last_detail_page", -1),
                       "resume_violation_index": progress.get("last_violation_index", -1),
                       "resume_violation_time": progress.get("last_violation_time", "")}, ensure_ascii=False))


def cmd_load_detail_progress():
    """Load the detail progress resume point.
    Args: --company NAME --query-date DATE (both required for file isolation)
    Returns JSON: {resume_page, resume_vehicle_index, resume_plate, processed_plates, total_violations}
    """
    p = {"company": "", "query_date": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        elif args[i] == "--query-date" and i + 1 < len(args):
            p["query_date"] = args[i + 1]; i += 2
        else:
            i += 1

    if not p["company"] or not p["query_date"]:
        print(json.dumps({"resume_page": 1, "resume_vehicle_index": 0,
                          "resume_plate": "", "processed_plates": [],
                          "total_violations": 0, "fresh": True,
                          "error": "--company and --query-date required"}, ensure_ascii=False))
        return

    data_dir = _get_data_dir()
    safe_company = re.sub(r'[<>:"/\\|?*]', '_', p["company"])
    prog_file = os.path.join(data_dir, f"details_progress_{safe_company}_{p['query_date']}.json")

    if not os.path.exists(prog_file):
        print(json.dumps({"resume_page": 1, "resume_vehicle_index": 0,
                          "resume_plate": "", "processed_plates": [],
                          "total_violations": 0, "fresh": True}, ensure_ascii=False))
        return

    try:
        with open(prog_file, "r", encoding="utf-8") as f:
            progress = json.load(f)
    except (json.JSONDecodeError, ValueError):
        progress = {}

    # If progress was cleared (empty dict or just empty plates), treat as fresh
    last_page = progress.get("last_page", 0)
    last_idx = progress.get("last_vehicle_index", 0)

    if last_page == 0:
        # No resume point set, but plates might exist from pre-resume-point era
        # Treat as fresh start
        print(json.dumps({"resume_page": 1, "resume_vehicle_index": 0,
                          "resume_plate": "", "processed_plates": progress.get("processed_plates", []),
                          "total_violations": progress.get("total_violations", 0),
                          "fresh": True, "note": "no resume point, plates list preserved"}, ensure_ascii=False))
        return

    result = {
        "resume_page": last_page,
        "resume_vehicle_index": last_idx,
        "resume_plate": progress.get("last_plate", ""),
        "resume_detail_page": progress.get("last_detail_page", -1),
        "resume_violation_index": progress.get("last_violation_index", -1),
        "resume_violation_time": progress.get("last_violation_time", ""),
        "processed_plates": progress.get("processed_plates", []),
        "total_violations": progress.get("total_violations", 0),
        "fresh": False
    }
    print(json.dumps(result, ensure_ascii=False))


def cmd_reset_detail_progress():
    """Safely reset detail progress. Keeps full vehicle list intact.
    Only clears the detail-level progress (plates processed, resume point).
    Does NOT touch all_vehicles_progress.json.

    Args: --company NAME --query-date DATE (both required)
    """
    p = {"company": "", "query_date": ""}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--company" and i + 1 < len(args):
            p["company"] = args[i + 1]; i += 2
        elif args[i] == "--query-date" and i + 1 < len(args):
            p["query_date"] = args[i + 1]; i += 2
        else:
            i += 1

    if not p["company"] or not p["query_date"]:
        print(json.dumps({"ok": False, "error": "--company and --query-date required"}, ensure_ascii=False))
        sys.exit(1)

    data_dir = _get_data_dir()
    safe_company = re.sub(r'[<>:"/\\|?*]', '_', p["company"])
    prog_file = os.path.join(data_dir, f"details_progress_{safe_company}_{p['query_date']}.json")
    details_file = os.path.join(data_dir, f"violation_details_{safe_company}_{p['query_date']}.json")

    with open(prog_file, "w", encoding="utf-8") as f:
        json.dump({"processed_plates": [], "total_violations": 0,
                   "last_page": 0, "last_vehicle_index": 0, "last_plate": ""}, f, ensure_ascii=False, indent=2)

    with open(details_file, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False)

    print(json.dumps({"ok": True, "message": "detail progress reset, vehicle list untouched"}, ensure_ascii=False))


def cmd_get_page_vehicles():
    """Get vehicles on the current page AND the current page number.
    Returns JSON: {vehicles: [...], page: N, total_pages: N}
    This is the primary command for the page-by-page batch query flow.

    Auto-dismisses popups before extraction.
    """
    # Dismiss any popup first
    _dismiss_popup_js()
    time.sleep(0.5)

    # Extract vehicles from current page
    vehicles_js = """
(function() {
  var vehicles = [];
  // Find the vehicle table by header keyword — querySelector alone
  // returns the first table on the page, which may be empty.
  var tables = document.querySelectorAll('table');
  var table = null;
  for (var i = 0; i < tables.length; i++) {
    if (tables[i].textContent.indexOf('号牌号码') !== -1) {
      table = tables[i];
      break;
    }
  }
  if (!table) { return JSON.stringify({error: 'no table found'}); }

  var rows = table.querySelectorAll('tr');
  for (var r = 0; r < rows.length; r++) {
    var tds = rows[r].querySelectorAll('td');
    if (tds.length >= 6) {
      var vals = [];
      for (var c = 0; c < tds.length; c++) {
        vals.push(tds[c].textContent.trim());
      }
      var first = vals[0] || '';
      // Chinese plate: province char + letter, 7-8 chars
      if (/^[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁][A-Z]/.test(first)) {
        vehicles.push({
          plate: vals[0] || '',
          type: vals[1] || '',
          status: vals[2] || '',
          inspection_date: vals[3] || '',
          scrap: vals[4] || '',
          unprocessed: parseInt(vals[5]) || 0
        });
      }
    }
  }
  return JSON.stringify(vehicles);
})()
"""
    v_result = _run(["pinchtab", "eval", vehicles_js])
    vehicles = []
    try:
        out = v_result.stdout.strip()
        m = re.search(r'\[.*\]', out, re.DOTALL)
        if m:
            vehicles = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        pass

    # 🔴 Page drift auto-recovery: if no vehicles found, check whether
    # we strayed to a detail page and navigate back to vehicle list.
    if len(vehicles) == 0:
        url_check = _run(["pinchtab", "eval",
            "(function(){var u=window.location.href;return u.indexOf('vehlist')!==-1?'list':'other'})()"])
        if 'other' in url_check.stdout:
            _dismiss_popup_js()
            time.sleep(0.5)
            snap = _run(["pinchtab", "snap"])
            ref = None
            for line in snap.stdout.split('\n'):
                if '我的主页' in line:
                    m2 = re.match(r'(e\d+)', line.strip())
                    if m2:
                        ref = m2.group(1)
                        break
            if ref:
                _run(["pinchtab", "click", ref])
                time.sleep(random.uniform(3, 5))
            # Retry extraction
            v_result2 = _run(["pinchtab", "eval", vehicles_js])
            try:
                out2 = v_result2.stdout.strip()
                m3 = re.search(r'\[.*\]', out2, re.DOTALL)
                if m3:
                    vehicles = json.loads(m3.group(0))
            except (json.JSONDecodeError, ValueError):
                pass

    # Get pagination
    page_state = _get_pagination_state()
    current_page = page_state.get("current", 1) if page_state else 1
    total_pages = page_state.get("total_pages", page_state.get("max_page", 1)) if page_state else 1

    result = {
        "vehicles": vehicles,
        "page": current_page,
        "total_pages": total_pages
    }
    print(json.dumps(result, ensure_ascii=False))


def cmd_find_plate_page():
    """Find which page a plate is on, starting from current page.
    If plate not found on current page, try next 3 pages.
    If still not found, reset to page 1 and scan page by page.

    Args: --plate PLATE --max-forward N (default 3)

    Returns JSON: {found: bool, page: N, method: "current"|"forward"|"scan"}
    """
    p = {"plate": "", "max_forward": "3"}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--plate" and i + 1 < len(args):
            p["plate"] = args[i + 1]; i += 2
        elif args[i] == "--max-forward" and i + 1 < len(args):
            p["max_forward"] = args[i + 1]; i += 2
        else:
            i += 1

    plate = p["plate"]
    max_forward = int(p["max_forward"])

    if not plate:
        print(json.dumps({"found": False, "error": "missing --plate"}))
        return

    # Step 1: Check current page
    vehicles = _get_current_page_vehicles()
    if plate in vehicles:
        page_info = _get_pagination_state()
        pg = page_info["current"] if page_info else 0
        print(json.dumps({"found": True, "page": pg, "method": "current"}))
        return

    # Step 2: Try forward up to max_forward pages (data may have shifted)
    for fwd in range(1, max_forward + 1):
        time.sleep(random.uniform(1, 2))  # slow down
        _click_page_direct("next")
        time.sleep(random.uniform(1, 2))
        vehicles = _get_current_page_vehicles()
        if plate in vehicles:
            page_info = _get_pagination_state()
            pg = page_info["current"] if page_info else 0
            print(json.dumps({"found": True, "page": pg, "method": "forward", "forward_count": fwd}))
            return

    # Step 3: Not found - return to page 1 for full scan
    # Navigate back to page 1 using smart pagination
    page_info = _get_pagination_state()
    if page_info:
        min_p = page_info["min_page"]
        if min_p > 1:
            # Click min page to shift window toward page 1
            for _ in range(10):
                _click_page_number(min_p)
                time.sleep(random.uniform(1, 2))
                pi = _get_pagination_state()
                if pi and pi["min_page"] <= 1:
                    break
                min_p = pi["min_page"] if pi else min_p - 5

            # Click page 1 if visible
            pi = _get_pagination_state()
            if pi and 1 >= pi["min_page"] and 1 <= pi["max_page"]:
                _click_page_number(1)
                time.sleep(random.uniform(1, 2))

    print(json.dumps({"found": False, "page": 1, "method": "scan",
                       "message": f"plate {plate} not found in {max_forward} forward pages, reset to page 1"}))


def _get_current_page_vehicles():
    """Get set of plate numbers on the current page. Used internally by find-plate-page."""
    js = """
(function() {
  var plates = [];
  var table = document.querySelector('table');
  if (!table) return JSON.stringify([]);
  var rows = table.querySelectorAll('tr');
  for (var r = 0; r < rows.length; r++) {
    var tds = rows[r].querySelectorAll('td');
    if (tds.length >= 1) {
      var first = tds[0].textContent.trim();
      if (first.length >= 7 && first.length <= 8) {
        plates.push(first);
      }
    }
  }
  return JSON.stringify(plates);
})()
"""
    result = _run(["pinchtab", "eval", js])
    try:
        out = result.stdout.strip()
        m = re.search(r'\[.*\]', out, re.DOTALL)
        if m:
            return set(json.loads(m.group(0)))
    except (json.JSONDecodeError, ValueError):
        pass
    return set()


def cmd_detect_rate_limit():
    """Check if the current page shows rate-limiting or feng-kong warnings.
    Returns JSON: {blocked: bool, keywords_found: [...], should_stop: bool}
    Exit code 1 if blocked (for script use).
    """
    text = _run(["pinchtab", "text"]).stdout
    snap = _run(["pinchtab", "snap"]).stdout
    combined = text + " " + snap

    found = [kw for kw in RATE_LIMIT_KEYWORDS if kw in combined]

    # Also check: is the vehicle table missing but we should be on vehlist?
    has_table = "号牌号码" in snap or "未处理违法" in snap
    on_vehlist = "vehlist" in snap or "租赁车" in snap

    blocked = len(found) > 0 or (on_vehlist and not has_table)

    result = {
        "blocked": blocked,
        "keywords_found": found,
        "should_stop": blocked,
        "details": "rate-limit keywords detected" if found else (
            "vehicle table missing on vehlist page" if (on_vehlist and not has_table) else "ok"
        )
    }
    print(json.dumps(result, ensure_ascii=False))
    if blocked:
        sys.exit(1)


def cmd_dismiss_popup():
    """Dismiss any system popup/modal that blocks the vehicle table.
    Handles: 本人已知晓, 系统提示, 安全提醒, etc.
    Returns JSON: {dismissed: bool, method: str}
    """
    js = """
(function() {
  // Strategy 1: Look for dismiss buttons by text
  var dismissTexts = ['本人已知晓', '确定', '知道了', '关闭', '同意', '确认', '我知道了'];
  var all = document.querySelectorAll('button, a, span[role="button"], div[role="button"]');
  for (var i = 0; i < all.length; i++) {
    var t = (all[i].textContent || '').trim();
    for (var j = 0; j < dismissTexts.length; j++) {
      if (t.indexOf(dismissTexts[j]) !== -1 && all[i].offsetHeight > 0) {
        all[i].click();
        return 'clicked:' + t;
      }
    }
  }

  // Strategy 2: Close buttons (×)
  var closeSelectors = ['.close', '.aui_close', '.el-icon-close', '.dialog-close',
                        '.modal-close', '[class*="close"]', '.layui-layer-close'];
  for (var k = 0; k < closeSelectors.length; k++) {
    try {
      var els = document.querySelectorAll(closeSelectors[k]);
      for (var m = 0; m < els.length; m++) {
        if (els[m].offsetHeight > 0) {
          els[m].click();
          return 'closed:' + closeSelectors[k];
        }
      }
    } catch(e) {}
  }

  // Strategy 3: Look for modal with system notice text
  var modals = document.querySelectorAll('div[class*="dialog"], div[class*="modal"], div[class*="popup"], div[class*="notice"], .layui-layer');
  for (var n = 0; n < modals.length; n++) {
    var text = (modals[n].textContent || '');
    if (text.indexOf('妥善保管') !== -1 || text.indexOf('系统提示') !== -1 ||
        text.indexOf('安全提醒') !== -1 || text.indexOf('本人已知晓') !== -1) {
      // Find the confirm button inside this modal
      var btns = modals[n].querySelectorAll('button, a');
      for (var p = 0; p < btns.length; p++) {
        if (btns[p].offsetHeight > 0) {
          btns[p].click();
          return 'modal-btn-clicked';
        }
      }
    }
  }

  return 'no-popup-found';
})()
"""
    result = _run(["pinchtab", "eval", js])
    dismissed = 'clicked' in result.stdout or 'closed' in result.stdout or 'modal' in result.stdout
    print(json.dumps({"dismissed": dismissed, "method": result.stdout.strip()}, ensure_ascii=False))




"""
Phase 2: For each vehicle with violations in DB, search -> open -> collect -> go back.
Usage: python3 -u collect_violations.py --company "<公司名>" --batch-id "20260713_001"
              --tab-id "<tab_id>" --instance-port <port>

Uses pinchtab eval JS for search box interaction (search-vehicle not yet in helper).
No page navigation logic. Only search-based single vehicle queries.
"""
import subprocess, json, time, random, sys, os, sqlite3

# === Force unbuffered output (Rule: 批量脚本完成判定铁律) ===
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None
sys.stderr.reconfigure(line_buffering=True) if hasattr(sys.stderr, 'reconfigure') else None

# === Resolve paths ===
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(SKILL_DIR, 'violation_helper.py')
DATA_DIR = os.path.join(os.getcwd(), 'violation_query', 'data')
DB_PATH = os.path.join(DATA_DIR, 'violations.db')
py = 'python3'

# === Parse CLI args ===
args = sys.argv[1:]
company = None
batch_id = None
tab_id = None
instance_port = None

i = 0
while i < len(args):
    if args[i] == '--company' and i + 1 < len(args):
        company = args[i + 1]; i += 2
    elif args[i] == '--batch-id' and i + 1 < len(args):
        batch_id = args[i + 1]; i += 2
    elif args[i] == '--tab-id' and i + 1 < len(args):
        tab_id = args[i + 1]; i += 2
    elif args[i] == '--instance-port' and i + 1 < len(args):
        instance_port = args[i + 1]; i += 2
    else:
        i += 1

missing = []
if not company: missing.append('--company')
if not batch_id: missing.append('--batch-id')
if not tab_id: missing.append('--tab-id')
if not instance_port: missing.append('--instance-port')
if missing:
    print(f"ERROR: Missing required arguments: {', '.join(missing)}")
    sys.exit(1)

date = batch_id[:4] + '-' + batch_id[4:6] + '-' + batch_id[6:8]

os.environ['VIOLATION_TAB_ID'] = tab_id
os.environ['VIOLATION_INSTANCE_PORT'] = instance_port

print(f"Phase 2: company={company}, batch={batch_id}, date={date}")

def h(cmd_args):
    result = subprocess.run([py, HELPER] + cmd_args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    return result.stdout.strip()

def pt(cmd_args):
    """Run pinchtab CLI with server+tab injected."""
    full = ['pinchtab', '--server', f'http://127.0.0.1:{instance_port}',
            '--tab', tab_id] + cmd_args
    result = subprocess.run(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    return result.stdout.strip()

# === Get company_id ===
company_id = json.loads(h(['db-insert-company', '--name', company, '--query-date', date]))['company_id']
print(f"Company ID: {company_id}")

# === Read query list from DB ===
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute("""
    SELECT plate_number, unprocessed_count
    FROM vehicles
    WHERE company_id = ? AND query_date = ? AND unprocessed_count > 0
    ORDER BY plate_number
""", (company_id, date))
violation_vehicles = [(r[0], r[1]) for r in cur.fetchall()]
conn.close()

print(f"Phase 2: {len(violation_vehicles)} vehicles with violations to process")

# === Load progress (续跑) ===
prog = json.loads(h(['load-detail-progress', '--company', company, '--batch-id', batch_id]))
processed = set(prog.get('processed_plates', []))
resume_detail_page = prog.get('resume_detail_page', -1)
resume_plate = prog.get('resume_plate', '')
print(f"Already processed: {len(processed)}")

# === Stats tracking ===
new_violations_total = 0
new_points_total = 0
new_fine_total = 0
failed_vehicles = {}  # plate → unprocessed_count (dict for second pass)

# ── Per-vehicle query helper ──

def _query_one_vehicle(plate, unprocessed_count, search_start):
    """Query one vehicle: search → confirm → (retry once) → open → collect → back.
    Returns (success: bool, new_vios: int, new_pts: int, new_fine: int, next_search_start: float).
    Side effects: updates DB, saves progress, modifies processed set.
    """
    global new_violations_total, new_points_total, new_fine_total

    # === 车间保底 10s：从上一台搜索开始计时 ===
    if search_start is not None:
        elapsed = time.time() - search_start
        if elapsed < 10:
            wait = 10 - elapsed
            print(f"    [10s floor] elapsed={elapsed:.1f}s, waiting {wait:.1f}s")
            time.sleep(wait)

    # Dismiss popups first
    h(['dismiss-popup'])

    # === Search with confirmation (1s fixed + polling max 3s total + 1 retry) ===
    new_search_start = time.time()  # Mark for 10s floor

    plate_no_prefix = plate[1:] if len(plate) > 1 else plate
    search_js = f"""
    (function() {{
        var typeSel = document.querySelector('select[id*="hpzl"], select[name*="hpzl"]');
        if (!typeSel) {{ var sels = document.querySelectorAll('select'); for (var i=0; i<sels.length; i++) {{ if (sels[i].options && sels[i].options.length >= 2) {{ typeSel = sels[i]; break; }} }} }}
        if (typeSel) {{ typeSel.value = '52'; typeSel.dispatchEvent(new Event('change', {{bubbles:true}})); }}

        var inputs = document.querySelectorAll('input[type="text"], input:not([type])');
        var plateInput = null;
        for (var i=0; i<inputs.length; i++) {{
            var ph = inputs[i].placeholder || '';
            if (ph.indexOf('车牌') >= 0 || ph.indexOf('号码') >= 0) {{ plateInput = inputs[i]; break; }}
        }}
        if (!plateInput && inputs.length > 0) {{ plateInput = inputs[inputs.length-1]; }}

        if (plateInput) {{
            plateInput.value = '';
            plateInput.focus();
            var nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            nativeSetter.call(plateInput, '{plate_no_prefix}');
            plateInput.dispatchEvent(new Event('input', {{bubbles:true}}));
            plateInput.dispatchEvent(new Event('change', {{bubbles:true}}));
        }}

        try {{
            var events = jQuery._data(document.getElementById('jdcquery'), 'events');
            if (events && events.click && events.click[0]) {{
                events.click[0].handler();
                return 'SEARCHED_' + '{plate}';
            }}
        }} catch(e) {{}}

        var buttons = document.querySelectorAll('button');
        for (var i=0; i<buttons.length; i++) {{
            if (buttons[i].textContent.indexOf('搜索') >= 0 || buttons[i].innerText.indexOf('搜索') >= 0) {{
                buttons[i].click();
                return 'SEARCHED_' + '{plate}';
            }}
        }}
        return 'NO_SEARCH_BTN';
    }})()
    """
    search_result = pt(['eval', search_js])
    print(f"    search: {search_result}")

    # Confirm search results loaded: 1s fixed + polling (max 3s total)
    found = False
    time.sleep(1.0)
    check_js = f"(function(){{return document.body.innerText.indexOf('{plate}')!==-1}})()"
    for poll in range(10):  # 10 × 0.2s = 2s more, total max 3s
        if pt(['eval', check_js]).strip() == 'true':
            found = True
            print(f"    search confirmed: {1.0 + poll * 0.2:.1f}s")
            break
        time.sleep(0.2)

    # Retry once immediately if not found
    if not found:
        print(f"    search not confirmed, retrying...")
        h(['dismiss-popup'])
        pt(['eval', search_js])
        time.sleep(1.0)
        for poll in range(15):  # 15 × 0.2s = 3s for retry
            if pt(['eval', check_js]).strip() == 'true':
                found = True
                print(f"    retry confirmed: {1.0 + poll * 0.2:.1f}s")
                break
            time.sleep(0.2)

    if not found:
        print(f"    SKIP: search failed after retry for {plate}")
        # Mark query failure + record attempt time
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE vehicles SET status_code = 'query_failed', last_queried_at = datetime('now','localtime') WHERE plate_number = ? AND company_id = ?",
            (plate, company_id))
        conn.commit()
        conn.close()
        return (False, 0, 0, 0, new_search_start)

    # Open vehicle (always index 1 after search filters to single result)
    open_result = h(['open-vehicle', '--index', '1'])
    print(f"    open: {open_result[:100]}")

    # Collect violation details (Rule #12: must enter detail page for each violation)
    collect_args = ['collect-violations', '--plate', plate, '--query-date', date, '--auto-insert',
                    '--company', company, '--batch-id', batch_id]
    if plate == resume_plate and resume_detail_page > 0:
        collect_args.extend(['--resume-from', str(resume_detail_page)])
        print(f"    Resuming from detail page {resume_detail_page}")
    violations_out = h(collect_args)
    new_count = 0
    new_pts = 0
    new_fine = 0
    try:
        violations = json.loads(violations_out)
        new_ones = [x for x in violations if not x.get('skipped') and not x.get('from_db')]
        new_count = len(new_ones)
        print(f"    -> {new_count} new violations")
        if new_count > 0:
            for v in new_ones:
                new_pts += v.get('points', 0) or 0
                new_fine += v.get('fine', 0) or 0
        # Record query completion time + clear failure status on recovery
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE vehicles SET last_queried_at = datetime('now','localtime'), status_code = CASE WHEN status_code = 'query_failed' THEN '' ELSE status_code END WHERE plate_number = ? AND company_id = ?",
            (plate, company_id))
        conn.commit()
        conn.close()
    except json.JSONDecodeError:
        print(f"    -> parse error: {violations_out[:200]}")
        # Still record attempt time even on parse failure
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE vehicles SET last_queried_at = datetime('now','localtime') WHERE plate_number = ? AND company_id = ?",
            (plate, company_id))
        conn.commit()
        conn.close()

    # Back to list
    h(['go-back'])

    # Update globals
    new_violations_total += new_count
    new_points_total += new_pts
    new_fine_total += new_fine
    processed.add(plate)

    return (True, new_count, new_pts, new_fine, new_search_start)


# ── First pass: process all vehicles ──

search_start_time = None
first_pass_failed = {}  # plate → unprocessed_count
for idx, (plate, unprocessed_count) in enumerate(violation_vehicles):
    if plate in processed:
        if (idx + 1) % 50 == 0:
            print(f"  [{idx+1}/{len(violation_vehicles)}] {plate}: skip (already processed)")
        continue

    print(f"  [{idx+1}/{len(violation_vehicles)}] {plate}: {unprocessed_count} violations [first pass]")

    success, nv, np, nf, search_start_time = _query_one_vehicle(
        plate, unprocessed_count, search_start_time)

    if success:
        # Save progress (续跑)
        h(['save-detail-progress', '--phase', 'collect',
            '--vehicle-index', str(idx + 1), '--plate', plate,
            '--detail-page', '-1',
            '--company', company, '--batch-id', batch_id])
    else:
        first_pass_failed[plate] = unprocessed_count


# ── Second pass: retry all failed vehicles ──

failed_vehicles = {}  # Final failures after second pass
if first_pass_failed:
    print(f"\n=== Second pass: retrying {len(first_pass_failed)} failed vehicles ===")
    # Reset search_start_time to allow immediate start (no 10s floor between passes)
    retry_search_start = None
    for retry_idx, (plate, unprocessed_count) in enumerate(first_pass_failed.items()):
        print(f"  [{retry_idx+1}/{len(first_pass_failed)}] {plate}: {unprocessed_count} violations [second pass - final retry]")

        success, nv, np, nf, retry_search_start = _query_one_vehicle(
            plate, unprocessed_count, retry_search_start)

        if success:
            print(f"    ✓ recovered on second pass: {nv} new violations")
            # Save progress for recovered vehicles too
            h(['save-detail-progress', '--phase', 'collect',
                '--vehicle-index', str(len(violation_vehicles) + retry_idx + 1),
                '--plate', plate,
                '--detail-page', '-1',
                '--company', company, '--batch-id', batch_id])
        else:
            failed_vehicles[plate] = unprocessed_count
            print(f"    ✗ still failed after second pass")

success_count = len(processed)

# === Query missing vehicle count (tag='不存在') ===
conn = sqlite3.connect(DB_PATH)
cur = conn.execute(
    "SELECT COUNT(*) FROM vehicles WHERE company_id = ? AND tag = '不存在'",
    (company_id,))
missing_count = cur.fetchone()[0]
conn.close()

print(f"\n=== Phase 2 complete ===")
print(f"  Vehicles in query list: {len(violation_vehicles)}")
print(f"  Successfully queried:  {success_count}")
print(f"  Failed (final):        {len(failed_vehicles)}")
if failed_vehicles:
    print(f"  Failed plates: {', '.join(sorted(failed_vehicles))}")
    print(f"  (will retry on next batch run)")
print(f"  New violations: {new_violations_total}")
print(f"  New points:     {new_points_total}")
print(f"  New fine:       {new_fine_total}")
print(f"  Missing (不存在): {missing_count}")

# === Sync to central console via Node Agent (铁律 #0 complement) ===
print("Triggering sync via Node Agent...")
sync_result = h(['sync-now', '--company', company])
try:
    sr = json.loads(sync_result)
    if sr.get('sync_triggered'):
        print(f"  Sync triggered: {sr.get('company')} (via {sr.get('via', 'node_agent')})")
    else:
        print(f"  Sync trigger failed: {sr.get('error', 'unknown')}")
except json.JSONDecodeError:
    print(f"  Sync trigger: parse error: {sync_result[:200]}")

# === Auto-cleanup: mark done + release tab (铁律 #18) ===
h(['mark-task-done', '--company', company, '--query-type', 'batch',
    '--vehicles-queried', str(success_count),
    '--new-violations', str(new_violations_total),
    '--new-points', str(new_points_total),
    '--new-fine', str(new_fine_total),
    '--failed-vehicles', str(len(failed_vehicles)),
    '--missing-vehicles', str(missing_count)])
h(['release-tab'])
print("Tab released.")

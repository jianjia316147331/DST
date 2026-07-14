"""
Phase 1: Scan all vehicle list pages, write to vehicles table.
Usage: python3 -u scan_vehicles.py --company "<公司名>" --batch-id "20260713_001"
              --tab-id "<tab_id>" --instance-port <port>

Uses existing helper subcommands only: get-page-vehicles, click-page, db-insert-vehicle, dismiss-popup.
No detail page access. No JSON manifest. No page navigation in Phase 2.
Automatically resumes from the current browser page (续跑).
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

# Validate required args
missing = []
if not company: missing.append('--company')
if not batch_id: missing.append('--batch-id')
if not tab_id: missing.append('--tab-id')
if not instance_port: missing.append('--instance-port')
if missing:
    print(f"ERROR: Missing required arguments: {', '.join(missing)}")
    print(f"Usage: python3 -u scan_vehicles.py --company '<公司名>' --batch-id '20260713_001' --tab-id '<id>' --instance-port <port>")
    sys.exit(1)

date = batch_id[:4] + '-' + batch_id[4:6] + '-' + batch_id[6:8]

os.environ['VIOLATION_TAB_ID'] = tab_id
os.environ['VIOLATION_INSTANCE_PORT'] = instance_port

print(f"Phase 1: company={company}, batch={batch_id}, date={date}")
print(f"Tab: {tab_id}, Instance: {instance_port}")

def h(cmd_args):
    result = subprocess.run([py, HELPER] + cmd_args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
    return result.stdout.strip()

# === Ensure company record ===
company_id = json.loads(h(['db-insert-company', '--name', company, '--query-date', date]))['company_id']
print(f"Company ID: {company_id}")

# === Load existing plates for tag tracking ===
conn = sqlite3.connect(DB_PATH)
cur = conn.execute(
    "SELECT plate_number, tag FROM vehicles WHERE company_id = ?",
    (company_id,))
existing_plates = {r[0]: r[1] for r in cur.fetchall()}
conn.close()
scanned_plates = set()
print(f"Existing vehicles in DB: {len(existing_plates)}")

total_vehicles = 0
violation_count = 0

# === Resume from breakpoint (续跑) ===
h(['dismiss-popup'])
prog = json.loads(h(['load-detail-progress', '--company', company, '--batch-id', batch_id]))
resume_page = prog.get('resume_page', 1)
if resume_page > 1:
    print(f"Resuming from page {resume_page} (saved breakpoint)...")
    h(['click-page', '--target', str(resume_page)])
    time.sleep(random.uniform(2, 5))
    h(['dismiss-popup'])
    # 续跑：从 DB 补充前一轮已扫描的车牌，保证 scanned_plates 完整
    # （否则后续的校验和"不存在"标记会因数据不完整而误判）
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT plate_number FROM vehicles WHERE company_id = ? AND query_date = ?",
        (company_id, date))
    for row in cur.fetchall():
        scanned_plates.add(row[0])
    conn.close()
    print(f"  Pre-loaded {len(scanned_plates)} plates from DB into scanned_plates")
page_data = json.loads(h(['get-page-vehicles']))
current_page = page_data.get('page', 1)
total_pages = page_data.get('total_pages', 1)
print(f"Starting from page {current_page}/{total_pages}: {len(page_data.get('vehicles', []))} vehicles")

# Extract total vehicle count from page (权威校验基准)
total_count_js = """
(function() {
    var m = document.body.innerText.match(/共\\s*(\\d+)\\s*条/);
    return m ? parseInt(m[1]) : 0;
})()
"""
total_count_str = subprocess.run(
    ['pinchtab', '--server', f'http://127.0.0.1:{instance_port}', '--tab', tab_id, 'eval', total_count_js],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8').stdout.strip()
try:
    expected_total = int(total_count_str)
except ValueError:
    expected_total = total_pages * 10

print(f"Total vehicles on platform: {expected_total} (total_pages={total_pages})")

# Save scan target immediately for completion verification (完成判定铁律 #2)
h(['save-detail-progress', '--phase', 'scan', '--page', str(current_page),
    '--vehicle-index', str(expected_total), '--plate', f'TOTAL={expected_total}',
    '--company', company, '--batch-id', batch_id])
print(f"Scan target saved: {expected_total} vehicles expected")

# === Page scan loop ===
while True:
    vehicles = page_data.get('vehicles', [])
    current_page = page_data.get('page', 1)
    total_pages = page_data.get('total_pages', 1)
    print(f"  Page {current_page}/{total_pages}: {len(vehicles)} vehicles")

    for v in vehicles:
        plate = v['plate']
        unprocessed = v.get('unprocessed', 0)

        # === Tag: 新增 / 恢复 / (空) ===
        tag = ""
        if plate not in existing_plates:
            tag = "新增"
        elif existing_plates.get(plate) == "不存在":
            tag = "恢复"

        scanned_plates.add(plate)

        h(['db-insert-vehicle',
            '--company-id', str(company_id),
            '--plate-number', plate,
            '--plate-type', v.get('type', ''),
            '--plate-type-label', v.get('type_label', v.get('type', '')),
            '--status-code', str(v.get('status_code', '')),
            '--status-label', v.get('status', ''),
            '--inspection-date', v.get('inspection_date', ''),
            '--unprocessed-count', str(unprocessed),
            '--query-date', date,
            '--tag', tag,
            '--tag-batch-id', batch_id if tag else ''])

        total_vehicles += 1
        if unprocessed > 0:
            violation_count += 1

    # Save per-page breakpoint for crash recovery
    last_plate = vehicles[-1]['plate'] if vehicles else ''
    h(['save-detail-progress', '--phase', 'scan', '--page', str(current_page),
        '--vehicle-index', '0', '--plate', last_plate,
        '--company', company, '--batch-id', batch_id])

    if current_page >= total_pages:
        print(f"  Page {current_page}/{total_pages} — last page, done.")
        break

    next_page = current_page + 1
    print(f"  Navigating to page {next_page}...")
    h(['click-page', '--target', str(next_page)])
    time.sleep(random.uniform(2, 5))  # Rule #2: 翻页间隔 2-5 秒
    h(['dismiss-popup'])
    page_data = json.loads(h(['get-page-vehicles']))
    # Page drift detection
    actual_page = page_data.get('page', 0)
    if actual_page != next_page and abs(actual_page - next_page) > 2:
        print(f"  WARNING: Page drift! Expected page {next_page}, got page {actual_page}. "
              f"Continuing from actual page, breakpoint tracks real position.")

# === 完成校验：本轮扫描 vs 平台总数 ===
scanned_count = len(scanned_plates)
if expected_total > 0 and abs(scanned_count - expected_total) > 5:
    print(f"ERROR: Scanned ({scanned_count}) != platform total ({expected_total})")
    print(f"  Diff: {expected_total - scanned_count} vehicles may be missing.")
    print(f"  Missing-vehicle marking SKIPPED. Re-run to fill gaps.")
    print(f"  Re-run: scan_vehicles.py --company '{company}' --batch-id '{batch_id}' ...")
    h(['save-detail-progress', '--phase', 'scan', '--page', str(total_pages),
        '--vehicle-index', '0', '--plate', '', '--company', company, '--batch-id', batch_id])
    sys.exit(2)
else:
    print(f"Validation OK: scanned={scanned_count}, platform={expected_total}")

# === Mark missing vehicles (在 DB 中但本轮未扫描到) ===
missing_plates = set(existing_plates.keys()) - scanned_plates
missing_count = len(missing_plates)

if missing_plates:
    conn = sqlite3.connect(DB_PATH)
    for plate in missing_plates:
        conn.execute(
            "UPDATE vehicles SET tag = ?, tag_batch_id = ? WHERE plate_number = ? AND company_id = ?",
            ("不存在", batch_id, plate, company_id))
    conn.commit()
    conn.close()
    print(f"Marked as '不存在': {missing_count} vehicles")

new_count = sum(1 for p in scanned_plates if p not in existing_plates)
recovered_count = sum(1 for p in scanned_plates if existing_plates.get(p) == "不存在")

# === Mark scan complete (完成判定铁律 #1: 进度文件必须存在) ===
h(['save-detail-progress', '--phase', 'scan', '--page', str(total_pages),
    '--vehicle-index', '0', '--plate', '', '--company', company, '--batch-id', batch_id])

print(f"\n=== Phase 1 complete ===")
print(f"Pages: {total_pages} | Vehicles scanned this run: {total_vehicles} | With violations: {violation_count}")
print(f"Tags: new={new_count} recovered={recovered_count} missing={missing_count}")

# === Auto-cleanup: mark done + release tab (铁律 #18) ===
h(['mark-task-done', '--company', company, '--query-type', 'batch',
    '--vehicles-queried', str(total_vehicles),
    '--new-vehicles', str(new_count),
    '--missing-vehicles', str(missing_count)])
h(['release-tab'])
print("Tab released.")

import pool from './index.js';

export async function upsertCompaniesList(companies: any[]): Promise<void> {
  if (!Array.isArray(companies) || companies.length === 0) return;

  const conn = await pool.getConnection();
  try {
    for (const c of companies) {
      // Only update account_status from sync; province/contact managed via console
      await conn.query(
        `INSERT INTO companies (name, account_status)
         VALUES (?, ?)
         ON DUPLICATE KEY UPDATE account_status=?, updated_at=NOW()`,
        [c.name, c.account_status || 'offline',
         c.account_status || 'offline']
      );
    }
  } finally {
    conn.release();
  }
}

export async function upsertVehicle(vehicle: any, companyIdMap: Map<string, number>, nodeDbId: number | null): Promise<void> {
  const companyId = companyIdMap.get(vehicle.company_name);
  if (!companyId) {
    console.warn(`[Sync] 跳过车辆 ${vehicle.plate_number}: 公司 "${vehicle.company_name}" 未匹配`);
    return;
  }
  const conn = await pool.getConnection();
  try {
    await conn.query(
      `INSERT INTO vehicles (company_id, node_id, plate_number, plate_type, plate_type_label,
       status_code, status_label, inspection_date, unprocessed_count, tag, tag_batch_id, query_date, last_query_time)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON DUPLICATE KEY UPDATE plate_type=?, plate_type_label=?, status_code=?, status_label=?,
       inspection_date=?, unprocessed_count=?, tag=?, tag_batch_id=?, last_query_time=?, updated_at=NOW()`,
      [companyId, nodeDbId, vehicle.plate_number, vehicle.plate_type || null,
       vehicle.plate_type_label || null, vehicle.status_code || null, vehicle.status_label || null,
       vehicle.inspection_date || null, vehicle.unprocessed_count || 0, vehicle.tag || null,
       vehicle.tag_batch_id || null, vehicle.query_date || new Date().toISOString().slice(0, 10),
       vehicle.last_query_time || null,
       vehicle.plate_type || null, vehicle.plate_type_label || null, vehicle.status_code || null,
       vehicle.status_label || null, vehicle.inspection_date || null, vehicle.unprocessed_count || 0,
       vehicle.tag || null, vehicle.tag_batch_id || null, vehicle.last_query_time || null]
    );
  } finally {
    conn.release();
  }
}

export async function upsertViolations(violations: any[], companyIdMap: Map<string, number>, taskId: number | null): Promise<{ inserted: number; updated: number }> {
  if (!Array.isArray(violations) || violations.length === 0) return { inserted: 0, updated: 0 };

  const conn = await pool.getConnection();
  let inserted = 0, updated = 0;
  try {
    for (const v of violations) {
      const companyId = companyIdMap.get(v.company_name);
      if (!companyId) {
        console.warn(`[Sync] 跳过违章 ${v.plate_number}: 公司 "${v.company_name}" 未匹配`);
        continue;
      }
      const [existing] = await conn.query(
        'SELECT id FROM violations WHERE company_id = ? AND plate_number = ? AND violation_time = ? AND violation_behavior = ?',
        [companyId, v.plate_number, v.violation_time || null, v.violation_behavior || null]
      ) as any[];

      if (existing.length > 0) {
        await conn.query(
          `UPDATE violations SET fine_amount=?, points=?, handling_status=?, payment_status=?, province=?,
           last_query_time=COALESCE(?, last_query_time)
           WHERE id=?`,
          [v.fine_amount ?? null, v.points ?? null, v.handling_status || null,
           v.payment_status || null, v.province || null,
           v.last_query_time || null, existing[0].id]
        );
        updated++;
      } else {
        await conn.query(
          `INSERT INTO violations (task_id, company_id, plate_number, violation_time, violation_location,
           violation_behavior, fine_amount, points, handling_status, payment_status, province, query_date, last_query_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          [taskId, companyId, v.plate_number, v.violation_time || null,
           v.violation_location || null, v.violation_behavior || null, v.fine_amount ?? null,
           v.points ?? null, v.handling_status || null, v.payment_status || null,
           v.province || null, v.query_date || new Date().toISOString().slice(0, 10),
           v.last_query_time || null]
        );
        inserted++;
      }
    }
  } finally {
    conn.release();
  }
  return { inserted, updated };
}

/**
 * Query the database for known company names, return as a Set for O(1) lookup.
 */
export async function getKnownCompanyNames(names: string[]): Promise<Set<string>> {
  if (!Array.isArray(names) || names.length === 0) return new Set();
  const conn = await pool.getConnection();
  try {
    const placeholders = names.map(() => '?').join(',');
    const [rows] = await conn.query(
      `SELECT name FROM companies WHERE name IN (${placeholders})`,
      names
    ) as any[];
    return new Set((rows as any[]).map((r: any) => r.name));
  } finally {
    conn.release();
  }
}

/**
 * Resolve company names to MySQL company IDs. Returns Map<company_name, company_id>.
 */
export async function getCompanyNameToIdMap(names: string[]): Promise<Map<string, number>> {
  const map = new Map<string, number>();
  if (!Array.isArray(names) || names.length === 0) return map;
  const conn = await pool.getConnection();
  try {
    const placeholders = names.map(() => '?').join(',');
    const [rows] = await conn.query(
      `SELECT id, name FROM companies WHERE name IN (${placeholders})`,
      names
    ) as any[];
    for (const r of (rows as any[])) {
      map.set(r.name, r.id);
    }
  } finally {
    conn.release();
  }
  return map;
}

export function validateCompanies(companies: any[]): { valid: any[]; errors: string[] } {
  const valid: any[] = [];
  const errors: string[] = [];
  if (!Array.isArray(companies)) return { valid, errors: ['Invalid payload'] };

  for (let i = 0; i < companies.length; i++) {
    const c = companies[i];
    if (!c) {
      errors.push(`Company ${i}: null entry`);
      continue;
    }
    if (!c.name) {
      errors.push(`Company ${i}: missing required field (name)`);
    } else {
      valid.push(c);
    }
  }
  return { valid, errors };
}

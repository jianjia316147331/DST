import pool from './index.js';

export async function upsertCompaniesList(companies: any[], nodeId?: string): Promise<void> {
  if (!Array.isArray(companies) || companies.length === 0) return;

  const conn = await pool.getConnection();
  try {
    for (const c of companies) {
      await conn.query(
        `INSERT INTO companies (name, short_name, province, province_url, contact_name, contact_phone, account_status)
         VALUES (?, ?, ?, ?, ?, ?, ?)
         ON DUPLICATE KEY UPDATE short_name=?, province_url=?, contact_name=?, contact_phone=?, account_status=?, updated_at=NOW()`,
        [c.name, c.short_name || null, c.province, c.province_url, c.contact_name || null,
         c.contact_phone || null, c.account_status || 'offline',
         c.short_name || null, c.province_url, c.contact_name || null, c.contact_phone || null,
         c.account_status || 'offline']
      );
    }
  } finally {
    conn.release();
  }
}

export async function upsertVehicle(vehicle: any): Promise<void> {
  const conn = await pool.getConnection();
  try {
    await conn.query(
      `INSERT INTO vehicles (company_id, node_id, plate_number, plate_type, plate_type_label,
       status_code, status_label, inspection_date, unprocessed_count, tag, tag_batch_id, query_date)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON DUPLICATE KEY UPDATE plate_type=?, plate_type_label=?, status_code=?, status_label=?,
       inspection_date=?, unprocessed_count=?, tag=?, tag_batch_id=?, updated_at=NOW()`,
      [vehicle.company_id, vehicle.node_id || null, vehicle.plate_number, vehicle.plate_type || null,
       vehicle.plate_type_label || null, vehicle.status_code || null, vehicle.status_label || null,
       vehicle.inspection_date || null, vehicle.unprocessed_count || 0, vehicle.tag || null,
       vehicle.tag_batch_id || null, vehicle.query_date || new Date().toISOString().slice(0, 10),
       vehicle.plate_type || null, vehicle.plate_type_label || null, vehicle.status_code || null,
       vehicle.status_label || null, vehicle.inspection_date || null, vehicle.unprocessed_count || 0,
       vehicle.tag || null, vehicle.tag_batch_id || null]
    );
  } finally {
    conn.release();
  }
}

export async function upsertViolations(violations: any[]): Promise<{ inserted: number; updated: number }> {
  if (!Array.isArray(violations) || violations.length === 0) return { inserted: 0, updated: 0 };

  const conn = await pool.getConnection();
  let inserted = 0, updated = 0;
  try {
    for (const v of violations) {
      const [existing] = await conn.query(
        'SELECT id FROM violations WHERE company_id = ? AND plate_number = ? AND violation_time = ? AND violation_behavior = ?',
        [v.company_id, v.plate_number, v.violation_time || null, v.violation_behavior || null]
      ) as any[];

      if (existing.length > 0) {
        await conn.query(
          `UPDATE violations SET fine_amount=?, points=?, handling_status=?, payment_status=?, province=?
           WHERE id=?`,
          [v.fine_amount || null, v.points || null, v.handling_status || null,
           v.payment_status || null, v.province || null, existing[0].id]
        );
        updated++;
      } else {
        await conn.query(
          `INSERT INTO violations (task_id, company_id, plate_number, violation_time, violation_location,
           violation_behavior, fine_amount, points, handling_status, payment_status, province, query_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          [v.task_id || null, v.company_id, v.plate_number, v.violation_time || null,
           v.violation_location || null, v.violation_behavior || null, v.fine_amount || null,
           v.points || null, v.handling_status || null, v.payment_status || null,
           v.province || null, v.query_date || new Date().toISOString().slice(0, 10)]
        );
        inserted++;
      }
    }
  } finally {
    conn.release();
  }
  return { inserted, updated };
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
    if (!c.name || !c.province || !c.province_url) {
      errors.push(`Company ${i}: missing required fields (name, province, province_url)`);
    } else {
      valid.push(c);
    }
  }
  return { valid, errors };
}

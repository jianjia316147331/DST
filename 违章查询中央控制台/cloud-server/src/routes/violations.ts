import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

export default async function violationsRoutes(app: FastifyInstance) {
  // GET /api/violations — list violations with filters
  app.get('/api/violations', { preHandler: [app.authenticate] }, async (request) => {
    const { page = '1', pageSize = '50', plate_number, company_id, task_id } = request.query as Record<string, string>;
    const limit = Math.min(parseInt(pageSize, 10) || 50, 200);
    const offset = (Math.max(parseInt(page, 10), 1) - 1) * limit;

    const conditions: string[] = [];
    const params: any[] = [];

    if (plate_number) {
      conditions.push('v.plate_number LIKE ?');
      params.push(`%${plate_number}%`);
    }
    if (company_id) {
      conditions.push('v.company_id = ?');
      params.push(parseInt(company_id, 10));
    }
    if (task_id) {
      conditions.push('v.task_id = ?');
      params.push(parseInt(task_id, 10));
    }

    const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

    const [rows] = await pool.query(
      `SELECT v.*, COALESCE(c.short_name, c.name) as company_name, n.node_name
       FROM violations v
       LEFT JOIN companies c ON c.id = v.company_id
       LEFT JOIN tasks t ON t.id = v.task_id
       LEFT JOIN nodes n ON n.id = t.node_id
       ${where}
       ORDER BY v.violation_time DESC LIMIT ? OFFSET ?`,
      [...params, limit, offset]
    );
    const [countRows] = await pool.query(
      `SELECT COUNT(*) as total FROM violations v ${where}`,
      params
    );

    return { data: rows, total: (countRows as any[])[0]?.total || 0 };
  });

  // GET /api/violations/stats — summary stats
  app.get('/api/violations/stats', { preHandler: [app.authenticate] }, async () => {
    const [rows] = await pool.query(
      `SELECT COUNT(*) as total,
       SUM(CASE WHEN handling_status = '未处理' THEN 1 ELSE 0 END) as unhandled,
       SUM(CASE WHEN payment_status = '未缴款' THEN 1 ELSE 0 END) as unpaid,
       COALESCE(SUM(fine_amount), 0) as total_fines
       FROM violations`
    );
    return (rows as any[])[0] || { total: 0, unhandled: 0, unpaid: 0, total_fines: 0 };
  });
}

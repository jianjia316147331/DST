import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

export default async function vehicleRoutes(app: FastifyInstance) {
  // GET /api/vehicles — list vehicles with filters
  app.get('/api/vehicles', { preHandler: [app.authenticate] }, async (request) => {
    const { page = '1', pageSize = '50', company_id, plate_number } = request.query as Record<string, string>;
    const limit = Math.min(parseInt(pageSize, 10) || 50, 200);
    const offset = (Math.max(parseInt(page, 10), 1) - 1) * limit;

    const conditions: string[] = [];
    const params: any[] = [];

    if (company_id) {
      conditions.push('v.company_id = ?');
      params.push(parseInt(company_id, 10));
    }
    if (plate_number) {
      conditions.push('v.plate_number LIKE ?');
      params.push(`%${plate_number}%`);
    }

    const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

    const [rows] = await pool.query(
      `SELECT v.*, c.short_name as company_name
       FROM vehicles v
       LEFT JOIN companies c ON c.id = v.company_id
       ${where}
       ORDER BY v.updated_at DESC LIMIT ? OFFSET ?`,
      [...params, limit, offset]
    );
    const [countRows] = await pool.query(
      `SELECT COUNT(*) as total FROM vehicles v ${where}`,
      params
    );

    return { data: rows, total: (countRows as any[])[0]?.total || 0 };
  });

  // GET /api/vehicles/:id — get single vehicle
  app.get('/api/vehicles/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const [rows] = await pool.query(
      `SELECT v.*, c.short_name as company_name
       FROM vehicles v
       LEFT JOIN companies c ON c.id = v.company_id
       WHERE v.id = ?`,
      [parseInt(id, 10)]
    );
    const vehicle = (rows as any[])[0];
    if (!vehicle) return reply.status(404).send({ error: 'Vehicle not found' });
    return vehicle;
  });

  // GET /api/vehicles/stats — summary stats
  app.get('/api/vehicles/stats', { preHandler: [app.authenticate] }, async () => {
    const [rows] = await pool.query(
      `SELECT COUNT(*) as total,
       SUM(CASE WHEN unprocessed_count > 0 THEN 1 ELSE 0 END) as with_violations,
       SUM(unprocessed_count) as total_unprocessed
       FROM vehicles`
    );
    return (rows as any[])[0] || { total: 0, with_violations: 0, total_unprocessed: 0 };
  });
}

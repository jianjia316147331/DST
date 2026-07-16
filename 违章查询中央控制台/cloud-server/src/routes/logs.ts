import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

export default async function logRoutes(app: FastifyInstance) {
  app.get('/api/logs', { preHandler: [app.authenticate] }, async (request) => {
    const { task_id, level, category, page = '1', pageSize = '50' } = request.query as Record<string, string>;
    const offset = (parseInt(page) - 1) * parseInt(pageSize);
    const limit = parseInt(pageSize);

    const conditions: string[] = [];
    const params: unknown[] = [];

    if (task_id) { conditions.push('task_id = ?'); params.push(task_id); }
    if (level) { conditions.push('level = ?'); params.push(level); }
    if (category) { conditions.push('category = ?'); params.push(category); }

    const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

    const [[rows], [countRows]] = await Promise.all([
      pool.query(
        `SELECT * FROM logs ${where} ORDER BY created_at DESC LIMIT ? OFFSET ?`,
        [...params, limit, offset]
      ),
      pool.query(`SELECT COUNT(*) as count FROM logs ${where}`, params),
    ]);

    return { data: rows, total: countRows[0].count, page: parseInt(page), pageSize: limit };
  });

  app.get('/api/logs/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const [rows] = await pool.query('SELECT * FROM logs WHERE id = ?', [id]);
    if (rows.length === 0) return reply.status(404).send({ error: 'Log not found' });
    return rows[0];
  });
}

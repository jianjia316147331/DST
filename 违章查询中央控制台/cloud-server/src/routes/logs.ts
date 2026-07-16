import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

export default async function logRoutes(app: FastifyInstance) {
  app.get('/api/logs', { preHandler: [app.authenticate] }, async (request) => {
    const { task_id, level, category, page = '1', pageSize = '50' } = request.query as Record<string, string>;
    const offset = (parseInt(page) - 1) * parseInt(pageSize);
    const limit = parseInt(pageSize);

    const conditions: string[] = [];
    const params: unknown[] = [];
    let idx = 1;

    if (task_id) { conditions.push(`task_id = $${idx++}`); params.push(task_id); }
    if (level) { conditions.push(`level = $${idx++}`); params.push(level); }
    if (category) { conditions.push(`category = $${idx++}`); params.push(category); }

    const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

    const [{ rows }, { rows: countRows }] = await Promise.all([
      pool.query(
        `SELECT * FROM logs ${where} ORDER BY created_at DESC LIMIT $${idx++} OFFSET $${idx}`,
        [...params, limit, offset]
      ),
      pool.query(`SELECT COUNT(*) FROM logs ${where}`, params),
    ]);

    return { data: rows, total: parseInt(countRows[0].count, 10), page: parseInt(page), pageSize: limit };
  });

  app.get('/api/logs/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const { rows } = await pool.query('SELECT * FROM logs WHERE id = $1', [id]);
    if (rows.length === 0) return reply.status(404).send({ error: 'Log not found' });
    return rows[0];
  });
}

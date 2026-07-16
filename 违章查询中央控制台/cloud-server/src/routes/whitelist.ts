import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

export default async function whitelistRoutes(app: FastifyInstance) {
  app.get('/api/whitelist', { preHandler: [app.authenticate] }, async () => {
    const { rows } = await pool.query('SELECT * FROM user_whitelist ORDER BY created_at DESC');
    return { data: rows };
  });

  app.post('/api/whitelist', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { phone, name } = request.body as { phone: string; name?: string };

    if (!phone || !/^1[3-9]\d{9}$/.test(phone)) {
      return reply.status(400).send({ error: 'Invalid phone number' });
    }

    const { rows } = await pool.query(
      `INSERT INTO user_whitelist (phone, name) VALUES ($1, $2)
       ON CONFLICT (phone) DO UPDATE SET enabled = TRUE, name = COALESCE($2, user_whitelist.name)
       RETURNING *`,
      [phone, name || null]
    );

    reply.status(201);
    return rows[0];
  });

  app.patch('/api/whitelist/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const { enabled, name } = request.body as { enabled?: boolean; name?: string };

    const sets: string[] = [];
    const params: unknown[] = [];
    let idx = 1;

    if (enabled !== undefined) { sets.push(`enabled = $${idx++}`); params.push(enabled); }
    if (name !== undefined) { sets.push(`name = $${idx++}`); params.push(name); }
    if (sets.length === 0) return reply.status(400).send({ error: 'No valid fields' });

    params.push(id);
    const { rows } = await pool.query(
      `UPDATE user_whitelist SET ${sets.join(', ')} WHERE id = $${idx} RETURNING *`,
      params
    );
    if (rows.length === 0) return reply.status(404).send({ error: 'User not found' });
    return rows[0];
  });

  app.delete('/api/whitelist/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const { rowCount } = await pool.query('DELETE FROM user_whitelist WHERE id = $1', [id]);
    if (rowCount === 0) return reply.status(404).send({ error: 'User not found' });
    return { success: true };
  });
}

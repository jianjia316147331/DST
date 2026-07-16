import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

export default async function whitelistRoutes(app: FastifyInstance) {
  app.get('/api/whitelist', { preHandler: [app.authenticate] }, async () => {
    const [rows] = await pool.query('SELECT * FROM user_whitelist ORDER BY created_at DESC');
    return { data: rows };
  });

  app.post('/api/whitelist', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { phone, name } = request.body as { phone: string; name?: string };

    if (!phone || !/^1[3-9]\d{9}$/.test(phone)) {
      return reply.status(400).send({ error: 'Invalid phone number' });
    }

    await pool.query(
      `INSERT INTO user_whitelist (phone, name) VALUES (?, ?)
       ON DUPLICATE KEY UPDATE enabled = 1, name = COALESCE(?, user_whitelist.name)`,
      [phone, name || null, name || null]
    );

    // Fetch back the inserted/updated row
    const [rows] = await pool.query('SELECT * FROM user_whitelist WHERE phone = ?', [phone]);

    reply.status(201);
    return rows[0];
  });

  app.patch('/api/whitelist/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const { enabled, name } = request.body as { enabled?: boolean; name?: string };

    const sets: string[] = [];
    const params: unknown[] = [];

    if (enabled !== undefined) { sets.push('enabled = ?'); params.push(enabled); }
    if (name !== undefined) { sets.push('name = ?'); params.push(name); }
    if (sets.length === 0) return reply.status(400).send({ error: 'No valid fields' });

    params.push(id);
    const [result] = await pool.query(
      `UPDATE user_whitelist SET ${sets.join(', ')} WHERE id = ?`,
      params
    );
    if (result.affectedRows === 0) return reply.status(404).send({ error: 'User not found' });

    // Fetch updated row
    const [rows] = await pool.query('SELECT * FROM user_whitelist WHERE id = ?', [id]);
    return rows[0];
  });

  app.delete('/api/whitelist/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const [result] = await pool.query('DELETE FROM user_whitelist WHERE id = ?', [id]);
    if (result.affectedRows === 0) return reply.status(404).send({ error: 'User not found' });
    return { success: true };
  });
}

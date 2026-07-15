import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

export default async function nodeRoutes(app: FastifyInstance) {
  // List nodes
  app.get('/api/nodes', { preHandler: [app.authenticate] }, async () => {
    const { rows } = await pool.query('SELECT * FROM nodes ORDER BY created_at DESC');
    return { data: rows };
  });

  // Get node detail
  app.get('/api/nodes/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const { rows } = await pool.query('SELECT * FROM nodes WHERE id = $1', [id]);
    if (rows.length === 0) return reply.status(404).send({ error: 'Node not found' });
    return rows[0];
  });

  // Update node config (max_concurrency)
  app.patch('/api/nodes/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const { max_concurrency, node_name } = request.body as { max_concurrency?: number; node_name?: string };

    const sets: string[] = [];
    const params: unknown[] = [];
    let idx = 1;

    if (max_concurrency !== undefined) { sets.push(`max_concurrency = $${idx++}`); params.push(max_concurrency); }
    if (node_name) { sets.push(`node_name = $${idx++}`); params.push(node_name); }

    if (sets.length === 0) return reply.status(400).send({ error: 'No valid fields' });

    params.push(id);
    const { rows } = await pool.query(
      `UPDATE nodes SET ${sets.join(', ')} WHERE id = $${idx} RETURNING *`,
      params
    );

    if (rows.length === 0) return reply.status(404).send({ error: 'Node not found' });
    return rows[0];
  });
}

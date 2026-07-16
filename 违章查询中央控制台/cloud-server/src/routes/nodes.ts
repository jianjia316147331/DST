import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

export default async function nodeRoutes(app: FastifyInstance) {
  // List nodes
  app.get('/api/nodes', { preHandler: [app.authenticate] }, async () => {
    const [rows] = await pool.query('SELECT * FROM nodes ORDER BY created_at DESC') as any;
    return { data: rows };
  });

  // Create node (pre-register before deployment)
  app.post('/api/nodes', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { node_name, max_concurrency = 15 } = request.body as { node_name: string; max_concurrency?: number };

    if (!node_name || !node_name.trim()) {
      return reply.status(400).send({ error: 'node_name is required' });
    }

    // Check for duplicate name
    const [existing] = await pool.query(
      'SELECT id FROM nodes WHERE node_name = ?', [node_name.trim()]
    ) as any;
    if (existing.length > 0) {
      return reply.status(409).send({ error: 'DEVICE_NAME_EXISTS', message: `设备 "${node_name.trim()}" 已存在` });
    }

    const [result] = await pool.query(
      `INSERT INTO nodes (node_name, status, max_concurrency) VALUES (?, 'offline', ?)`,
      [node_name.trim(), max_concurrency]
    ) as any;

    const [rows] = await pool.query('SELECT * FROM nodes WHERE id = ?', [result.insertId]) as any;

    // Log
    await pool.query(
      `INSERT INTO logs (level, category, message) VALUES ('INFO', 'system', ?)`,
      [`新增设备预注册: ${node_name.trim()}`]
    );

    reply.status(201);
    return rows[0];
  });

  // Get node detail
  app.get('/api/nodes/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const [rows] = await pool.query('SELECT * FROM nodes WHERE id = ?', [id]);
    if (rows.length === 0) return reply.status(404).send({ error: 'Node not found' });
    return rows[0];
  });

  // Update node config (max_concurrency)
  app.patch('/api/nodes/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const { max_concurrency, node_name } = request.body as { max_concurrency?: number; node_name?: string };

    const sets: string[] = [];
    const params: unknown[] = [];

    if (max_concurrency !== undefined) { sets.push('max_concurrency = ?'); params.push(max_concurrency); }
    if (node_name) { sets.push('node_name = ?'); params.push(node_name); }

    if (sets.length === 0) return reply.status(400).send({ error: 'No valid fields' });

    params.push(id);
    const [result] = await pool.query(
      `UPDATE nodes SET ${sets.join(', ')} WHERE id = ?`,
      params
    ) as any;

    if (result.affectedRows === 0) return reply.status(404).send({ error: 'Node not found' });

    // Fetch updated row
    const [rows] = await pool.query('SELECT * FROM nodes WHERE id = ?', [id]) as any;
    return rows[0];
  });

  // Delete node
  app.delete('/api/nodes/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };

    // Unbind companies bound to this node
    await pool.query(
      `UPDATE company_node_bindings SET is_active = 0, unbound_at = NOW()
       WHERE node_id = ? AND is_active = 1`,
      [id]
    );

    const [result] = await pool.query('DELETE FROM nodes WHERE id = ?', [id]) as any;
    if (result.affectedRows === 0) return reply.status(404).send({ error: 'Node not found' });

    await pool.query(
      `INSERT INTO logs (level, category, message) VALUES ('INFO', 'system', ?)`,
      [`删除设备 id=${id}`]
    );

    return { success: true };
  });
}

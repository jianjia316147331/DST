import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

export default async function reportingSchedulesRoutes(app: FastifyInstance) {
  // GET /api/reporting-schedules
  app.get('/api/reporting-schedules', { preHandler: [app.authenticate] }, async () => {
    const [rows] = await pool.query(
      `SELECT rs.*, n.node_name
       FROM reporting_schedules rs
       LEFT JOIN nodes n ON n.id = rs.node_id
       ORDER BY rs.id DESC`
    );
    return { data: rows };
  });

  // POST /api/reporting-schedules
  app.post('/api/reporting-schedules', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { node_id, frequency, times, enabled } = request.body as {
      node_id: number; frequency: string; times?: string[]; enabled?: boolean;
    };
    if (!node_id) return reply.status(400).send({ error: 'node_id required' });

    const [nodes] = await pool.query('SELECT id FROM nodes WHERE id = ?', [node_id]);
    if (!(nodes as any[]).length) return reply.status(404).send({ error: 'Node not found' });

    const [result] = await pool.query(
      'INSERT INTO reporting_schedules (node_id, frequency, times, enabled) VALUES (?, ?, ?, ?)',
      [node_id, frequency || 'daily', times ? JSON.stringify(times) : null, enabled !== false ? 1 : 0]
    );
    const id = (result as any).insertId;
    const [rows] = await pool.query('SELECT * FROM reporting_schedules WHERE id = ?', [id]);
    return (rows as any[])[0];
  });

  // PUT /api/reporting-schedules/:id
  app.put('/api/reporting-schedules/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const { node_id, frequency, times, enabled } = request.body as {
      node_id?: number; frequency?: string; times?: string[]; enabled?: boolean;
    };

    const [existing] = await pool.query('SELECT * FROM reporting_schedules WHERE id = ?', [parseInt(id, 10)]);
    if (!(existing as any[]).length) return reply.status(404).send({ error: 'Not found' });

    const updates: string[] = [];
    const params: any[] = [];

    if (node_id !== undefined) { updates.push('node_id = ?'); params.push(node_id); }
    if (frequency !== undefined) { updates.push('frequency = ?'); params.push(frequency); }
    if (times !== undefined) { updates.push('times = ?'); params.push(JSON.stringify(times)); }
    if (enabled !== undefined) { updates.push('enabled = ?'); params.push(enabled ? 1 : 0); }

    if (updates.length > 0) {
      params.push(parseInt(id, 10));
      await pool.query(`UPDATE reporting_schedules SET ${updates.join(', ')} WHERE id = ?`, params);
    }

    const [rows] = await pool.query('SELECT * FROM reporting_schedules WHERE id = ?', [parseInt(id, 10)]);
    return (rows as any[])[0];
  });

  // DELETE /api/reporting-schedules/:id
  app.delete('/api/reporting-schedules/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const [existing] = await pool.query('SELECT * FROM reporting_schedules WHERE id = ?', [parseInt(id, 10)]);
    if (!(existing as any[]).length) return reply.status(404).send({ error: 'Not found' });
    await pool.query('DELETE FROM reporting_schedules WHERE id = ?', [parseInt(id, 10)]);
    return { ok: true };
  });
}

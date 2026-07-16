import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';
import { CronExpressionParser } from 'cron-parser';

export default async function scheduleRoutes(app: FastifyInstance) {
  app.get('/api/schedules', { preHandler: [app.authenticate] }, async () => {
    const [rows] = await pool.query(
      `SELECT s.*, c.name as company_name, c.province
       FROM schedules s LEFT JOIN companies c ON s.company_id = c.id
       ORDER BY s.created_at DESC`
    );
    return { data: rows };
  });

  app.post('/api/schedules', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { company_id, cron_expression } = request.body as { company_id: number; cron_expression: string };

    // Validate cron
    try {
      const interval = CronExpressionParser.parse(cron_expression);
      const nextRun = interval.next().toDate();

      const [result] = await pool.query(
        `INSERT INTO schedules (company_id, cron_expression, next_run_at)
         VALUES (?, ?, ?)`,
        [company_id, cron_expression, nextRun]
      );

      // Fetch back inserted row
      const [rows] = await pool.query('SELECT * FROM schedules WHERE id = ?', [result.insertId]);

      reply.status(201);
      return rows[0];
    } catch {
      return reply.status(400).send({ error: 'Invalid cron expression' });
    }
  });

  app.put('/api/schedules/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const { cron_expression, enabled } = request.body as { cron_expression?: string; enabled?: boolean };

    const sets: string[] = [];
    const params: unknown[] = [];

    if (cron_expression) {
      try {
        const nextRun = CronExpressionParser.parse(cron_expression).next().toDate();
        sets.push('cron_expression = ?'); params.push(cron_expression);
        sets.push('next_run_at = ?'); params.push(nextRun);
      } catch {
        return reply.status(400).send({ error: 'Invalid cron expression' });
      }
    }
    if (enabled !== undefined) { sets.push('enabled = ?'); params.push(enabled); }

    if (sets.length === 0) return reply.status(400).send({ error: 'No valid fields' });

    params.push(id);
    const [result] = await pool.query(
      `UPDATE schedules SET ${sets.join(', ')} WHERE id = ?`,
      params
    );
    if (result.affectedRows === 0) return reply.status(404).send({ error: 'Schedule not found' });

    // Fetch updated row
    const [rows] = await pool.query(
      `SELECT s.*, c.name as company_name, c.province
       FROM schedules s LEFT JOIN companies c ON s.company_id = c.id
       WHERE s.id = ?`,
      [id]
    );
    return rows[0];
  });

  app.delete('/api/schedules/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const [result] = await pool.query('DELETE FROM schedules WHERE id = ?', [id]);
    if (result.affectedRows === 0) return reply.status(404).send({ error: 'Schedule not found' });
    return { success: true };
  });
}

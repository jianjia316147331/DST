import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

const ACTIVE_STATUSES = ['进行中', '暂停指令下发', '暂停', '继续指令已下发'];

export default async function taskRoutes(app: FastifyInstance) {
  // List tasks
  app.get('/api/tasks', { preHandler: [app.authenticate] }, async (request) => {
    const { company_id, status, page = '1', pageSize = '20' } = request.query as Record<string, string>;
    const offset = (parseInt(page) - 1) * parseInt(pageSize);
    const limit = parseInt(pageSize);

    const conditions: string[] = [];
    const params: unknown[] = [];

    if (company_id) { conditions.push('t.company_id = ?'); params.push(company_id); }
    if (status) { conditions.push('t.status = ?'); params.push(status); }

    const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

    const [[rows], [countRows]] = await Promise.all([
      pool.query(
        `SELECT t.*, c.name as company_name, c.province, n.node_name
         FROM tasks t
         LEFT JOIN companies c ON t.company_id = c.id
         LEFT JOIN nodes n ON t.node_id = n.id
         ${where} ORDER BY t.created_at DESC LIMIT ? OFFSET ?`,
        [...params, limit, offset]
      ),
      pool.query(`SELECT COUNT(*) as count FROM tasks t ${where}`, params),
    ]);

    return { data: rows, total: countRows[0].count, page: parseInt(page), pageSize: limit };
  });

  // Get single task
  app.get('/api/tasks/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const [rows] = await pool.query(
      `SELECT t.*, c.name as company_name, c.province, n.node_name
       FROM tasks t LEFT JOIN companies c ON t.company_id = c.id LEFT JOIN nodes n ON t.node_id = n.id
       WHERE t.id = ?`, [id]
    );
    if (rows.length === 0) return reply.status(404).send({ error: 'Task not found' });
    return rows[0];
  });

  // Create task (manual trigger)
  app.post('/api/tasks', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { company_id, scheduled_at } = request.body as { company_id: number; scheduled_at?: string };

    // Check for active task conflict
    const [active] = await pool.query(
      `SELECT id, status, claude_session_id FROM tasks
       WHERE company_id = ? AND status IN (?)`,
      [company_id, ACTIVE_STATUSES]
    );

    if (active.length > 0) {
      return reply.status(409).send({
        error: 'ACTIVE_TASK_EXISTS',
        message: '该公司已有查询任务进行中',
        existingTask: active[0],
      });
    }

    const [companyRows] = await pool.query('SELECT * FROM companies WHERE id = ?', [company_id]);
    if (companyRows.length === 0) return reply.status(404).send({ error: 'Company not found' });

    const sessionId = `sess-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const [result] = await pool.query(
      `INSERT INTO tasks (company_id, progress, progress_desc, status, scheduled_at, claude_session_id)
       VALUES (?, '入口导航', '任务已创建，等待分配节点...', '进行中', ?, ?)`,
      [company_id, scheduled_at || null, sessionId]
    );

    // Fetch back inserted row
    const [rows] = await pool.query('SELECT * FROM tasks WHERE id = ?', [result.insertId]);

    reply.status(201);
    return rows[0];
  });

  // Terminate existing active task and create new (force start)
  app.post('/api/tasks/force-start', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { company_id } = request.body as { company_id: number };

    // Terminate existing active tasks
    const [active] = await pool.query(
      `SELECT id FROM tasks WHERE company_id = ? AND status IN (?)`,
      [company_id, ACTIVE_STATUSES]
    );

    for (const t of active) {
      await pool.query(`UPDATE tasks SET status = '终止指令下发', updated_at = NOW() WHERE id = ?`, [t.id]);
      await pool.query(
        `INSERT INTO logs (task_id, level, category, message) VALUES (?, 'INFO', 'command', '手动强制终止前序任务，启动新任务')`,
        [t.id]
      );
    }

    const [companyRows] = await pool.query('SELECT * FROM companies WHERE id = ?', [company_id]);
    if (companyRows.length === 0) return reply.status(404).send({ error: 'Company not found' });

    const sessionId = `sess-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const [result] = await pool.query(
      `INSERT INTO tasks (company_id, progress, progress_desc, status, claude_session_id)
       VALUES (?, '入口导航', '任务已创建，等待分配节点...', '进行中', ?)`,
      [company_id, sessionId]
    );

    // Fetch back inserted row
    const [rows] = await pool.query('SELECT * FROM tasks WHERE id = ?', [result.insertId]);

    reply.status(201);
    return rows[0];
  });

  // Update task status/progress (from tray)
  app.patch('/api/tasks/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const updates = request.body as Record<string, unknown>;

    const allowed = ['progress', 'progress_desc', 'status', 'total_vehicles', 'processed_vehicles',
      'violations_found', 'current_page', 'node_id', 'error_message'];
    const sets: string[] = [];
    const params: unknown[] = [];

    for (const key of allowed) {
      if (updates[key] !== undefined) {
        sets.push(`${key} = ?`);
        params.push(updates[key]);
      }
    }

    if (sets.length === 0) return reply.status(400).send({ error: 'No valid fields to update' });

    if (updates['status'] === '完成' || updates['status'] === '终止') {
      sets.push('completed_at = NOW()');
    }

    sets.push('updated_at = NOW()');
    params.push(id);

    const [result] = await pool.query(
      `UPDATE tasks SET ${sets.join(', ')} WHERE id = ?`,
      params
    );

    if (result.affectedRows === 0) return reply.status(404).send({ error: 'Task not found' });

    // Fetch updated row
    const [rows] = await pool.query('SELECT * FROM tasks WHERE id = ?', [id]);
    return rows[0];
  });
}

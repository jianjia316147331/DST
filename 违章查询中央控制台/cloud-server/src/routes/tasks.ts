import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';
import { getNodeWs } from '../ws/handler.js';
import { WebSocket } from 'ws';

const ACTIVE_STATUSES = ['进行中', '暂停指令下发', '暂停', '继续指令已下发'];

// ── Dispatch a task to the bound node via WebSocket ──
async function dispatchTask(taskId: number, companyId: number, sessionId: string) {
  try {
    // 1. Get company info
    const [companyRows] = await pool.query('SELECT * FROM companies WHERE id = ?', [companyId]) as any[];
    const company = companyRows[0];
    if (!company) {
      await pool.query(
        `UPDATE tasks SET status='完成', error_message='公司不存在', completed_at=NOW() WHERE id=?`,
        [taskId]
      );
      return;
    }

    // 2. Check company 12123 login status
    if (company.account_status !== 'online') {
      await pool.query(
        `UPDATE tasks SET status='终止', error_message=?, completed_at=NOW() WHERE id=?`,
        [`公司 ${company.name} 12123 账号未登录，请先在控制台中触发扫码登录`, taskId]
      );
      await pool.query(
        `INSERT INTO logs (task_id, level, category, message)
         VALUES (?, 'WARN', 'system', ?)`,
        [taskId, `任务中断: ${company.name} 12123 账号未登录 (account_status=${company.account_status})`]
      );
      return;
    }

    // 3. Find active node binding
    const [bindings] = await pool.query(
      `SELECT b.*, n.node_name, n.status as node_status
       FROM company_node_bindings b
       JOIN nodes n ON b.node_id = n.id
       WHERE b.company_id = ? AND b.is_active = 1`,
      [companyId]
    ) as any[];

    const binding = bindings[0];
    if (!binding || binding.node_status !== 'online') {
      await pool.query(
        `UPDATE tasks SET status='完成', error_message=?, completed_at=NOW() WHERE id=?`,
        [`设备 ${binding?.node_name || '未绑定'} 离线或未绑定`, taskId]
      );
      return;
    }

    // 4. Build query prompt
    const LQ = '“'; // “
    const RQ = '”'; // ”
    let prompt = `查询${LQ}${company.name}${RQ}公司车辆违章信息`;

    const notifyChat = company.notify_chat_name || '';
    const contactName = company.contact_name || '';
    const contactPhone = company.contact_phone || '';

    if (notifyChat) {
      const atInfo = contactName ? `@${contactName}（${contactPhone}）` : '';
      prompt += `。发送到${LQ}${notifyChat}${RQ}群，并${LQ}${atInfo}${RQ}`;
    } else if (contactName && contactPhone) {
      prompt += `。发送给${LQ}${contactName}（${contactPhone}）${RQ}`;
    }

    // 5. Send session_create via WebSocket to node (handled by both tray-app and node_agent.py)
    const nodeWs = getNodeWs(binding.node_name);
    if (!nodeWs || nodeWs.readyState !== WebSocket.OPEN) {
      await pool.query(
        `UPDATE tasks SET status='完成', error_message=?, completed_at=NOW() WHERE id=?`,
        [`设备 ${binding.node_name} WebSocket 未连接`, taskId]
      );
      return;
    }

    nodeWs.send(JSON.stringify({
      type: 'session_create',
      session_id: sessionId,
      prompt: prompt,
      filter_mode: 'text_only',
      markers: ['TASK_DONE'],
      interactive: true,
      task_id: taskId,
      company_id: companyId,
      company_name: company.name,
    }));

    // 6. Update task: set node_id + progress_desc
    await pool.query(
      `UPDATE tasks SET node_id=?, progress_desc=?, updated_at=NOW() WHERE id=?`,
      [binding.node_id, `任务已分发至设备 ${binding.node_name}`, taskId]
    );

    // Log
    await pool.query(
      `INSERT INTO logs (task_id, level, category, message)
       VALUES (?, 'INFO', 'system', ?)`,
      [taskId, `任务已分发: ${company.name} → ${binding.node_name}, session=${sessionId}`]
    );

    console.log(`[dispatch] Task #${taskId} → node ${binding.node_name} (${company.name})`);
  } catch (err: any) {
    console.error(`[dispatch] Task #${taskId} dispatch error:`, err.message);
    await pool.query(
      `UPDATE tasks SET status='完成', error_message=?, completed_at=NOW() WHERE id=?`,
      [`分发异常: ${err.message}`, taskId]
    );
  }
}

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

    const company = companyRows[0];

    // Check company 12123 login status before creating task
    if (company.account_status !== 'online') {
      return reply.status(400).send({
        error: 'COMPANY_NOT_LOGGED_IN',
        message: `公司 ${company.name} 12123 账号未登录，请先在控制台中触发扫码登录`,
      });
    }

    const sessionId = `sess-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const [result] = await pool.query(
      `INSERT INTO tasks (company_id, progress, progress_desc, status, scheduled_at, claude_session_id)
       VALUES (?, '入口导航', '任务已创建，等待分配节点...', '进行中', ?, ?)`,
      [company_id, scheduled_at || null, sessionId]
    );

    // Fetch back inserted row
    const [rows] = await pool.query('SELECT * FROM tasks WHERE id = ?', [result.insertId]);

    // Dispatch to bound node (async, don't block response)
    dispatchTask(result.insertId, company_id, sessionId);

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

    const companyF = companyRows[0];

    // Check company 12123 login status before force-starting
    if (companyF.account_status !== 'online') {
      return reply.status(400).send({
        error: 'COMPANY_NOT_LOGGED_IN',
        message: `公司 ${companyF.name} 12123 账号未登录，请先在控制台中触发扫码登录`,
      });
    }

    const sessionId = `sess-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const [result] = await pool.query(
      `INSERT INTO tasks (company_id, progress, progress_desc, status, claude_session_id)
       VALUES (?, '入口导航', '任务已创建，等待分配节点...', '进行中', ?)`,
      [company_id, sessionId]
    );

    // Fetch back inserted row
    const [rows] = await pool.query('SELECT * FROM tasks WHERE id = ?', [result.insertId]);

    // Dispatch to bound node
    dispatchTask(result.insertId, company_id, sessionId);

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

  // ── Task control actions (pause / resume / terminate) ──
  async function sendControlAction(taskId: number, action: 'pause_task' | 'resume_task' | 'terminate_task', newStatus: string) {
    // Get task with binding info
    const [taskRows] = await pool.query(
      `SELECT t.*, n.node_name
       FROM tasks t
       LEFT JOIN company_node_bindings b ON t.company_id = b.company_id AND b.is_active = 1
       LEFT JOIN nodes n ON b.node_id = n.id
       WHERE t.id = ?`, [taskId]
    ) as any[];

    const task = taskRows[0];
    if (!task) return { error: '任务不存在' };

    const nodeWs = task.node_name ? getNodeWs(task.node_name) : null;
    if (!nodeWs || nodeWs.readyState !== WebSocket.OPEN) {
      return { error: `设备 ${task.node_name || '未绑定'} WebSocket 未连接` };
    }

    // Send control message to node (both legacy and bridge)
    nodeWs.send(JSON.stringify({
      type: action,
      task_id: taskId,
      session_id: task.claude_session_id || '',
    }));

    // For terminate: also cancel bridge session if one exists
    if (action === 'terminate_task' && task.claude_session_id) {
      nodeWs.send(JSON.stringify({
        type: 'session_cancel',
        session_id: task.claude_session_id,
        task_id: taskId,
      }));
    }

    // Update task status
    if (newStatus === '终止') {
      await pool.query(
        `UPDATE tasks SET status='终止指令下发', updated_at=NOW() WHERE id=?`, [taskId]
      );
    } else if (newStatus === '暂停') {
      await pool.query(
        `UPDATE tasks SET status='暂停指令下发', updated_at=NOW() WHERE id=?`, [taskId]
      );
    } else {
      await pool.query(
        `UPDATE tasks SET status='继续指令下发', updated_at=NOW() WHERE id=?`, [taskId]
      );
    }

    await pool.query(
      `INSERT INTO logs (task_id, level, category, message)
       VALUES (?, 'INFO', 'command', ?)`,
      [taskId, `${action}: 任务 #${taskId}`]
    );

    return { ok: true };
  }

  app.post('/api/tasks/:id/terminate', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const result = await sendControlAction(parseInt(id), 'terminate_task', '终止');
    if (result.error) return reply.status(400).send(result);
    return result;
  });

  app.post('/api/tasks/:id/pause', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const result = await sendControlAction(parseInt(id), 'pause_task', '暂停');
    if (result.error) return reply.status(400).send(result);
    return result;
  });

  app.post('/api/tasks/:id/resume', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const result = await sendControlAction(parseInt(id), 'resume_task', '继续');
    if (result.error) return reply.status(400).send(result);
    return result;
  });
}

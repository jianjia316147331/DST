import pool from './db/index.js';
import { CronExpressionParser } from 'cron-parser';
import { getNodeWs } from './ws/handler.js';
import { WebSocket } from 'ws';

const ACTIVE_STATUSES = ['进行中', '暂停指令下发', '暂停', '继续指令已下发'];

let schedulerInterval: ReturnType<typeof setInterval> | null = null;

export function startScheduler() {
  if (schedulerInterval) return;
  console.log('[scheduler] 启动定时任务检查器 (间隔 30s)');
  schedulerInterval = setInterval(checkSchedules, 30000);
}

export function stopScheduler() {
  if (schedulerInterval) {
    clearInterval(schedulerInterval);
    schedulerInterval = null;
    console.log('[scheduler] 已停止');
  }
}

async function checkSchedules() {
  try {
    const [dueSchedules] = await pool.query(
      `SELECT s.*, c.name as company_name, c.province, c.contact_name, c.contact_phone, c.notify_chat_name
       FROM schedules s
       JOIN companies c ON s.company_id = c.id
       WHERE s.enabled = 1
         AND s.next_run_at IS NOT NULL
         AND s.next_run_at <= NOW()`
    );

    for (const schedule of (dueSchedules as any[])) {
      await processSchedule(schedule);
    }
  } catch (err) {
    console.error('[scheduler] 检查异常:', err);
  }
}

async function processSchedule(schedule: any) {
  const { id, company_id, company_name, cron_expression, contact_name, contact_phone, notify_chat_name } = schedule;

  // 1. Check: does this company already have an active task?
  const [activeTasks] = await pool.query(
    `SELECT id, claude_session_id FROM tasks
     WHERE company_id = ? AND status IN (?)`,
    [company_id, ACTIVE_STATUSES]
  );

  if ((activeTasks as any[]).length > 0) {
    console.log(`[scheduler] ${company_name} 已有活跃任务，跳过本次调度`);
    await updateNextRun(id, cron_expression);
    return;
  }

  // 2. Check: is the company bound to an online node?
  const [bindings] = await pool.query(
    `SELECT b.*, n.node_name, n.status as node_status
     FROM company_node_bindings b
     JOIN nodes n ON b.node_id = n.id
     WHERE b.company_id = ? AND b.is_active = 1 AND n.status = 'online'`,
    [company_id]
  );

  if ((bindings as any[]).length === 0) {
    console.log(`[scheduler] ${company_name} 无已绑定在线设备，跳过`);
    // Still create task with error
    const sessionId = `sess-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    await pool.query(
      `INSERT INTO tasks (company_id, progress, progress_desc, status, scheduled_at, claude_session_id, error_message)
       VALUES (?, '入口导航', '调度任务：设备离线', '完成', NOW(), ?, '调度时设备离线或未绑定')`,
      [company_id, sessionId]
    );
    await pool.query(
      `INSERT INTO logs (level, category, message)
       VALUES ('WARN', 'system', ?)`,
      [`调度失败: ${company_name} 无已绑定在线设备`]
    );
    await updateNextRun(id, cron_expression);
    return;
  }

  // 3. Create task
  const sessionId = `sess-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const [result] = await pool.query(
    `INSERT INTO tasks (company_id, progress, progress_desc, status, scheduled_at, claude_session_id)
     VALUES (?, '入口导航', '调度任务已创建，等待分配...', '进行中', NOW(), ?)`,
    [company_id, sessionId]
  );
  const taskId = (result as any).insertId;

  // 4. Dispatch to node
  const binding = (bindings as any[])[0];
  const nodeWs = getNodeWs(binding.node_name);
  if (nodeWs && nodeWs.readyState === WebSocket.OPEN) {
    // Build prompt
    const LQ = '“'; const RQ = '”';
    let prompt = `查询${LQ}${company_name}${RQ}公司车辆违章信息`;
    if (notify_chat_name) {
      prompt += `。发送到${LQ}${notify_chat_name}${RQ}群`;
      if (contact_name) {
        prompt += `，并@${contact_name}（${contact_phone}）`;
      }
    } else if (contact_name) {
      prompt += `。发送给${LQ}${contact_name}（${contact_phone}）${RQ}`;
    }

    nodeWs.send(JSON.stringify({
      type: 'session_create',
      session_id: sessionId,
      prompt: prompt,
      filter_mode: 'text_only',
      markers: ['TASK_DONE'],
      interactive: true,
      task_id: taskId,
      company_id: company_id,
      company_name: company_name,
    }));

    await pool.query(
      `UPDATE tasks SET node_id=?, progress_desc=?, updated_at=NOW() WHERE id=?`,
      [binding.node_id, `调度任务已分发至设备 ${binding.node_name}`, taskId]
    );

    console.log(`[scheduler] 任务 #${taskId}: ${company_name} → ${binding.node_name}`);
  } else {
    await pool.query(
      `UPDATE tasks SET status='完成', error_message=?, completed_at=NOW() WHERE id=?`,
      [`调度时设备 ${binding.node_name} WebSocket 未连接`, taskId]
    );
  }

  // 5. Update next_run_at
  await updateNextRun(id, cron_expression);

  // 6. Log
  await pool.query(
    `INSERT INTO logs (level, category, message)
     VALUES ('INFO', 'system', ?)`,
    [`调度任务创建并分发: ${company_name} (任务 #${taskId})`]
  );
}

async function updateNextRun(scheduleId: number, cronExpression: string) {
  try {
    const interval = CronExpressionParser.parse(cronExpression);
    const nextRun = interval.next().toDate();
    await pool.query(
      `UPDATE schedules SET next_run_at = ?, last_triggered_at = NOW() WHERE id = ?`,
      [nextRun, scheduleId]
    );
  } catch (err) {
    console.error(`[scheduler] 解析 cron 表达式失败 (schedule #${scheduleId}):`, err);
    await pool.query(
      `UPDATE schedules SET enabled = 0 WHERE id = ?`,
      [scheduleId]
    );
  }
}

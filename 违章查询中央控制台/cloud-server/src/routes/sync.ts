import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';
import { getNodeWs, broadcastToFrontend } from '../ws/handler.js';
import { WebSocket } from 'ws';

export default async function syncRoutes(app: FastifyInstance) {
  // GET /api/sync/logs — list sync logs
  app.get('/api/sync/logs', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { page = '1', pageSize = '50', node_id } = request.query as Record<string, string>;
    const limit = Math.min(parseInt(pageSize, 10) || 50, 200);
    const offset = (Math.max(parseInt(page, 10), 1) - 1) * limit;

    let where = '';
    const params: any[] = [];
    if (node_id) {
      where = 'WHERE node_id = ?';
      params.push(parseInt(node_id, 10));
    }

    const [rows] = await pool.query(
      `SELECT sl.*, n.node_name FROM sync_logs sl
       LEFT JOIN nodes n ON n.id = sl.node_id
       ${where}
       ORDER BY sl.created_at DESC LIMIT ? OFFSET ?`,
      [...params, limit, offset]
    );
    const [countRows] = await pool.query(
      `SELECT COUNT(*) as total FROM sync_logs ${where}`,
      params
    );

    return { data: rows, total: (countRows as any[])[0]?.total || 0 };
  });

  // POST /api/sync/trigger — trigger manual sync on a node
  app.post('/api/sync/trigger', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { node_id } = request.body as { node_id: number };

    const [nodes] = await pool.query('SELECT * FROM nodes WHERE id = ?', [node_id]);
    if (!(nodes as any[]).length) {
      return reply.status(404).send({ error: 'Node not found' });
    }

    // The actual sync is triggered via WebSocket message to the node
    // Here we just log the intent
    await pool.query(
      `INSERT INTO sync_logs (node_id, sync_type, companies, vehicles, violations_ins, violations_upd, status)
       VALUES (?, 'manual', 0, 0, 0, 0, 'pending')`,
      [node_id]
    );

    return { ok: true, message: 'Sync triggered' };
  });

  // POST /api/sync/trigger-login — trigger 12123 login on the node bound to a company
  app.post('/api/sync/trigger-login', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { company_id, company_name, mode } = request.body as { company_id: number; company_name: string; mode?: string };

    if (!company_id && !company_name) {
      return reply.status(400).send({ error: 'company_id or company_name is required' });
    }

    // Find the company
    let company: any;
    if (company_id) {
      const [rows] = await pool.query('SELECT * FROM companies WHERE id = ?', [company_id]);
      company = (rows as any[])[0];
    } else {
      const [rows] = await pool.query('SELECT * FROM companies WHERE name = ?', [company_name]);
      company = (rows as any[])[0];
    }

    if (!company) {
      return reply.status(404).send({ error: 'Company not found' });
    }

    // Find the active node binding
    const [bindings] = await pool.query(
      `SELECT b.*, n.node_name, n.status as node_status
       FROM company_node_bindings b
       JOIN nodes n ON b.node_id = n.id
       WHERE b.company_id = ? AND b.is_active = 1`,
      [company.id]
    );

    const binding = (bindings as any[])[0];
    if (!binding) {
      return reply.status(400).send({ error: '该公司未绑定设备，请先在控制台中绑定设备' });
    }

    if (binding.node_status !== 'online') {
      return reply.status(400).send({ error: `设备 "${binding.node_name}" 当前离线，请确认设备已启动` });
    }

    // Send trigger_login message to the node via WebSocket
    const nodeWs = getNodeWs(binding.node_name);
    if (!nodeWs || nodeWs.readyState !== WebSocket.OPEN) {
      return reply.status(400).send({ error: `设备 "${binding.node_name}" WebSocket 未连接，请重启设备` });
    }

    try {
      // Build the login prompt centrally
      const LQ = '“'; // "
      const RQ = '”'; // "
      let prompt = `启动违章查询${LQ}${company.name}${RQ}登录任务，本次只登录不执行（严禁）查询`;

      const contactName = company.contact_name || '';
      const contactPhone = company.contact_phone || '';
      const notifyChat = company.notify_chat_name || '';

      if (notifyChat) {
        const atInfo = contactName ? `@${contactName}（${contactPhone}）` : '';
        prompt += `。发送到${LQ}${notifyChat}${RQ}群，并${LQ}${atInfo}${RQ}`;
      } else if (contactName && contactPhone) {
        prompt += `。发送给${LQ}${contactName}（${contactPhone}）${RQ}扫码`;
      }

      prompt += `。获取截图后保存到 violation_query/screenshots/qr_${company.name}.png，然后输出 __QR_READY__:qr_${company.name}.png`;

      nodeWs.send(JSON.stringify({
        type: 'trigger_login',
        company_id: company.id,
        company_name: company.name,
        prompt: prompt,
        mode: mode || 'keepalive',
      }));

      // Log the action
      await pool.query(
        `INSERT INTO logs (level, category, message)
         VALUES ('INFO', 'system', ?)`,
        [`触发登录: ${company.name} (设备 ${binding.node_name})`]
      );

      return {
        ok: true,
        message: `已向 ${company.name} 发送登录指令`,
        path: 'keepalive',
        node_name: binding.node_name,
      };
    } catch (err: any) {
      return reply.status(500).send({ error: err.message || '发送登录指令失败' });
    }
  });

  // POST /api/sync/session-message — forward chat message to active Claude session via node
  app.post('/api/sync/session-message', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { company_id, text } = request.body as { company_id: number; text: string };

    if (!company_id || !text) {
      return reply.status(400).send({ error: 'company_id and text are required' });
    }

    const [bindings] = await pool.query(
      `SELECT b.*, n.node_name, n.status as node_status
       FROM company_node_bindings b
       JOIN nodes n ON b.node_id = n.id
       WHERE b.company_id = ? AND b.is_active = 1`,
      [company_id]
    );

    const binding = (bindings as any[])[0];
    if (!binding) {
      return reply.status(400).send({ error: '该公司未绑定设备' });
    }

    const nodeWs = getNodeWs(binding.node_name);
    if (!nodeWs || nodeWs.readyState !== WebSocket.OPEN) {
      return reply.status(400).send({ error: '设备 WebSocket 未连接' });
    }

    nodeWs.send(JSON.stringify({
      type: 'session_message',
      company_id: String(company_id),
      text: text,
    }));

    return { ok: true };
  });

  // GET /api/sync/status/:nodeId — check node sync status
  app.get('/api/sync/status/:nodeId', { preHandler: [app.authenticate] }, async (request) => {
    const { nodeId } = request.params as { nodeId: string };
    const [lastSync] = await pool.query(
      `SELECT * FROM sync_logs WHERE node_id = ? ORDER BY created_at DESC LIMIT 1`,
      [parseInt(nodeId, 10)]
    );
    return { lastSync: (lastSync as any[])[0] || null };
  });
}

import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

export default async function companyRoutes(app: FastifyInstance) {
  // List companies
  app.get('/api/companies', { preHandler: [app.authenticate] }, async (request) => {
    const { province, account_status, search, page = '1', pageSize = '20' } = request.query as Record<string, string>;
    const offset = (parseInt(page) - 1) * parseInt(pageSize);
    const limit = parseInt(pageSize);

    const conditions: string[] = [];
    const params: unknown[] = [];

    if (province) {
      conditions.push('province = ?');
      params.push(province);
    }
    if (account_status) {
      conditions.push('account_status = ?');
      params.push(account_status);
    }
    if (search) {
      conditions.push('(name LIKE ? OR short_name LIKE ?)');
      params.push(`%${search}%`, `%${search}%`);
    }

    const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

    const [[rows], [countRows]] = await Promise.all([
      pool.query(
        `SELECT * FROM companies ${where} ORDER BY created_at DESC LIMIT ? OFFSET ?`,
        [...params, limit, offset]
      ),
      pool.query(`SELECT COUNT(*) as count FROM companies ${where}`, params),
    ]);

    return {
      data: rows,
      total: countRows[0].count,
      page: parseInt(page),
      pageSize: limit,
    };
  });

  // Get single company
  app.get('/api/companies/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const [rows] = await pool.query('SELECT * FROM companies WHERE id = ?', [id]);
    if (rows.length === 0) return reply.status(404).send({ error: 'Company not found' });
    return rows[0];
  });

  // Create company
  app.post('/api/companies', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { name, short_name, province, feishu_contact_id, contact_name, contact_phone, notify_chat_name } =
      request.body as Record<string, string>;

    if (!name || !province || !contact_name || !contact_phone) {
      return reply.status(400).send({ error: 'name, province, contact_name, contact_phone are required' });
    }

    const [result] = await pool.query(
      `INSERT INTO companies (name, short_name, province, feishu_contact_id, contact_name, contact_phone, notify_chat_name)
       VALUES (?, ?, ?, ?, ?, ?, ?)`,
      [name, short_name || null, province, feishu_contact_id || null, contact_name, contact_phone, notify_chat_name || null]
    );

    // Fetch back the inserted row
    const [rows] = await pool.query('SELECT * FROM companies WHERE id = ?', [result.insertId]);

    reply.status(201);
    return rows[0];
  });

  // Update company
  app.put('/api/companies/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const { name, short_name, province, feishu_contact_id, contact_name, contact_phone, notify_chat_name } =
      request.body as Record<string, string>;

    const [result] = await pool.query(
      `UPDATE companies SET name=?, short_name=?, province=?,
       feishu_contact_id=?, contact_name=?, contact_phone=?, notify_chat_name=?, updated_at=NOW()
       WHERE id=?`,
      [name, short_name || null, province, feishu_contact_id || null, contact_name, contact_phone, notify_chat_name || null, id]
    );

    if (result.affectedRows === 0) return reply.status(404).send({ error: 'Company not found' });

    // Fetch updated row
    const [rows] = await pool.query('SELECT * FROM companies WHERE id = ?', [id]);
    return rows[0];
  });

  // Delete company
  app.delete('/api/companies/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };

    try {
      // Delete dependent records first (cascade manually since FKs don't all have ON DELETE CASCADE)
      await pool.query('DELETE FROM violations WHERE company_id = ?', [id]);
      await pool.query('DELETE FROM vehicles WHERE company_id = ?', [id]);
      await pool.query('DELETE FROM tasks WHERE company_id = ?', [id]);
      await pool.query('DELETE FROM schedules WHERE company_id = ?', [id]);
      await pool.query('DELETE FROM company_node_bindings WHERE company_id = ?', [id]);

      const [result] = await pool.query('DELETE FROM companies WHERE id = ?', [id]) as any;
      if (result.affectedRows === 0) return reply.status(404).send({ error: 'Company not found' });

      await pool.query(
        `INSERT INTO logs (level, category, message) VALUES ('INFO', 'system', ?)`,
        [`删除公司 id=${id} 及其关联数据`]
      );

      return { success: true };
    } catch (err: any) {
      return reply.status(500).send({ error: err.message || 'Delete failed' });
    }
  });

  // Bind company to node
  app.put('/api/companies/:id/bind', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const { node_id } = request.body as { node_id: number };

    // Verify company exists
    const [compRows] = await pool.query('SELECT id FROM companies WHERE id = ?', [id]);
    if (compRows.length === 0) return reply.status(404).send({ error: 'Company not found' });

    // Verify node exists
    const [nodeRows] = await pool.query('SELECT id, status FROM nodes WHERE id = ?', [node_id]);
    if (nodeRows.length === 0) return reply.status(404).send({ error: 'Node not found' });

    // When binding switches to a new node, clear account_status immediately.
    // The old node's stale keepalive_status reports must not keep showing "online".
    await pool.query(
      `UPDATE companies SET account_status = 'offline' WHERE id = ?`,
      [id]
    );

    // Upsert binding: deactivate old, then insert-or-update
    await pool.query(
      `UPDATE company_node_bindings SET is_active = 0, unbound_at = NOW()
       WHERE company_id = ? AND is_active = 1`,
      [id]
    );

    await pool.query(
      `INSERT INTO company_node_bindings (company_id, node_id, is_active, bound_at)
       VALUES (?, ?, 1, NOW())
       ON DUPLICATE KEY UPDATE node_id = VALUES(node_id), is_active = 1, bound_at = NOW(), unbound_at = NULL`,
      [id, node_id]
    );

    // Fetch back
    const [rows] = await pool.query(
      `SELECT b.*, n.node_name, n.status as node_status
       FROM company_node_bindings b
       JOIN nodes n ON b.node_id = n.id
       WHERE b.company_id = ? AND b.is_active = 1`,
      [id]
    );

    await pool.query(
      `INSERT INTO logs (level, category, message)
       VALUES ('INFO', 'system', ?)`,
      [`公司 id=${id} 已绑定设备 ${nodeRows[0].node_name || nodeRows[0].id}`]
    );

    reply.status(201);
    return rows[0] || { company_id: id, node_id, is_active: true };
  });

  // Unbind company from node
  app.delete('/api/companies/:id/bind', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    await pool.query(
      `UPDATE company_node_bindings SET is_active = 0, unbound_at = NOW()
       WHERE company_id = ? AND is_active = 1`,
      [id]
    );
    return { success: true };
  });

  // Get current binding for a company
  app.get('/api/companies/:id/bind', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const [rows] = await pool.query(
      `SELECT b.*, n.node_name, n.status as node_status
       FROM company_node_bindings b
       JOIN nodes n ON b.node_id = n.id
       WHERE b.company_id = ? AND b.is_active = 1`,
      [id]
    );
    return rows[0] || null;
  });
}

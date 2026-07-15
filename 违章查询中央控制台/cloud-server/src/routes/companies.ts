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
    let paramIdx = 1;

    if (province) {
      conditions.push(`province = $${paramIdx++}`);
      params.push(province);
    }
    if (account_status) {
      conditions.push(`account_status = $${paramIdx++}`);
      params.push(account_status);
    }
    if (search) {
      conditions.push(`(name ILIKE $${paramIdx} OR short_name ILIKE $${paramIdx})`);
      params.push(`%${search}%`);
      paramIdx++;
    }

    const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

    const [{ rows }, { rows: countRows }] = await Promise.all([
      pool.query(
        `SELECT * FROM companies ${where} ORDER BY created_at DESC LIMIT $${paramIdx++} OFFSET $${paramIdx}`,
        [...params, limit, offset]
      ),
      pool.query(`SELECT COUNT(*) FROM companies ${where}`, params),
    ]);

    return {
      data: rows,
      total: parseInt(countRows[0].count, 10),
      page: parseInt(page),
      pageSize: limit,
    };
  });

  // Get single company
  app.get('/api/companies/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const { rows } = await pool.query('SELECT * FROM companies WHERE id = $1', [id]);
    if (rows.length === 0) return reply.status(404).send({ error: 'Company not found' });
    return rows[0];
  });

  // Create company
  app.post('/api/companies', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { name, short_name, province, province_url, feishu_contact_id, contact_name, contact_phone } =
      request.body as Record<string, string>;

    if (!name || !province || !province_url) {
      return reply.status(400).send({ error: 'name, province, province_url are required' });
    }

    const { rows } = await pool.query(
      `INSERT INTO companies (name, short_name, province, province_url, feishu_contact_id, contact_name, contact_phone)
       VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *`,
      [name, short_name || null, province, province_url, feishu_contact_id || null, contact_name || null, contact_phone || null]
    );

    reply.status(201);
    return rows[0];
  });

  // Update company
  app.put('/api/companies/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const { name, short_name, province, province_url, feishu_contact_id, contact_name, contact_phone } =
      request.body as Record<string, string>;

    const { rows } = await pool.query(
      `UPDATE companies SET name=$1, short_name=$2, province=$3, province_url=$4,
       feishu_contact_id=$5, contact_name=$6, contact_phone=$7, updated_at=NOW()
       WHERE id=$8 RETURNING *`,
      [name, short_name, province, province_url, feishu_contact_id, contact_name, contact_phone, id]
    );

    if (rows.length === 0) return reply.status(404).send({ error: 'Company not found' });
    return rows[0];
  });

  // Delete company
  app.delete('/api/companies/:id', { preHandler: [app.authenticate] }, async (request, reply) => {
    const { id } = request.params as { id: string };
    const { rowCount } = await pool.query('DELETE FROM companies WHERE id = $1', [id]);
    if (rowCount === 0) return reply.status(404).send({ error: 'Company not found' });
    return { success: true };
  });
}

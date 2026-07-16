import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

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

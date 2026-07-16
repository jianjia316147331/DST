import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

export default async function authRoutes(app: FastifyInstance) {
  // Login: verify phone is in whitelist, issue JWT
  app.post('/api/auth/login', async (request, reply) => {
    const { phone } = request.body as { phone: string };

    if (!phone || !/^1[3-9]\d{9}$/.test(phone)) {
      return reply.status(400).send({ error: 'Invalid phone number' });
    }

    const { rows } = await pool.query(
      'SELECT id, phone, name, enabled FROM user_whitelist WHERE phone = $1',
      [phone]
    );

    if (rows.length === 0) {
      return reply.status(403).send({ error: 'Phone not in whitelist' });
    }

    if (!rows[0].enabled) {
      return reply.status(403).send({ error: 'Account disabled' });
    }

    const token = app.jwt.sign({
      phone: rows[0].phone,
      name: rows[0].name,
    }, { expiresIn: '24h' });

    return {
      token,
      user: {
        phone: rows[0].phone,
        name: rows[0].name,
      },
    };
  });

  // Get current user info
  app.get('/api/auth/me', { preHandler: [app.authenticate] }, async (request) => {
    const user = request.user as { phone: string; name: string | null };
    return { phone: user.phone, name: user.name };
  });
}

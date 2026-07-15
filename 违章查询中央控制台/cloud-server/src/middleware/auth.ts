import { FastifyRequest, FastifyReply } from 'fastify';
import pool from '../db/index.js';

export interface JwtPayload {
  phone: string;
  name: string | null;
  iat: number;
  exp: number;
}

export async function authGuard(request: FastifyRequest, reply: FastifyReply) {
  try {
    await request.jwtVerify<JwtPayload>();
  } catch {
    return reply.status(401).send({ error: 'Unauthorized' });
  }
}

export async function whitelistGuard(request: FastifyRequest, reply: FastifyReply) {
  const payload = request.user as JwtPayload;
  const { rows } = await pool.query(
    'SELECT enabled FROM user_whitelist WHERE phone = $1',
    [payload.phone]
  );
  if (rows.length === 0 || !rows[0].enabled) {
    return reply.status(403).send({ error: 'Not in whitelist' });
  }
}

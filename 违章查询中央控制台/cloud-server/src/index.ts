import Fastify from 'fastify';
import cors from '@fastify/cors';
import jwt from '@fastify/jwt';
import websocket from '@fastify/websocket';

import authRoutes from './routes/auth.js';
import companyRoutes from './routes/companies.js';
import taskRoutes from './routes/tasks.js';
import nodeRoutes from './routes/nodes.js';
import logRoutes from './routes/logs.js';
import scheduleRoutes from './routes/schedules.js';
import whitelistRoutes from './routes/whitelist.js';
import dashboardRoutes from './routes/dashboard.js';
import downloadRoutes from './routes/download.js';
import wsHandler from './ws/handler.js';
import { authGuard } from './middleware/auth.js';

const app = Fastify({ logger: true });

// Plugins
await app.register(cors, { origin: true, credentials: true });
await app.register(jwt, { secret: process.env.JWT_SECRET || 'violation-console-secret-dev' });
await app.register(websocket);

// Decorate with auth guard
app.decorate('authenticate', authGuard);

// REST routes
await app.register(authRoutes);
await app.register(companyRoutes);
await app.register(taskRoutes);
await app.register(nodeRoutes);
await app.register(logRoutes);
await app.register(scheduleRoutes);
await app.register(whitelistRoutes);
await app.register(dashboardRoutes);
await app.register(downloadRoutes);

// WebSocket
await app.register(wsHandler);

// Health check
app.get('/api/health', async () => ({ status: 'ok', timestamp: new Date().toISOString() }));

// Start
const port = parseInt(process.env.PORT || '3001', 10);
const host = process.env.HOST || '0.0.0.0';

try {
  await app.listen({ port, host });
  console.log(`Cloud server running at http://${host}:${port}`);
} catch (err) {
  app.log.error(err);
  process.exit(1);
}

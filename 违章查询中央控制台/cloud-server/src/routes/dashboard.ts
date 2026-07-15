import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

export default async function dashboardRoutes(app: FastifyInstance) {
  app.get('/api/dashboard/overview', { preHandler: [app.authenticate] }, async () => {
    const [
      { rows: companyCount },
      { rows: activeJobs },
      { rows: violationsToday },
      { rows: completedJobs },
      { rows: onlineNodes },
    ] = await Promise.all([
      pool.query('SELECT COUNT(*) FROM companies'),
      pool.query("SELECT COUNT(*) FROM tasks WHERE status IN ('进行中','暂停指令下发','暂停','继续指令已下发')"),
      pool.query('SELECT COUNT(*) FROM violations WHERE query_date = CURRENT_DATE'),
      pool.query("SELECT COUNT(*) FROM tasks WHERE status = '完成'"),
      pool.query("SELECT COUNT(*) FROM nodes WHERE status = 'online'"),
    ]);

    return {
      total_companies: parseInt(companyCount[0].count, 10),
      active_jobs: parseInt(activeJobs[0].count, 10),
      violations_today: parseInt(violationsToday[0].count, 10),
      completed_jobs: parseInt(completedJobs[0].count, 10),
      online_nodes: parseInt(onlineNodes[0].count, 10),
    };
  });
}

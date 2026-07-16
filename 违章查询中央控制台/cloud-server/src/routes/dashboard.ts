import { FastifyInstance } from 'fastify';
import pool from '../db/index.js';

export default async function dashboardRoutes(app: FastifyInstance) {
  app.get('/api/dashboard/overview', { preHandler: [app.authenticate] }, async () => {
    const [
      [companyCount],
      [activeJobs],
      [violationsToday],
      [completedJobs],
      [onlineNodes],
      [vehicleCount],
      [totalViolations],
      [unprocessedViolations],
    ] = await Promise.all([
      pool.query('SELECT COUNT(*) as count FROM companies'),
      pool.query("SELECT COUNT(*) as count FROM tasks WHERE status IN ('进行中','暂停指令下发','暂停','继续指令已下发')"),
      pool.query('SELECT COUNT(*) as count FROM violations WHERE query_date = CURRENT_DATE'),
      pool.query("SELECT COUNT(*) as count FROM tasks WHERE status = '完成'"),
      pool.query("SELECT COUNT(*) as count FROM nodes WHERE status = 'online'"),
      pool.query('SELECT COUNT(*) as count FROM vehicles'),
      pool.query('SELECT COUNT(*) as count FROM violations'),
      pool.query("SELECT COUNT(*) as count FROM violations WHERE handling_status = '0'"),
    ]);

    return {
      total_companies: companyCount[0].count,
      active_jobs: activeJobs[0].count,
      violations_today: violationsToday[0].count,
      completed_jobs: completedJobs[0].count,
      online_nodes: onlineNodes[0].count,
      total_vehicles: vehicleCount[0].count,
      total_violations: totalViolations[0].count,
      unprocessed_violations: unprocessedViolations[0].count,
    };
  });
}

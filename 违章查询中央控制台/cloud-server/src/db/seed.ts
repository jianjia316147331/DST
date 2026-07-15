import pool from './index.js';

async function seed() {
  const client = await pool.connect();
  try {
    // Seed admin user
    await client.query(`
      INSERT INTO user_whitelist (phone, name) VALUES ('13800138000', '管理员')
      ON CONFLICT (phone) DO NOTHING
    `);

    // Seed sample companies
    await client.query(`
      INSERT INTO companies (name, short_name, province, province_url, contact_name, contact_phone, account_status)
      VALUES
        ('成都驰驱新能源汽车科技有限公司', '成都驰驱', '四川', 'sc.122.gov.cn', '张管理', '13800138001', 'offline'),
        ('厦门市地上铁新创绿能汽车服务有限公司', '厦门地上铁', '福建', 'fj.122.gov.cn', '李管理', '13800138002', 'offline'),
        ('成都大搜车公司', '成都大搜车', '四川', 'sc.122.gov.cn', '王管理', '13800138003', 'offline')
      ON CONFLICT DO NOTHING
    `);

    console.log('Seed completed successfully.');
  } catch (err) {
    console.error('Seed failed:', err);
    process.exit(1);
  } finally {
    client.release();
    await pool.end();
  }
}

seed();

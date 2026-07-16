import mysql from 'mysql2/promise';

const pool = mysql.createPool({
  host: process.env.DB_HOST || 'localhost',
  port: parseInt(process.env.DB_PORT || '3306', 10),
  database: process.env.DB_NAME || 'violation_console',
  user: process.env.DB_USER || 'violation',
  password: process.env.DB_PASSWORD || 'violation123',
  waitForConnections: true,
  connectionLimit: 20,
  idleTimeout: 30000,
});

export default pool;

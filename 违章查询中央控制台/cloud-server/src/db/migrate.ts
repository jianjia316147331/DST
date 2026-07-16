import pool from './index.js';

const schema = `
CREATE TABLE IF NOT EXISTS user_whitelist (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    phone       VARCHAR(20) NOT NULL UNIQUE,
    name        VARCHAR(100),
    enabled     TINYINT(1) DEFAULT 1,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS nodes (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    node_name       VARCHAR(100) NOT NULL,
    hostname        VARCHAR(200),
    ip_address      VARCHAR(45),
    status          VARCHAR(20) DEFAULT 'offline',
    max_concurrency INT DEFAULT 15,
    active_sessions INT DEFAULT 0,
    memory_total_gb DECIMAL(5,2),
    cpu_cores       INT,
    last_heartbeat  TIMESTAMP NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS companies (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    name              VARCHAR(200) NOT NULL,
    short_name        VARCHAR(50),
    province          VARCHAR(20) NOT NULL,
    province_url      VARCHAR(100) NOT NULL,
    feishu_contact_id VARCHAR(100),
    contact_name      VARCHAR(100),
    contact_phone     VARCHAR(20),
    account_status    VARCHAR(20) DEFAULT 'offline',
    last_query_at     TIMESTAMP NULL,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tasks (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    company_id        INT NOT NULL,
    node_id           INT,
    progress          VARCHAR(20) DEFAULT '入口导航',
    progress_desc     TEXT,
    status            VARCHAR(20) DEFAULT '进行中',
    total_vehicles    INT DEFAULT 0,
    processed_vehicles INT DEFAULT 0,
    violations_found  INT DEFAULT 0,
    current_page      INT DEFAULT 1,
    scheduled_at      TIMESTAMP NULL,
    started_at        TIMESTAMP NULL,
    completed_at      TIMESTAMP NULL,
    claude_session_id VARCHAR(100),
    error_message     TEXT,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (company_id) REFERENCES companies(id),
    FOREIGN KEY (node_id) REFERENCES nodes(id),
    INDEX idx_tasks_status (status),
    INDEX idx_tasks_company (company_id),
    INDEX idx_tasks_node (node_id)
);

CREATE TABLE IF NOT EXISTS logs (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    task_id     INT,
    node_id     INT,
    level       VARCHAR(10) DEFAULT 'INFO',
    category    VARCHAR(50),
    message     TEXT NOT NULL,
    detail      JSON,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (node_id) REFERENCES nodes(id),
    INDEX idx_logs_task (task_id),
    INDEX idx_logs_created (created_at)
);

CREATE TABLE IF NOT EXISTS schedules (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    company_id        INT NOT NULL,
    cron_expression   VARCHAR(50) NOT NULL,
    enabled           TINYINT(1) DEFAULT 1,
    last_triggered_at TIMESTAMP NULL,
    next_run_at       TIMESTAMP NULL,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS violations (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    task_id             INT,
    company_id          INT NOT NULL,
    plate_number        VARCHAR(10) NOT NULL,
    violation_time      TIMESTAMP NULL,
    violation_location  TEXT,
    violation_behavior  TEXT,
    fine_amount         DECIMAL(10,2),
    points              INT,
    handling_status     VARCHAR(50),
    payment_status      VARCHAR(50),
    province            VARCHAR(20),
    query_date          DATE NOT NULL,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
`;

async function migrate() {
  // Split by semicolons and execute each statement
  const statements = schema
    .split(';')
    .map(s => s.trim())
    .filter(s => s.length > 0);

  try {
    console.log('Running migration...');
    for (const stmt of statements) {
      await pool.query(stmt);
    }
    // Add extra tables that may be missing
    await pool.query([
      'CREATE TABLE IF NOT EXISTS vehicles (',
      'id INT AUTO_INCREMENT PRIMARY KEY,',
      'company_id INT NOT NULL,',
      'node_id INT,',
      'plate_number VARCHAR(10) NOT NULL,',
      'plate_type VARCHAR(10),',
      'plate_type_label VARCHAR(50),',
      'status_code VARCHAR(20),',
      'status_label VARCHAR(50),',
      'inspection_date VARCHAR(20),',
      'unprocessed_count INT DEFAULT 0,',
      'tag VARCHAR(50),',
      'tag_batch_id VARCHAR(100),',
      'query_date DATE NOT NULL,',
      'created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,',
      'updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,',
      'FOREIGN KEY (company_id) REFERENCES companies(id),',
      'INDEX idx_vehicles_company (company_id),',
      'INDEX idx_vehicles_plate (plate_number)',
      ')',
    ].join(' '));
    await pool.query([
      'CREATE TABLE IF NOT EXISTS sync_logs (',
      'id INT AUTO_INCREMENT PRIMARY KEY,',
      'node_id INT NOT NULL,',
      'sync_type VARCHAR(20) DEFAULT \'periodic\',',
      'task_id INT,',
      'companies INT DEFAULT 0,',
      'vehicles INT DEFAULT 0,',
      'violations_ins INT DEFAULT 0,',
      'violations_upd INT DEFAULT 0,',
      'status VARCHAR(20) DEFAULT \'ok\',',
      'error_message TEXT,',
      'created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,',
      'FOREIGN KEY (node_id) REFERENCES nodes(id),',
      'INDEX idx_sync_logs_node (node_id),',
      'INDEX idx_sync_logs_created (created_at)',
      ')',
    ].join(' '));
    try {
      await pool.query('ALTER TABLE nodes ADD COLUMN last_sync_at TIMESTAMP NULL');
    } catch (e: any) {
      // Ignore if column already exists
      if (!e.message?.includes('Duplicate column')) throw e;
    }
    console.log('Migration completed successfully.');
  } catch (err) {
    console.error('Migration failed:', err);
    process.exit(1);
  } finally {
    await pool.end();
  }
}

migrate();

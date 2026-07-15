import pool from './index.js';

const schema = `
CREATE TABLE IF NOT EXISTS user_whitelist (
    id          SERIAL PRIMARY KEY,
    phone       VARCHAR(20) UNIQUE NOT NULL,
    name        VARCHAR(100),
    enabled     BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS nodes (
    id              SERIAL PRIMARY KEY,
    node_name       VARCHAR(100) NOT NULL,
    hostname        VARCHAR(200),
    ip_address      INET,
    status          VARCHAR(20) DEFAULT 'offline',
    max_concurrency INTEGER DEFAULT 15,
    active_sessions INTEGER DEFAULT 0,
    memory_total_gb NUMERIC(5,2),
    cpu_cores       INTEGER,
    last_heartbeat  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS companies (
    id                SERIAL PRIMARY KEY,
    name              VARCHAR(200) NOT NULL,
    short_name        VARCHAR(50),
    province          VARCHAR(20) NOT NULL,
    province_url      VARCHAR(100) NOT NULL,
    feishu_contact_id VARCHAR(100),
    contact_name      VARCHAR(100),
    contact_phone     VARCHAR(20),
    account_status    VARCHAR(20) DEFAULT 'offline',
    last_query_at     TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tasks (
    id                SERIAL PRIMARY KEY,
    company_id        INTEGER NOT NULL REFERENCES companies(id),
    node_id           INTEGER REFERENCES nodes(id),
    progress          VARCHAR(20) DEFAULT '入口导航',
    progress_desc     TEXT,
    status            VARCHAR(20) DEFAULT '进行中',
    total_vehicles    INTEGER DEFAULT 0,
    processed_vehicles INTEGER DEFAULT 0,
    violations_found  INTEGER DEFAULT 0,
    current_page      INTEGER DEFAULT 1,
    scheduled_at      TIMESTAMPTZ,
    started_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    claude_session_id VARCHAR(100),
    error_message     TEXT,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_company ON tasks(company_id);
CREATE INDEX IF NOT EXISTS idx_tasks_node ON tasks(node_id);

CREATE TABLE IF NOT EXISTS logs (
    id          SERIAL PRIMARY KEY,
    task_id     INTEGER REFERENCES tasks(id),
    node_id     INTEGER REFERENCES nodes(id),
    level       VARCHAR(10) DEFAULT 'INFO',
    category    VARCHAR(50),
    message     TEXT NOT NULL,
    detail      JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_logs_task ON logs(task_id);
CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);

CREATE TABLE IF NOT EXISTS schedules (
    id                SERIAL PRIMARY KEY,
    company_id        INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    cron_expression   VARCHAR(50) NOT NULL,
    enabled           BOOLEAN DEFAULT TRUE,
    last_triggered_at TIMESTAMPTZ,
    next_run_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS violations (
    id                  SERIAL PRIMARY KEY,
    task_id             INTEGER REFERENCES tasks(id),
    company_id          INTEGER NOT NULL REFERENCES companies(id),
    plate_number        VARCHAR(10) NOT NULL,
    violation_time      TIMESTAMPTZ,
    violation_location  TEXT,
    violation_behavior  TEXT,
    fine_amount         NUMERIC(10,2),
    points              INTEGER,
    handling_status     VARCHAR(50),
    payment_status      VARCHAR(50),
    province            VARCHAR(20),
    query_date          DATE NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
`;

async function migrate() {
  const client = await pool.connect();
  try {
    console.log('Running migration...');
    await client.query(schema);
    console.log('Migration completed successfully.');
  } catch (err) {
    console.error('Migration failed:', err);
    process.exit(1);
  } finally {
    client.release();
    await pool.end();
  }
}

migrate();

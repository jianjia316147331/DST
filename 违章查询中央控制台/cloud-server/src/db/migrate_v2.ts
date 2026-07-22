import pool from './index.js';

// Each statement is independent so one failure doesn't block others (idempotent migration)
const schema = [
  // ── vehicles ──
  `CREATE TABLE IF NOT EXISTS vehicles (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    company_id        INT NOT NULL,
    node_id           INT,
    plate_number      VARCHAR(20) NOT NULL,
    plate_type        VARCHAR(20) DEFAULT '',
    plate_type_label  VARCHAR(50) DEFAULT '',
    status_code       VARCHAR(50) DEFAULT '',
    status_label      VARCHAR(100) DEFAULT '',
    inspection_date   VARCHAR(20) DEFAULT '',
    unprocessed_count INT DEFAULT 0,
    tag               VARCHAR(50) DEFAULT '',
    tag_batch_id      VARCHAR(50) DEFAULT '',
    query_date        DATE NOT NULL,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_vehicle (company_id, plate_number),
    INDEX idx_vehicles_plate (plate_number),
    INDEX idx_vehicles_company (company_id)
  )`,

  // ── violations: add columns one at a time ──
  `ALTER TABLE violations ADD COLUMN natural_key_hash CHAR(32) DEFAULT '' AFTER id`,
  `ALTER TABLE violations ADD COLUMN node_id INT AFTER company_id`,
  `ALTER TABLE violations ADD COLUMN vehicle_id INT AFTER node_id`,
  `ALTER TABLE violations ADD COLUMN plate_type VARCHAR(20) DEFAULT '' AFTER plate_number`,
  `ALTER TABLE violations ADD COLUMN plate_type_label VARCHAR(50) DEFAULT '' AFTER plate_type`,
  `ALTER TABLE violations ADD COLUMN violation_code VARCHAR(50) DEFAULT '' AFTER violation_behavior`,
  `ALTER TABLE violations ADD COLUMN handling_status_label VARCHAR(100) DEFAULT '' AFTER handling_status`,
  `ALTER TABLE violations ADD COLUMN payment_status_label VARCHAR(100) DEFAULT '' AFTER payment_status`,
  `ALTER TABLE violations ADD COLUMN authority VARCHAR(200) DEFAULT '' AFTER payment_status_label`,
  `ALTER TABLE violations ADD COLUMN city VARCHAR(100) DEFAULT '' AFTER province`,
  `ALTER TABLE violations ADD COLUMN unique_id VARCHAR(200) DEFAULT '' AFTER city`,
  `ALTER TABLE violations ADD COLUMN processing_time VARCHAR(50) DEFAULT '' AFTER unique_id`,
  `ALTER TABLE violations ADD COLUMN data_update_time VARCHAR(50) DEFAULT '' AFTER processing_time`,
  `ALTER TABLE violations ADD COLUMN first_collection_time VARCHAR(50) DEFAULT '' AFTER data_update_time`,

  // ── violations: indexes (each separate) ──
  `ALTER TABLE violations ADD INDEX idx_violations_nkey (natural_key_hash)`,
  `ALTER TABLE violations ADD INDEX idx_violations_node (node_id)`,
  `ALTER TABLE violations ADD INDEX idx_violations_vehicle (vehicle_id)`,

  // ── violations: foreign keys ──
  `ALTER TABLE violations ADD CONSTRAINT fk_violations_vehicle FOREIGN KEY (vehicle_id) REFERENCES vehicles(id)`,
  `ALTER TABLE violations ADD CONSTRAINT fk_violations_node_fk FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE SET NULL`,

  // ── profiles ──
  `CREATE TABLE IF NOT EXISTS profiles (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    node_id         INT,
    company_name    VARCHAR(200) NOT NULL,
    profile_name    VARCHAR(200) NOT NULL DEFAULT '',
    profile_id      VARCHAR(100) DEFAULT '',
    platform_url    VARCHAR(500) NOT NULL DEFAULT '',
    instance_port   INT,
    last_login      TIMESTAMP NULL,
    is_logged_in    TINYINT(1) DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_profiles_company (company_name)
  )`,

  // ── company_node_bindings ──
  `CREATE TABLE IF NOT EXISTS company_node_bindings (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    company_id  INT NOT NULL UNIQUE,
    node_id     INT NOT NULL,
    is_active   TINYINT(1) DEFAULT 1,
    bound_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    unbound_at  TIMESTAMP NULL,
    INDEX idx_binding_node (node_id)
  )`,

  // ── sync_logs ──
  `CREATE TABLE IF NOT EXISTS sync_logs (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    node_id         INT,
    sync_type       VARCHAR(20) NOT NULL,
    task_id         INT,
    companies       INT DEFAULT 0,
    vehicles        INT DEFAULT 0,
    violations_ins  INT DEFAULT 0,
    violations_upd  INT DEFAULT 0,
    status          VARCHAR(20) DEFAULT 'success',
    error_message   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
  )`,

  // ── nodes: add last_sync_at ──
  `ALTER TABLE nodes ADD COLUMN last_sync_at TIMESTAMP NULL`,

  // ── Phase 1: companies — drop province_url, make contact required ──
  `ALTER TABLE companies DROP COLUMN province_url`,
  `UPDATE companies SET contact_name = '' WHERE contact_name IS NULL`,
  `UPDATE companies SET contact_phone = '' WHERE contact_phone IS NULL`,
  `ALTER TABLE companies MODIFY COLUMN contact_name VARCHAR(100) NOT NULL`,
  `ALTER TABLE companies MODIFY COLUMN contact_phone VARCHAR(20) NOT NULL`,

  // ── Phase 1: nodes — runtime metrics columns ──
  `ALTER TABLE nodes ADD COLUMN cpu_percent DECIMAL(6,2) DEFAULT 0`,
  `ALTER TABLE nodes ADD COLUMN cpu_count INT DEFAULT 0`,
  `ALTER TABLE nodes ADD COLUMN memory_used_gb DECIMAL(8,2) DEFAULT 0`,
  `ALTER TABLE nodes ADD COLUMN memory_percent DECIMAL(6,2) DEFAULT 0`,
  `ALTER TABLE nodes ADD COLUMN disk_total_gb DECIMAL(8,2) DEFAULT 0`,
  `ALTER TABLE nodes ADD COLUMN disk_used_gb DECIMAL(8,2) DEFAULT 0`,
  `ALTER TABLE nodes ADD COLUMN disk_percent DECIMAL(6,2) DEFAULT 0`,
  `ALTER TABLE nodes ADD COLUMN net_bytes_sent_mb DECIMAL(12,2) DEFAULT 0`,
  `ALTER TABLE nodes ADD COLUMN net_bytes_recv_mb DECIMAL(12,2) DEFAULT 0`,
  `ALTER TABLE nodes ADD COLUMN uptime_seconds INT DEFAULT 0`,
  `ALTER TABLE nodes ADD COLUMN process_count INT DEFAULT 0`,
  `ALTER TABLE nodes ADD COLUMN processes JSON`,

  // ── Phase 1: violations — sync timestamps ──
  `ALTER TABLE violations ADD COLUMN data_sync_time TIMESTAMP NULL`,
  `ALTER TABLE violations ADD COLUMN last_query_time TIMESTAMP NULL`,

  // ── Phase 1: vehicles — last_query_time ──
  `ALTER TABLE vehicles ADD COLUMN last_query_time TIMESTAMP NULL`,

  // ── Phase 1: reporting_schedules ──
  `CREATE TABLE IF NOT EXISTS reporting_schedules (
    id        INT AUTO_INCREMENT PRIMARY KEY,
    node_id   INT NOT NULL,
    enabled   TINYINT(1) DEFAULT 1,
    frequency VARCHAR(20) DEFAULT 'custom',
    times     JSON NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_rs_node (node_id)
  )`,
  `ALTER TABLE reporting_schedules ADD COLUMN frequency VARCHAR(20) DEFAULT 'custom' AFTER enabled`,

  // ── companies: notify_chat_name ──
  `ALTER TABLE companies ADD COLUMN notify_chat_name VARCHAR(200)`,

  // ── task_session_messages: persist Claude chat history ──
  `CREATE TABLE IF NOT EXISTS task_session_messages (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    task_id     INT,
    session_id  VARCHAR(100) NOT NULL,
    role        VARCHAR(20) NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_tsm_session (session_id),
    INDEX idx_tsm_task (task_id)
  )`,
];

async function migrate() {
  let ok = 0, skipped = 0, failed = 0;

  try {
    console.log('Running migration v2...');
    for (const stmt of schema) {
      try {
        await pool.query(stmt);
        ok++;
      } catch (err: any) {
        const code = err.code || '';
        const msg = err.message || '';
        // Idempotent: skip "duplicate" and "already exists" errors
        if (code === 'ER_DUP_FIELDNAME' || code === 'ER_DUP_KEYNAME' ||
            code === 'ER_DUP_ENTRY' ||
            msg.includes('Duplicate column') || msg.includes('already exists') ||
            msg.includes('Duplicate key') || msg.includes('Duplicate entry') ||
            msg.includes('duplicate key')) {
          skipped++;
        } else {
          console.error(`  FAIL: ${stmt.substring(0, 80)}...`);
          console.error(`        ${msg}`);
          failed++;
        }
      }
    }
    console.log(`Migration v2: ${ok} ok, ${skipped} skipped, ${failed} failed`);
    if (failed > 0) process.exit(1);
  } catch (err) {
    console.error('Migration v2 failed:', err);
    process.exit(1);
  } finally {
    await pool.end();
  }
}

migrate();

// Task progress: which step of the query
export type TaskProgress = '入口导航' | '登录中' | '查询准备' | '查询中' | '已完成';

// Task status: lifecycle state
export type TaskStatus =
  | '进行中'
  | '暂停指令下发'
  | '暂停'
  | '继续指令已下发'
  | '终止指令下发'
  | '终止'
  | '完成';

export type LogLevel = 'INFO' | 'WARN' | 'ERROR';
export type LogCategory = 'system' | 'task' | 'command';

export interface UserWhitelist {
  id: number;
  phone: string;
  name: string | null;
  enabled: boolean;
  created_at: Date;
}

export interface Node {
  id: number;
  node_name: string;
  hostname: string | null;
  ip_address: string | null;
  status: 'online' | 'offline';
  max_concurrency: number;
  active_sessions: number;
  memory_total_gb: number | null;
  cpu_cores: number | null;
  last_heartbeat: Date | null;
  last_sync_at: Date | null;
  created_at: Date;
}

export interface Company {
  id: number;
  name: string;
  short_name: string | null;
  province: string;
  province_url: string;
  feishu_contact_id: string | null;
  contact_name: string | null;
  contact_phone: string | null;
  account_status: 'offline' | 'online';
  last_query_at: Date | null;
  created_at: Date;
  updated_at: Date;
}

export interface Task {
  id: number;
  company_id: number;
  node_id: number | null;
  progress: TaskProgress;
  progress_desc: string | null;
  status: TaskStatus;
  total_vehicles: number;
  processed_vehicles: number;
  violations_found: number;
  current_page: number;
  scheduled_at: Date | null;
  started_at: Date | null;
  completed_at: Date | null;
  claude_session_id: string | null;
  error_message: string | null;
  created_at: Date;
  updated_at: Date;
}

export interface Log {
  id: number;
  task_id: number | null;
  node_id: number | null;
  level: LogLevel;
  category: LogCategory;
  message: string;
  detail: Record<string, unknown> | null;
  created_at: Date;
}

export interface Schedule {
  id: number;
  company_id: number;
  cron_expression: string;
  enabled: boolean;
  last_triggered_at: Date | null;
  next_run_at: Date | null;
  created_at: Date;
}

export interface Vehicle {
  id: number;
  company_id: number;
  node_id: number | null;
  plate_number: string;
  plate_type: string;
  plate_type_label: string;
  status_code: string;
  status_label: string;
  inspection_date: string;
  unprocessed_count: number;
  tag: string;
  tag_batch_id: string;
  query_date: string;
  created_at: Date;
  updated_at: Date;
}

export interface Profile {
  id: number;
  node_id: number | null;
  company_name: string;
  profile_name: string;
  profile_id: string;
  platform_url: string;
  instance_port: number | null;
  last_login: Date | null;
  is_logged_in: boolean;
  created_at: Date;
}

export interface CompanyNodeBinding {
  id: number;
  company_id: number;
  node_id: number;
  is_active: boolean;
  bound_at: Date;
  unbound_at: Date | null;
}

export interface SyncLog {
  id: number;
  node_id: number;
  sync_type: 'task_complete' | 'periodic' | 'manual';
  task_id: number | null;
  companies: number;
  vehicles: number;
  violations_ins: number;
  violations_upd: number;
  status: string;
  error_message: string | null;
  created_at: Date;
}

// WebSocket message types
export type WsMessageType =
  // tray → cloud
  | 'register'
  | 'heartbeat'
  | 'stream_output'
  | 'progress_update'
  | 'status_ack'
  | 'task_completed'
  | 'task_failed'
  | 'log'
  | 'sync_data'
  | 'sync_ack'
  | 'keepalive_status'
  | 'qr_code'
  | 'qr_expired'
  | 'login_ok'
  | 'login_failed'
  // cloud → tray
  | 'assign_task'
  | 'pause_task'
  | 'resume_task'
  | 'terminate_task'
  | 'update_config'
  | 'trigger_login'
  | 'trigger_sync';

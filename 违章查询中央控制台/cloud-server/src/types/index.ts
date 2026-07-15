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
  // cloud → tray
  | 'assign_task'
  | 'pause_task'
  | 'resume_task'
  | 'terminate_task'
  | 'update_config';

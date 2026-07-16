import type { ChildProcess } from 'child_process';

export type TaskProgress = '入口导航' | '登录中' | '查询准备' | '查询中' | '已完成';
export type TaskStatus = '进行中' | '暂停指令下发' | '暂停' | '继续指令已下发' | '终止指令下发' | '终止' | '完成';

export interface ManagedProcess {
  pid: number;
  taskId: number;
  companyId: number;
  companyName: string;
  sessionId: string;
  status: TaskStatus;
  child: ChildProcess;
  seq: number;
}

export interface WsMessage {
  type: string;
  [key: string]: unknown;
}

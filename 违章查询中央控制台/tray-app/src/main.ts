import { app, Tray, Menu, nativeImage, shell } from 'electron';
import { spawn } from 'child_process';
import WebSocket from 'ws';
import * as os from 'os';
import * as path from 'path';
import * as fs from 'fs';
import { ProcessManager } from './process-manager';
import type { WsMessage } from './types';

const CLOUD_WS_URL = process.env.CLOUD_WS_URL || 'ws://localhost:3001/ws?client=tray';
const NODE_ID = process.env.NODE_ID || os.hostname();
const MAX_CONCURRENCY = parseInt(process.env.MAX_CONCURRENCY || '15', 10);
const CLAUDE_PATH = process.env.CLAUDE_PATH || 'claude';

const isMacOS = process.platform === 'darwin';
const isWindows = process.platform === 'win32';

let tray: Tray | null = null;
let ws: WebSocket | null = null;
let reconnectTimer: NodeJS.Timeout | null = null;
const processManager = new ProcessManager(MAX_CONCURRENCY);

function send(msg: WsMessage) {
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

function findIconFile(): string {
  // Production: extraResources → Contents/Resources/
  const prodPath = path.join(process.resourcesPath || '', 'tray-icon.png');
  if (fs.existsSync(prodPath)) {
    console.log('Using production icon:', prodPath);
    return prodPath;
  }
  // Dev: dist/ directory
  const devPath = path.join(__dirname, 'tray-icon.png');
  if (fs.existsSync(devPath)) {
    console.log('Using dev icon:', devPath);
    return devPath;
  }
  console.log('No icon file found, generating fallback');
  return '';
}

function createTrayIcon(): Electron.NativeImage {
  const iconPath = findIconFile();
  if (iconPath) {
    const img = nativeImage.createFromPath(iconPath);
    if (!img.isEmpty()) return img;
  }
  // Fallback: generate visible blue circle
  const size = isMacOS ? 18 : 16;
  const buf = Buffer.alloc(size * size * 4, 0);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      if ((x - (size - 1) / 2) ** 2 + (y - (size - 1) / 2) ** 2 <= (size / 2 - 2) ** 2) {
        const i = (y * size + x) * 4;
        buf[i] = 74; buf[i + 1] = 144; buf[i + 2] = 217; buf[i + 3] = 255;
      }
    }
  }
  return nativeImage.createFromBuffer(buf, { width: size, height: size });
}

function connectWs() {
  ws = new WebSocket(CLOUD_WS_URL);

  ws.on('open', () => {
    console.log('[WS] Connected to cloud console');
    send({
      type: 'register',
      node_id: NODE_ID,
      hostname: os.hostname(),
      max_concurrency: MAX_CONCURRENCY,
      memory_total_gb: Math.round(os.totalmem() / 1024 / 1024 / 1024),
      cpu_cores: os.cpus().length,
    });

    const heartbeat = setInterval(() => {
      send({
        type: 'heartbeat',
        node_id: NODE_ID,
        active_sessions: processManager.activeCount,
        memory_used_gb: Math.round((os.totalmem() - os.freemem()) / 1024 / 1024 / 1024),
        cpu_percent: 0,
        sessions: processManager.getSessions(),
      });
    }, 10000);

    ws!.on('close', () => clearInterval(heartbeat));
  });

  ws.on('message', (data: Buffer) => {
    let msg: WsMessage;
    try { msg = JSON.parse(data.toString()); } catch { return; }

    switch (msg.type) {
      case 'assign_task': {
        const { task_id, company_id, company_name, province, session_id } = msg as unknown as {
          task_id: number; company_id: number; company_name: string; province: string; session_id: string;
        };

        // Safety net: if a process for this company is still running, terminate it first
        if (processManager.hasCompanySession(company_id)) {
          send({ type: 'log', task_id, level: 'WARN', category: 'system',
            message: `公司 ${company_name}(id=${company_id}) 已有活跃会话，先终止旧进程` });
          // Find and terminate old process for this company
          for (const [tid, proc] of processManager['processes'] as Map<number, { companyId: number; child: { kill: (s: string) => void } }>) {
            if (proc.companyId === company_id) {
              processManager.terminate(tid);
              break;
            }
          }
        }

        if (processManager.isFull) {
          send({ type: 'log', task_id, level: 'WARN', category: 'system',
            message: `进程池已满(${processManager.activeCountInfo})，任务排队` });
          return;
        }

        send({ type: 'log', task_id, level: 'INFO', category: 'task',
          message: `启动查询: ${company_name} (省份: ${province})` });

        processManager.launch(task_id, company_id, company_name, province, session_id, CLAUDE_PATH);

        send({
          type: 'status_ack', task_id,
          status: '进行中',
          message: `Claude Code 已启动, pid=${processManager.get(task_id)?.pid}, session=${session_id}`,
        });

        updateTrayMenu();
        break;
      }

      case 'session_create': {
        // Same as assign_task but from cloud server (node_agent.py compat)
        const sc = msg as unknown as {
          task_id: number; company_id: number; company_name: string; session_id: string; prompt?: string;
        };

        if (processManager.hasCompanySession(sc.company_id)) {
          send({ type: 'log', task_id: sc.task_id, level: 'WARN', category: 'system',
            message: `公司 ${sc.company_name}(id=${sc.company_id}) 已有活跃会话，先终止旧进程` });
          for (const [tid, proc] of processManager['processes'] as Map<number, { companyId: number; child: { kill: (s: string) => void } }>) {
            if (proc.companyId === sc.company_id) {
              processManager.terminate(tid);
              break;
            }
          }
        }

        if (processManager.isFull) {
          send({ type: 'log', task_id: sc.task_id, level: 'WARN', category: 'system',
            message: `进程池已满(${processManager.activeCountInfo})，任务排队` });
          return;
        }

        send({ type: 'log', task_id: sc.task_id, level: 'INFO', category: 'task',
          message: `启动查询: ${sc.company_name} (session_create)` });

        // Use server-provided prompt for richer query instructions
        processManager.launch(sc.task_id, sc.company_id, sc.company_name, '', sc.session_id, CLAUDE_PATH, sc.prompt);

        send({
          type: 'status_ack', task_id: sc.task_id,
          status: '进行中',
          message: `Claude Code 已启动, pid=${processManager.get(sc.task_id)?.pid}, session=${sc.session_id}`,
        });

        updateTrayMenu();
        break;
      }

      case 'pause_task': {
        const taskId = msg.task_id as number;
        const result = processManager.pause(taskId);
        if (result) {
          const proc = processManager.get(taskId);
          send({ type: 'status_ack', task_id: taskId, status: '暂停',
            message: `${result.method}, pid=${result.pid}, session=${proc?.sessionId}` });
          updateTrayMenu();
        }
        break;
      }

      case 'resume_task': {
        const taskId = msg.task_id as number;
        const result = processManager.resume(taskId);
        if (result) {
          const proc = processManager.get(taskId);
          send({ type: 'status_ack', task_id: taskId, status: '进行中',
            message: `${result.method}, pid=${result.pid}, session=${proc?.sessionId}` });
          updateTrayMenu();
        } else if (isWindows) {
          send({ type: 'log', task_id: taskId, level: 'WARN', category: 'system',
            message: 'Windows 不支持恢复已暂停的进程，请重新创建任务' });
        }
        break;
      }

      case 'terminate_task': {
        const taskId = msg.task_id as number;
        const result = processManager.terminate(taskId);
        if (result) {
          const proc = processManager.get(taskId);
          send({ type: 'status_ack', task_id: taskId, status: '终止',
            message: `${result.method}, pid=${result.pid}, session=${proc?.sessionId}` });
          updateTrayMenu();
        }
        break;
      }

      case 'trigger_login': {
        const { company_name, province_url } = msg as unknown as {
          company_name: string; province_url: string;
        };

        const loginSessionId = `login-${company_name}-${Date.now()}`;
        send({
          type: 'log', level: 'INFO', category: 'login',
          message: `启动扫码登录: ${company_name}${province_url ? ` (${province_url})` : ''}`,
        });

        // Use a special prompt that instructs Claude to only do login + keepalive
        const loginPrompt = `启动${company_name}违章查询登录任务，本次只执行登录和保活不执行具体违章查询`;

        const cmd = isWindows
          ? (CLAUDE_PATH.endsWith('.cmd') ? CLAUDE_PATH : `${CLAUDE_PATH}.cmd`)
          : CLAUDE_PATH;

        const child = spawn(cmd, ['--session', loginSessionId, '--prompt', loginPrompt], {
          stdio: ['ignore', 'pipe', 'pipe'],
          env: { ...process.env },
          shell: isWindows,
        });

        send({
          type: 'log', level: 'INFO', category: 'login',
          message: `Claude Code 已启动, pid=${child.pid}, session=${loginSessionId}`,
        });

        child.stdout?.on('data', (data: Buffer) => {
          for (const line of data.toString().split('\n').filter(Boolean)) {
            send({ type: 'stream_output', stream: 'stdout', line, seq: 0 });
          }
        });

        child.stderr?.on('data', (data: Buffer) => {
          for (const line of data.toString().split('\n').filter(Boolean)) {
            send({ type: 'stream_output', stream: 'stderr', line, seq: 0 });
          }
        });

        child.on('exit', (code) => {
          send({
            type: 'log', level: code === 0 ? 'INFO' : 'ERROR', category: 'login',
            message: `扫码登录进程退出: ${company_name}, exit code=${code}`,
          });
        });

        child.on('error', (err) => {
          send({
            type: 'log', level: 'ERROR', category: 'login',
            message: `扫码登录进程错误: ${company_name}, ${err.message}`,
          });
        });

        updateTrayMenu();
        break;
      }

      case 'session_message': {
        // Forward chat message to the Claude process's stdin
        const sm = msg as unknown as {
          session_id?: string; company_id?: string; task_id?: number; text: string;
        };
        let proc: ReturnType<typeof processManager.get> | undefined;
        if (sm.task_id) {
          proc = processManager.get(sm.task_id);
        } else if (sm.company_id) {
          proc = processManager.getByCompanyId(parseInt(sm.company_id, 10));
        }
        if (proc && sm.text) {
          const sent = processManager.sendStdin(proc.taskId, sm.text);
          send({
            type: 'log', task_id: proc.taskId, level: 'INFO', category: 'chat',
            message: sent ? `转发用户消息到会话 ${proc.sessionId}: ${sm.text.substring(0, 80)}`
              : `转发失败: stdin 不可用`,
          });
        } else {
          send({
            type: 'log', level: 'WARN', category: 'chat',
            message: `session_message 无法路由: task_id=${sm.task_id}, company_id=${sm.company_id}`,
          });
        }
        break;
      }

      case 'update_config': {
        const { max_concurrency } = msg as { max_concurrency?: number };
        if (max_concurrency) processManager.setMaxConcurrency(max_concurrency);
        break;
      }
    }
  });

  ws.on('close', () => {
    console.log('[WS] Disconnected, reconnecting in 5s...');
    ws = null;
    reconnectTimer = setTimeout(connectWs, 5000);
  });

  ws.on('error', (err) => {
    console.error('[WS] Error:', err.message);
    ws?.close();
  });
}

function updateTrayMenu() {
  if (!tray) return;

  const processes = Array.from(processManager['processes'].values()) as { companyName: string; pid: number; status: string }[];

  const contextMenu = Menu.buildFromTemplate([
    { label: `活跃进程: ${processManager.activeCountInfo}`, enabled: false },
    { type: 'separator' },
    { label: '打开控制台', click: () => shell.openExternal('http://localhost:5173') },
    { label: '查看进程', enabled: false },
    ...(processes.length > 0
      ? processes.map((proc) => ({
          label: `  ${proc.companyName} [pid:${proc.pid}] ${proc.status}`,
          enabled: false,
        }))
      : [{ label: '  (无活跃进程)', enabled: false }]
    ),
    { type: 'separator' },
    { label: '退出', click: () => { app.quit(); } },
  ]);

  tray.setContextMenu(contextMenu);
}

app.whenReady().then(() => {
  // Windows: set app user model ID for proper taskbar/tray integration
  if (isWindows) {
    app.setAppUserModelId('com.violation-query.tray');
  }

  const icon = createTrayIcon();

  // Create tray with explicit image
  tray = new Tray(icon);
  if (isMacOS) {
    tray.setImage(icon);
    tray.setTitle('违章');
    tray.setToolTip('违章查询托盘');
    app.setActivationPolicy('accessory');
  }

  console.log('Tray created, isEmpty:', icon.isEmpty(), 'size:', icon.getSize(), 'isTemplate:', icon.isTemplateImage());
  setTimeout(() => console.log('Tray bounds (delayed):', tray!.getBounds()), 1000);

  updateTrayMenu();
  connectWs();
});

// Process manager events
processManager.on('stream', (taskId: number, stream: string, line: string, seq: number) => {
  send({ type: 'stream_output', task_id: taskId, stream, line, seq });
});

processManager.on('progress', (taskId: number, progress: string, progressDesc: string, stats: Record<string, number>) => {
  send({
    type: 'progress_update',
    task_id: taskId,
    progress,
    progress_desc: progressDesc,
    ...stats,
  });
});

processManager.on('progress_desc', (taskId: number, desc: string) => {
  send({ type: 'progress_update', task_id: taskId, progress_desc: desc });
});

processManager.on('exit', (taskId: number, code: number | null) => {
  if (code === 0) {
    send({ type: 'task_completed', task_id: taskId });
    send({ type: 'log', task_id: taskId, level: 'INFO', category: 'task', message: `任务完成, exit code=${code}` });
  } else {
    send({ type: 'task_failed', task_id: taskId, error: `进程退出, exit code=${code}` });
    send({ type: 'log', task_id: taskId, level: 'ERROR', category: 'task', message: `进程异常退出, exit code=${code}` });
  }
  updateTrayMenu();
});

processManager.on('error', (taskId: number, error: string) => {
  send({ type: 'task_failed', task_id: taskId, error });
  send({ type: 'log', task_id: taskId, level: 'ERROR', category: 'system', message: `进程错误: ${error}` });
  updateTrayMenu();
});

app.on('window-all-closed', () => { /* don't quit, tray stays */ });
app.on('before-quit', () => {
  for (const [, proc] of processManager['processes']) {
    try { proc.child.kill('SIGTERM'); } catch { /* ignore */ }
  }
  ws?.close();
  if (reconnectTimer) clearTimeout(reconnectTimer);
});

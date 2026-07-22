import { spawn, ChildProcess } from 'child_process';
import { EventEmitter } from 'events';
import { platform } from 'os';
import type { ManagedProcess } from './types';
import { parseLine, generateProgressDesc } from './progress-parser';

const isWindows = platform() === 'win32';
const isMacOS = platform() === 'darwin';

export class ProcessManager extends EventEmitter {
  private processes = new Map<number, ManagedProcess>();
  private companySessionMap = new Map<number, string>();
  private maxConcurrency: number;

  constructor(maxConcurrency = 15) {
    super();
    this.maxConcurrency = maxConcurrency;
  }

  setMaxConcurrency(n: number) { this.maxConcurrency = n; }
  get activeCount() { return this.processes.size; }
  get activeCountInfo() { return `${this.processes.size}/${this.maxConcurrency}`; }
  get isFull() { return this.processes.size >= this.maxConcurrency; }
  get platform() { return platform(); }

  getSessions(): { taskId: number; companyId: number; sessionId: string }[] {
    return Array.from(this.processes.values()).map((p) => ({
      taskId: p.taskId, companyId: p.companyId, sessionId: p.sessionId,
    }));
  }

  hasCompanySession(companyId: number): boolean {
    return this.companySessionMap.has(companyId);
  }

  getCompanySession(companyId: number): string | undefined {
    return this.companySessionMap.get(companyId);
  }

  launch(taskId: number, companyId: number, companyName: string, province: string, sessionId: string, claudePath = 'claude', promptOverride?: string): ManagedProcess {
    let prompt: string;
    if (promptOverride) {
      prompt = promptOverride;
    } else if (province) {
      prompt = `查询${companyName}的车辆违章，省份${province}`;
    } else {
      prompt = `查询${companyName}的车辆违章`;
    }

    // On Windows, Claude Code may be invoked as 'claude.cmd' or via full path
    const cmd = isWindows ? (claudePath.endsWith('.cmd') ? claudePath : `${claudePath}.cmd`) : claudePath;

    const child = spawn(cmd, ['--session', sessionId, '--prompt', prompt], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env },
      shell: isWindows, // Use shell on Windows for .cmd batch files
    });

    const proc: ManagedProcess = {
      pid: child.pid!,
      taskId,
      companyId,
      companyName,
      sessionId,
      status: '进行中',
      child,
      seq: 0,
    };

    this.processes.set(taskId, proc);
    this.companySessionMap.set(companyId, sessionId);

    child.stdout?.on('data', (data: Buffer) => {
      const lines = data.toString().split('\n').filter(Boolean);
      for (const line of lines) {
        proc.seq++;
        const parsed = parseLine(line);
        this.emit('stream', taskId, 'stdout', line, proc.seq);
        if (parsed.progress) {
          this.emit('progress', taskId, parsed.progress, parsed.progressDesc || line, parsed.stats ?? {});
        } else if (proc.seq % 10 === 0) {
          this.emit('progress_desc', taskId, generateProgressDesc(line));
        }
      }
    });

    child.stderr?.on('data', (data: Buffer) => {
      for (const line of data.toString().split('\n').filter(Boolean)) {
        proc.seq++;
        this.emit('stream', taskId, 'stderr', line, proc.seq);
      }
    });

    child.on('exit', (code) => {
      this.processes.delete(taskId);
      this.companySessionMap.delete(companyId);
      this.emit('exit', taskId, code);
    });

    child.on('error', (err) => {
      this.processes.delete(taskId);
      this.companySessionMap.delete(companyId);
      this.emit('error', taskId, err.message);
    });

    return proc;
  }

  // Pause: macOS → SIGSTOP (freeze), Windows → SIGTERM (no true freeze available)
  pause(taskId: number): { ok: boolean; method: string; pid: number } | null {
    const proc = this.processes.get(taskId);
    if (!proc || proc.status === '暂停') return null;

    if (isMacOS) {
      proc.child.kill('SIGSTOP');
      proc.status = '暂停';
      return { ok: true, method: 'SIGSTOP (冻结进程)', pid: proc.pid };
    } else {
      // Windows: kill process, mark as paused
      proc.child.kill('SIGTERM');
      proc.status = '暂停';
      this.processes.delete(taskId);
      this.companySessionMap.delete(proc.companyId);
      return { ok: true, method: 'SIGTERM (Windows不支持进程冻结，已终止进程)', pid: proc.pid };
    }
  }

  // Resume: macOS → SIGCONT, Windows → not supported (need to restart task)
  resume(taskId: number): { ok: boolean; method: string; pid: number } | null {
    const proc = this.processes.get(taskId);
    if (!proc) return null;

    if (isMacOS) {
      proc.child.kill('SIGCONT');
      proc.status = '进行中';
      return { ok: true, method: 'SIGCONT (恢复进程)', pid: proc.pid };
    }
    return null; // Windows cannot resume killed process
  }

  // Terminate: SIGTERM → SIGKILL after 10s (both platforms)
  terminate(taskId: number): { ok: boolean; method: string; pid: number } | null {
    const proc = this.processes.get(taskId);
    if (!proc) return null;

    if (isWindows) {
      // Windows: taskkill /F /PID
      proc.child.kill('SIGTERM');
      proc.status = '终止指令下发';
      setTimeout(() => {
        try { proc.child.kill('SIGKILL'); } catch { /* */ }
      }, 10000);
      return { ok: true, method: 'SIGTERM', pid: proc.pid };
    } else {
      proc.child.kill('SIGTERM');
      proc.status = '终止指令下发';
      setTimeout(() => {
        try { proc.child.kill('SIGKILL'); } catch { /* */ }
      }, 10000);
      return { ok: true, method: 'SIGTERM', pid: proc.pid };
    }
  }

  get(taskId: number): ManagedProcess | undefined {
    return this.processes.get(taskId);
  }

  getByCompanyId(companyId: number): ManagedProcess | undefined {
    for (const proc of this.processes.values()) {
      if (proc.companyId === companyId) return proc;
    }
    return undefined;
  }

  sendStdin(taskId: number, text: string): boolean {
    const proc = this.processes.get(taskId);
    if (!proc || !proc.child.stdin || proc.child.stdin.destroyed) return false;
    try {
      proc.child.stdin.write(text + '\n');
      return true;
    } catch {
      return false;
    }
  }
}

import { FastifyInstance } from 'fastify';
import { WebSocket } from 'ws';
import pool from '../db/index.js';
import { upsertCompaniesList, upsertVehicle, upsertViolations, validateCompanies, getKnownCompanyNames } from '../db/sync.js';

// Connected tray nodes: Map<node_id, WebSocket>
const nodes = new Map<string, WebSocket>();
// Frontend clients for streaming: Map<task_id, Set<WebSocket>>
const taskSubscribers = new Map<number, Set<WebSocket>>();
// Frontend general subscribers (receive all broadcasts)
const frontendClients = new Set<WebSocket>();

export function getNodeWs(nodeId: string): WebSocket | undefined {
  return nodes.get(nodeId);
}

export function broadcastToFrontend(payload: string) {
  for (const client of frontendClients) {
    if (client.readyState === WebSocket.OPEN) client.send(payload);
  }
  for (const [, subs] of taskSubscribers) {
    for (const client of subs) {
      if (client.readyState === WebSocket.OPEN) client.send(payload);
    }
  }
}

export function subscribeTask(taskId: number, ws: WebSocket) {
  if (!taskSubscribers.has(taskId)) taskSubscribers.set(taskId, new Set());
  taskSubscribers.get(taskId)!.add(ws);
}

export function unsubscribeTask(taskId: number, ws: WebSocket) {
  taskSubscribers.get(taskId)?.delete(ws);
}

async function handleMessage(ws: WebSocket, raw: string) {
  let msg: { type: string; [key: string]: unknown };
  try { msg = JSON.parse(raw); } catch { return; }

  switch (msg.type) {
    case 'register': {
      const nodeName = (msg.node_name as string) || (msg.hostname as string) || (msg.node_id as string);
      nodes.set(nodeName, ws);

      const [rows] = await pool.query('SELECT id FROM nodes WHERE node_name = ?', [nodeName]);
      if (rows.length === 0) {
        await pool.query(
          `INSERT INTO nodes (node_name, hostname, max_concurrency, memory_total_gb, cpu_cores, status)
           VALUES (?, ?, ?, ?, ?, 'online')`,
          [nodeName, msg.hostname || nodeName, msg.max_concurrency || 15, msg.memory_total_gb || 0, msg.cpu_cores || 0]
        );
      } else {
        await pool.query(
          `UPDATE nodes SET status='online', hostname=?, max_concurrency=?, memory_total_gb=?,
           cpu_cores=?, last_heartbeat=NOW() WHERE node_name=?`,
          [msg.hostname || '', msg.max_concurrency || 15, msg.memory_total_gb || 0, msg.cpu_cores || 0, nodeName]
        );
      }

      // Send register acknowledgment (建联确认)
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: 'register_ack',
          node_id: nodeName,
          message: 'registered',
          timestamp: new Date().toISOString(),
        }));

        // Push reporting schedule config for this node (避免离线遗漏)
        const [scheduleRows] = await pool.query(
          'SELECT id, enabled, frequency, times FROM reporting_schedules WHERE enabled = 1'
        ) as any;
        if (scheduleRows.length > 0) {
          ws.send(JSON.stringify({
            type: 'reporting_schedule_config',
            schedules: scheduleRows.map((s: any) => ({
              id: s.id,
              enabled: !!s.enabled,
              frequency: s.frequency || 'custom',
              times: typeof s.times === 'string' ? JSON.parse(s.times) : s.times,
            })),
          }));
        }
      }
      break;
    }

    case 'heartbeat': {
      const nodeName = msg.node_id as string;
      await pool.query(
        `UPDATE nodes SET status='online', active_sessions=?,
         cpu_percent=?, cpu_count=?, memory_total_gb=?, memory_used_gb=?, memory_percent=?,
         disk_total_gb=?, disk_used_gb=?, disk_percent=?,
         net_bytes_sent_mb=?, net_bytes_recv_mb=?, uptime_seconds=?, process_count=?,
         processes=?,
         last_heartbeat=NOW()
         WHERE node_name=?`,
        [msg.active_sessions || 0,
         msg.cpu_percent || 0, msg.cpu_count || 0,
         msg.memory_total_gb || 0, msg.memory_used_gb || 0, msg.memory_percent || 0,
         msg.disk_total_gb || 0, msg.disk_used_gb || 0, msg.disk_percent || 0,
         msg.net_bytes_sent_mb || 0, msg.net_bytes_recv_mb || 0,
         msg.uptime_seconds || 0, msg.process_count || 0,
         msg.processes ? JSON.stringify(msg.processes) : null,
         nodeName]
      );
      break;
    }

    case 'stream_output': {
      const taskId = msg.task_id as number;
      // Forward stream to frontend subscribers
      const subs = taskSubscribers.get(taskId);
      if (subs) {
        const payload = JSON.stringify(msg);
        for (const client of subs) {
          if (client.readyState === WebSocket.OPEN) client.send(payload);
        }
      }
      break;
    }

    case 'progress_update': {
      const { task_id, progress, progress_desc, processed_vehicles, total_vehicles,
        current_page, violations_found } = msg;
      await pool.query(
        `UPDATE tasks SET progress=?, progress_desc=?, processed_vehicles=?,
         total_vehicles=?, current_page=?, violations_found=?, updated_at=NOW()
         WHERE id=?`,
        [progress, progress_desc, processed_vehicles, total_vehicles, current_page, violations_found, task_id]
      );
      break;
    }

    case 'status_ack': {
      const { task_id, status, message } = msg;
      await pool.query('UPDATE tasks SET status=?, updated_at=NOW() WHERE id=?', [status, task_id]);
      await pool.query(
        `INSERT INTO logs (task_id, level, category, message)
         VALUES (?, 'INFO', 'command', ?)`,
        [task_id, message]
      );
      break;
    }

    case 'task_completed': {
      const { task_id, violations_found, processed_vehicles } = msg;
      await pool.query(
        `UPDATE tasks SET status='完成', progress='已完成', violations_found=?,
         processed_vehicles=?, completed_at=NOW(), updated_at=NOW() WHERE id=?`,
        [violations_found, processed_vehicles, task_id]
      );
      break;
    }

    case 'task_failed': {
      const { task_id, error } = msg;
      await pool.query(
        `UPDATE tasks SET status='完成', error_message=?, completed_at=NOW(), updated_at=NOW() WHERE id=?`,
        [error, task_id]
      );
      break;
    }

    case 'log': {
      const { task_id, level, category, message, detail } = msg;
      await pool.query(
        `INSERT INTO logs (task_id, level, category, message, detail)
         VALUES (?, ?, ?, ?, ?)`,
        [task_id || null, level || 'INFO', category || 'system', message, detail ? JSON.stringify(detail) : null]
      );
      break;
    }

    // ── Data sync ──
    case 'sync_data': {
      const { node_id, task_id, companies, vehicles, violations } = msg as {
        node_id?: string; task_id?: number; companies?: any[]; vehicles?: any[]; violations?: any[];
      };

      try {
        let stats = { companies: 0, vehicles: 0, violations_ins: 0, violations_upd: 0 };

        // ── Validate companies before processing ──
        const allCompanyNames = new Set<string>();
        if (Array.isArray(companies)) companies.forEach((c: any) => allCompanyNames.add(c.name));
        if (Array.isArray(vehicles)) vehicles.forEach((v: any) => allCompanyNames.add(v.company_name));
        if (Array.isArray(violations)) violations.forEach((v: any) => allCompanyNames.add(v.company_name));

        const knownCompanies = await getKnownCompanyNames([...allCompanyNames]);
        const unknownCompanies = [...allCompanyNames].filter(n => !knownCompanies.has(n));

        if (unknownCompanies.length > 0) {
          console.warn(`[Sync] 数据上报企业未匹配: ${unknownCompanies.join(', ')} (跳过 ${unknownCompanies.length} 个企业)`);
          await pool.query(
            `INSERT INTO logs (level, category, message)
             VALUES ('WARN', 'system', ?)`,
            [`数据上报企业未匹配: ${unknownCompanies.join(', ')} (跳过 ${unknownCompanies.length} 个企业)`]
          );
        }

        // If ALL companies are unknown, reject sync
        if (allCompanyNames.size > 0 && knownCompanies.size === 0) {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
              type: 'sync_ack', task_id, ok: false,
              error: `所有上报企业未匹配: ${unknownCompanies.join(', ')}`,
            }));
          }
          break;
        }

        if (Array.isArray(companies) && companies.length > 0) {
          const known = companies.filter((c: any) => knownCompanies.has(c.name));
          stats.companies = await upsertCompaniesList(known);
        }

        if (Array.isArray(vehicles) && vehicles.length > 0) {
          for (const v of vehicles) {
            if (!knownCompanies.has(v.company_name)) continue;
            await upsertVehicle(node_id || null, v);
            stats.vehicles++;
          }
        }

        if (Array.isArray(violations) && violations.length > 0) {
          const knownViolations = violations.filter((v: any) => knownCompanies.has(v.company_name));
          const result = await upsertViolations(node_id || null, task_id || null, knownViolations);
          stats.violations_ins = result.inserted;
          stats.violations_upd = result.updated;
        }

        // Update node last_sync_at
        if (node_id) {
          await pool.query(
            'UPDATE nodes SET last_sync_at = NOW() WHERE node_name = ?',
            [node_id]
          );
        }

        // Log sync
        const totalViolations = stats.violations_ins + stats.violations_upd;
        await pool.query(
          `INSERT INTO sync_logs (node_id, sync_type, task_id, companies, vehicles,
             violations_ins, violations_upd, status)
           SELECT id, 'task_complete', ?, ?, ?, ?, ?, 'success'
           FROM nodes WHERE node_name = ? LIMIT 1`,
          [task_id || null, stats.companies, stats.vehicles, stats.violations_ins, stats.violations_upd, node_id]
        );

        await pool.query(
          `INSERT INTO logs (task_id, level, category, message, detail)
           VALUES (?, 'INFO', 'system', ?, ?)`,
          [task_id || null,
           `数据同步完成: ${stats.vehicles}辆车, ${totalViolations}条违章 (${stats.violations_ins}新/${stats.violations_upd}更新)`,
           JSON.stringify(stats)]
        );

        // Send ack
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({
            type: 'sync_ack', task_id, ok: true, stats,
          }));
        }
      } catch (error: any) {
        console.error('[Sync] Error:', error);
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({
            type: 'sync_ack', task_id, ok: false, error: error.message,
          }));
        }
      }
      break;
    }

    // ── Keepalive status report ──
    case 'keepalive_status': {
      const { companies: keepaliveCompanies } = msg as { companies?: Array<{ name: string; is_logged_in: boolean; keepalive_alive: boolean }> };
      if (Array.isArray(keepaliveCompanies)) {
        // Validate companies
        const names = keepaliveCompanies.map((c: any) => c.name);
        const known = await getKnownCompanyNames(names);
        const unknownNames = names.filter((n: string) => !known.has(n));
        if (unknownNames.length > 0) {
          console.warn(`[Keepalive] 未知企业: ${unknownNames.join(', ')}`);
        }

        for (const c of keepaliveCompanies) {
          if (!known.has(c.name)) continue; // Skip unknown
          const newStatus = (c.is_logged_in && c.keepalive_alive) ? 'online' : 'offline';
          await pool.query(
            `UPDATE companies SET account_status = ? WHERE name = ?`,
            [newStatus, c.name]
          );
        }
      }
      // Forward to frontend
      const broadcastPayload = JSON.stringify(msg);
      for (const client of frontendClients) {
        if (client.readyState === WebSocket.OPEN) client.send(broadcastPayload);
      }
      for (const [, subs] of taskSubscribers) {
        for (const client of subs) {
          if (client.readyState === WebSocket.OPEN) client.send(broadcastPayload);
        }
      }
      break;
    }

    // ── QR code relay (node → frontend) ──
    case 'qr_code':
    case 'qr_expired': {
      const relayPayload = JSON.stringify(msg);
      for (const client of frontendClients) {
        if (client.readyState === WebSocket.OPEN) client.send(relayPayload);
      }
      for (const [, subs] of taskSubscribers) {
        for (const client of subs) {
          if (client.readyState === WebSocket.OPEN) client.send(relayPayload);
        }
      }
      break;
    }

    case 'login_ok': {
      const relayPayload = JSON.stringify(msg);
      for (const client of frontendClients) {
        if (client.readyState === WebSocket.OPEN) client.send(relayPayload);
      }
      for (const [, subs] of taskSubscribers) {
        for (const client of subs) {
          if (client.readyState === WebSocket.OPEN) client.send(relayPayload);
        }
      }
      const { company_name } = msg as unknown as { company_name: string };
      await pool.query(
        `UPDATE companies SET account_status = 'online' WHERE name = ?`,
        [company_name]
      );
      await pool.query(
        `INSERT INTO logs (level, category, message)
         VALUES ('INFO', 'system', ?)`,
        [`${company_name} 扫码登录成功`]
      );
      break;
    }

    // ── Session bridge relay (node → frontend) ──
    case 'session_created':
    case 'session_chunk':
    case 'session_marker':
    case 'session_done':
    case 'session_error':
    case 'session_list_result': {
      const relayPayload = JSON.stringify(msg);
      for (const client of frontendClients) {
        if (client.readyState === WebSocket.OPEN) client.send(relayPayload);
      }
      for (const [, subs] of taskSubscribers) {
        for (const client of subs) {
          if (client.readyState === WebSocket.OPEN) client.send(relayPayload);
        }
      }
      break;
    }

    // ── Keepalive login relay (node → frontend) ──
    case 'keepalive_login_progress':
    case 'keepalive_login_result': {
      const relayPayload = JSON.stringify(msg);
      for (const client of frontendClients) {
        if (client.readyState === WebSocket.OPEN) client.send(relayPayload);
      }
      for (const [, subs] of taskSubscribers) {
        for (const client of subs) {
          if (client.readyState === WebSocket.OPEN) client.send(relayPayload);
        }
      }
      break;
    }

    case 'login_failed': {
      const relayPayload = JSON.stringify(msg);
      for (const client of frontendClients) {
        if (client.readyState === WebSocket.OPEN) client.send(relayPayload);
      }
      for (const [, subs] of taskSubscribers) {
        for (const client of subs) {
          if (client.readyState === WebSocket.OPEN) client.send(relayPayload);
        }
      }
      const { company_name, reason } = msg as unknown as { company_name: string; reason: string };
      await pool.query(
        `INSERT INTO logs (level, category, message)
         VALUES ('WARN', 'system', ?)`,
        [`${company_name} 扫码登录失败: ${reason}`]
      );
      break;
    }
  }
}

export default async function wsHandler(app: FastifyInstance) {
  // 启动时将所有节点标记为离线（进程崩溃时 close 事件不保证触发）
  // 节点重新 register 后会自动切回 online
  pool.query("UPDATE nodes SET status='offline' WHERE status='online'").catch(() => {});
  console.log('[ws] 启动：将所有在线节点重置为 offline，等待 re-register');

  app.get('/ws', { websocket: true }, (socket, req) => {
    // Determine if this is a tray node or frontend client
    const url = new URL(req.url || '', `http://${req.headers.host}`);
    const clientType = url.searchParams.get('client') || 'frontend';
    const taskId = url.searchParams.get('task_id');

    if (clientType === 'tray' || clientType === 'node') {
      // Tray node connection - persistent
      socket.on('message', (data: Buffer) => {
        handleMessage(socket, data.toString());
      });

      socket.on('close', () => {
        for (const [name, ws] of nodes) {
          if (ws === socket) {
            pool.query("UPDATE nodes SET status='offline' WHERE node_name=?", [name]).catch(() => {});
            nodes.delete(name);
            break;
          }
        }
      });
    } else if (taskId) {
      // Frontend client subscribing to a specific task's stream
      subscribeTask(parseInt(taskId), socket);
      socket.on('close', () => {
        unsubscribeTask(parseInt(taskId), socket);
      });
    } else {
      // Frontend general connection - receives all task updates and broadcasts
      frontendClients.add(socket);
      socket.on('close', () => {
        frontendClients.delete(socket);
        for (const [, subs] of taskSubscribers) {
          subs.delete(socket);
        }
      });
    }
  });
}

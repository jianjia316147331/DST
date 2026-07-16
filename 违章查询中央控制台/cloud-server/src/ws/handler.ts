import { FastifyInstance } from 'fastify';
import { WebSocket } from 'ws';
import pool from '../db/index.js';

// Connected tray nodes: Map<node_id, WebSocket>
const nodes = new Map<string, WebSocket>();
// Frontend clients for streaming: Map<task_id, Set<WebSocket>>
const taskSubscribers = new Map<number, Set<WebSocket>>();

export function getNodeWs(nodeId: string): WebSocket | undefined {
  return nodes.get(nodeId);
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

      const { rows } = await pool.query('SELECT id FROM nodes WHERE node_name = $1', [nodeName]);
      if (rows.length === 0) {
        await pool.query(
          `INSERT INTO nodes (node_name, hostname, max_concurrency, memory_total_gb, cpu_cores, status)
           VALUES ($1, $2, $3, $4, $5, 'online')`,
          [nodeName, msg.hostname || nodeName, msg.max_concurrency || 15, msg.memory_total_gb || 0, msg.cpu_cores || 0]
        );
      } else {
        await pool.query(
          `UPDATE nodes SET status='online', hostname=$1, max_concurrency=$2, memory_total_gb=$3,
           cpu_cores=$4, last_heartbeat=NOW() WHERE node_name=$5`,
          [msg.hostname || '', msg.max_concurrency || 15, msg.memory_total_gb || 0, msg.cpu_cores || 0, nodeName]
        );
      }
      break;
    }

    case 'heartbeat': {
      const nodeName = msg.node_id as string;
      await pool.query(
        `UPDATE nodes SET status='online', active_sessions=$1, last_heartbeat=NOW()
         WHERE node_name=$2`,
        [msg.active_sessions || 0, nodeName]
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
        `UPDATE tasks SET progress=$1, progress_desc=$2, processed_vehicles=$3,
         total_vehicles=$4, current_page=$5, violations_found=$6, updated_at=NOW()
         WHERE id=$7`,
        [progress, progress_desc, processed_vehicles, total_vehicles, current_page, violations_found, task_id]
      );
      break;
    }

    case 'status_ack': {
      const { task_id, status, message } = msg;
      await pool.query('UPDATE tasks SET status=$1, updated_at=NOW() WHERE id=$2', [status, task_id]);
      await pool.query(
        `INSERT INTO logs (task_id, level, category, message)
         VALUES ($1, 'INFO', 'command', $2)`,
        [task_id, message]
      );
      break;
    }

    case 'task_completed': {
      const { task_id, violations_found, processed_vehicles } = msg;
      await pool.query(
        `UPDATE tasks SET status='完成', progress='已完成', violations_found=$1,
         processed_vehicles=$2, completed_at=NOW(), updated_at=NOW() WHERE id=$3`,
        [violations_found, processed_vehicles, task_id]
      );
      break;
    }

    case 'task_failed': {
      const { task_id, error } = msg;
      await pool.query(
        `UPDATE tasks SET status='完成', error_message=$1, completed_at=NOW(), updated_at=NOW() WHERE id=$2`,
        [error, task_id]
      );
      break;
    }

    case 'log': {
      const { task_id, level, category, message, detail } = msg;
      await pool.query(
        `INSERT INTO logs (task_id, level, category, message, detail)
         VALUES ($1, $2, $3, $4, $5)`,
        [task_id || null, level || 'INFO', category || 'system', message, detail ? JSON.stringify(detail) : null]
      );
      break;
    }
  }
}

export default async function wsHandler(app: FastifyInstance) {
  app.get('/ws', { websocket: true }, (socket, req) => {
    // Determine if this is a tray node or frontend client
    const url = new URL(req.url || '', `http://${req.headers.host}`);
    const clientType = url.searchParams.get('client') || 'frontend';
    const taskId = url.searchParams.get('task_id');

    if (clientType === 'tray') {
      // Tray node connection - persistent
      socket.on('message', (data: Buffer) => {
        handleMessage(socket, data.toString());
      });

      socket.on('close', () => {
        for (const [name, ws] of nodes) {
          if (ws === socket) {
            pool.query("UPDATE nodes SET status='offline' WHERE node_name=$1", [name]).catch(() => {});
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
      // Frontend general connection - receives all task updates
      socket.on('close', () => {
        for (const [, subs] of taskSubscribers) {
          subs.delete(socket);
        }
      });
    }
  });
}

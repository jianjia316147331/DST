import { useEffect, useState, useCallback } from 'react';
import { Card, Tabs, Tag, Button, Space, message, Modal, Progress, Empty, Descriptions, Select, Input } from 'antd';
import { PauseCircleOutlined, PlayCircleOutlined, StopOutlined, ExpandAltOutlined, PlusOutlined, WechatOutlined, SendOutlined } from '@ant-design/icons';
import api from '../api';

interface Task {
  id: number;
  company_id: number;
  company_name?: string;
  province?: string;
  node_name?: string;
  progress: string;
  progress_desc: string | null;
  status: string;
  total_vehicles: number;
  processed_vehicles: number;
  violations_found: number;
  current_page: number;
  claude_session_id: string | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
}

const PROGRESS_STEPS = ['入口导航', '登录中', '查询准备', '查询中', '已完成'];

function TaskCard({ task, onRefresh, onOpenChat }: { task: Task; onRefresh: () => void; onOpenChat: (task: Task) => void }) {
  const [streamOpen, setStreamOpen] = useState(false);
  const [streamLines, setStreamLines] = useState<string[]>([]);
  const [ws, setWs] = useState<WebSocket | null>(null);

  const toggleStream = useCallback(() => {
    if (streamOpen) {
      ws?.close();
      setWs(null);
      setStreamOpen(false);
      return;
    }
    setStreamOpen(true);
    const wsUrl = import.meta.env.VITE_WS_URL || 'ws://localhost:3001';
    const socket = new WebSocket(`${wsUrl}/ws?client=frontend&task_id=${task.id}`);
    socket.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'stream_output') {
          setStreamLines((prev) => [...prev.slice(-500), `${msg.stream === 'stderr' ? '[ERR] ' : ''}${msg.line}`]);
        }
      } catch { /* ignore */ }
    };
    setWs(socket);
  }, [streamOpen, task.id, ws]);

  const handleAction = async (action: string) => {
    try {
      await api.post(`/api/tasks/${task.id}/${action}`);
      message.success('指令已下发');
      onRefresh();
    } catch (err: unknown) {
      const res = (err as { response?: { data?: { error?: string } } })?.response?.data;
      message.error(res?.error || '操作失败');
    }
  };

  const pct = task.total_vehicles > 0 ? Math.round((task.processed_vehicles / task.total_vehicles) * 100) : 0;
  const currentStep = PROGRESS_STEPS.indexOf(task.progress);

  return (
    <Card
      style={{ marginBottom: 16 }}
      title={
        <Space>
          <span>{task.company_name || `公司 #${task.company_id}`}</span>
          <Tag color="processing">{task.progress}</Tag>
          {task.claude_session_id && <Tag>{task.claude_session_id}</Tag>}
        </Space>
      }
      extra={
        <Space>
          {task.status === '进行中' && (
            <>
              <Button size="small" icon={<PauseCircleOutlined />} onClick={() => handleAction('pause')}>暂停</Button>
              <Button size="small" danger icon={<StopOutlined />} onClick={() => handleAction('terminate')}>终止</Button>
            </>
          )}
          {task.status === '暂停' && (
            <Button size="small" type="primary" icon={<PlayCircleOutlined />} onClick={() => handleAction('resume')}>继续</Button>
          )}
          {task.status === '进行中' && task.claude_session_id && (
            <Button size="small" icon={<WechatOutlined />} onClick={() => onOpenChat(task)}>对话</Button>
          )}
          <Button size="small" icon={<ExpandAltOutlined />} onClick={toggleStream}>{streamOpen ? '收起输出' : '实时输出'}</Button>
        </Space>
      }
    >
      <div style={{ marginBottom: 8 }}>
        <span style={{ fontWeight: 'bold', marginRight: 8 }}>{task.processed_vehicles} / {task.total_vehicles} 台</span>
        <Progress percent={pct} style={{ width: 200, display: 'inline-block' }} />
      </div>

      <div style={{ marginBottom: 12, display: 'flex', gap: 4, alignItems: 'center', fontSize: 13 }}>
        {PROGRESS_STEPS.map((step, i) => (
          <span key={step} style={{ display: 'flex', alignItems: 'center' }}>
            {i > 0 && <span style={{ margin: '0 4px', color: '#ccc' }}>→</span>}
            <Tag color={i < currentStep ? 'success' : i === currentStep ? 'processing' : 'default'} style={{ margin: 0 }}>
              {i < currentStep ? '✓' : i === currentStep ? '⟳' : '○'} {step}
            </Tag>
          </span>
        ))}
      </div>

      <Descriptions size="small" column={4}>
        <Descriptions.Item label="第几页">{task.current_page || '-'}</Descriptions.Item>
        <Descriptions.Item label="发现违章">{task.violations_found} 条</Descriptions.Item>
        <Descriptions.Item label="节点">{task.node_name || '未分配'}</Descriptions.Item>
        <Descriptions.Item label="状态">{task.status}</Descriptions.Item>
      </Descriptions>

      {task.progress_desc && (
        <div style={{ marginTop: 8, padding: '4px 8px', background: '#f6ffed', borderRadius: 4, fontSize: 13 }}>
          当前: {task.progress_desc}
        </div>
      )}

      {task.error_message && (
        <div style={{ marginTop: 8, padding: '4px 8px', background: '#fff2f0', borderRadius: 4, fontSize: 13, color: '#ff4d4f' }}>
          错误: {task.error_message}
        </div>
      )}

      {streamOpen && (
        <div style={{ marginTop: 12, background: '#1e1e1e', color: '#d4d4d4', padding: 12, borderRadius: 4, maxHeight: 300, overflow: 'auto', fontFamily: 'monospace', fontSize: 12 }}>
          {streamLines.length === 0 && <div style={{ color: '#666' }}>等待输出...</div>}
          {streamLines.map((line, i) => <div key={i} style={{ whiteSpace: 'pre-wrap' }}>{line}</div>)}
        </div>
      )}
    </Card>
  );
}

export default function Tasks() {
  const [activeTasks, setActiveTasks] = useState<Task[]>([]);
  const [historyTasks, setHistoryTasks] = useState<Task[]>([]);
  const [pausedTasks, setPausedTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [companies, setCompanies] = useState<{ id: number; name: string }[]>([]);
  const [selectedCompanyId, setSelectedCompanyId] = useState<number | null>(null);
  const [createLoading, setCreateLoading] = useState(false);

  // Chat state
  const [chatOpen, setChatOpen] = useState(false);
  const [chatTask, setChatTask] = useState<Task | null>(null);
  const [chatMessages, setChatMessages] = useState<{ text: string; isUser?: boolean }[]>([]);
  const [chatInput, setChatInput] = useState('');

  const handleCreateTask = async () => {
    if (!selectedCompanyId) { message.warning('请选择公司'); return; }
    setCreateLoading(true);
    try {
      await api.post('/api/tasks', { company_id: selectedCompanyId });
      message.success('任务已创建');
      setCreateOpen(false);
      setSelectedCompanyId(null);
      fetchAll();
    } catch (err: unknown) {
      const res = (err as { response?: { data?: { error?: string; message?: string; existingTask?: { id: number } } } })?.response?.data;
      if (res?.error === 'ACTIVE_TASK_EXISTS') {
        Modal.confirm({
          title: '该公司已有查询任务进行中',
          content: `检测到任务 #${res.existingTask!.id} 正在进行中。是否终止前序任务并启动新任务？`,
          okText: '终止并启动',
          cancelText: '取消',
          okType: 'danger',
          onOk: async () => {
            await api.post('/api/tasks/force-start', { company_id: selectedCompanyId });
            message.success('新任务已创建');
            setCreateOpen(false);
            setSelectedCompanyId(null);
            fetchAll();
          },
        });
      } else {
        message.error(res?.message || res?.error || '创建失败');
      }
    } finally {
      setCreateLoading(false);
    }
  };

  const fetchAll = useCallback(() => {
    setLoading(true);
    Promise.all([
      api.get('/api/tasks', { params: { status: '进行中', pageSize: 100 } }),
      api.get('/api/tasks', { params: { status: '暂停', pageSize: 100 } }),
      api.get('/api/tasks', { params: { status: '完成', pageSize: 50 } }),
      api.get('/api/tasks', { params: { status: '终止', pageSize: 50 } }),
    ]).then(([r1, r2, r3, r4]) => {
      setActiveTasks(r1.data.data);
      setPausedTasks(r2.data.data);
      setHistoryTasks([...r3.data.data, ...r4.data.data]);
    }).finally(() => setLoading(false));
  }, []);

  useEffect(() => { fetchAll(); }, [fetchAll]);
  // Auto-refresh every 10s
  useEffect(() => {
    const t = setInterval(fetchAll, 10000);
    return () => clearInterval(t);
  }, [fetchAll]);

  // Global WebSocket listener for session_chunk (chat updates)
  useEffect(() => {
    const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws?client=frontend`;
    const socket = new WebSocket(wsUrl);
    socket.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === 'session_chunk' && chatOpen && chatTask) {
          // Support both formats: direct msg.text (from node_agent.py) and msg.event (stream-json format)
          let text = msg.text || '';
          if (!text && msg.event) {
            const event = msg.event as any;
            if (event.type === 'assistant') {
              const blocks = event.message?.content || [];
              for (const b of blocks) {
                if (b.type === 'text' && b.text) text += b.text;
                else if (b.type === 'tool_use') text += `[调用工具: ${b.name}]`;
              }
            } else if (event.type === 'user') {
              const blocks = event.message?.content || [];
              for (const b of blocks) {
                if (b.type === 'tool_result') {
                  const content = typeof b.content === 'string' ? b.content : JSON.stringify(b.content);
                  text = `[结果] ${content.substring(0, 100)}`;
                }
              }
            }
          }
          if (text) {
            setChatMessages(prev => [...prev, { text }]);
          }
        }
      } catch { /* ignore */ }
    };
    return () => socket.close();
  }, [chatOpen, chatTask]);

  const openChat = async (task: Task) => {
    setChatTask(task);
    setChatMessages([]);
    // Fetch session history
    if (task.claude_session_id) {
      try {
        const { data: history } = await api.get(`/api/sync/session-history-by-session/${task.claude_session_id}`);
        const msgs = (history.data || []).map((m: any) => ({
          text: m.content,
          isUser: m.role === 'user',
        }));
        setChatMessages(msgs);
      } catch { /* ignore */ }
    } else {
      // Try by task ID
      try {
        const { data: history } = await api.get(`/api/sync/session-history/${task.id}`);
        const msgs = (history.data || []).map((m: any) => ({
          text: m.content,
          isUser: m.role === 'user',
        }));
        setChatMessages(msgs);
      } catch { /* ignore */ }
    }
    setChatOpen(true);
  };

  const sendChatMessage = async () => {
    if (!chatInput.trim() || !chatTask) return;
    const text = chatInput.trim();
    setChatMessages(prev => [...prev, { text, isUser: true }]);
    setChatInput('');
    try {
      await api.post('/api/sync/session-message', {
        task_id: chatTask.id,
        session_id: chatTask.claude_session_id || '',
        text,
      });
    } catch {
      setChatMessages(prev => [...prev, { text: '[发送失败]' }]);
    }
  };

  const openCreate = () => {
    setCreateOpen(true);
    api.get('/api/companies', { params: { pageSize: 500 } }).then(({ data }) => setCompanies(data.data));
  };

  return (
    <div>
    <div style={{ marginBottom: 16 }}>
      <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建任务</Button>
    </div>
    <Tabs defaultActiveKey="active" items={[
      {
        key: 'active', label: `进行中 (${activeTasks.length})`,
        children: (
          <div>
            {activeTasks.map((t) => <TaskCard key={t.id} task={t} onRefresh={fetchAll} onOpenChat={openChat} />)}
            {!loading && activeTasks.length === 0 && <Empty description="暂无进行中的任务" />}
          </div>
        ),
      },
      {
        key: 'paused', label: `暂停中 (${pausedTasks.length})`,
        children: (
          <div>
            {pausedTasks.map((t) => <TaskCard key={t.id} task={t} onRefresh={fetchAll} onOpenChat={openChat} />)}
            {!loading && pausedTasks.length === 0 && <Empty description="暂无暂停的任务" />}
          </div>
        ),
      },
      {
        key: 'history', label: '历史',
        children: (
          <div>
            {historyTasks.map((t) => (
              <Card key={t.id} style={{ marginBottom: 8 }} size="small">
                <Space>
                  <span>{t.company_name}</span>
                  <Tag>{t.status}</Tag>
                  <span>处理 {t.processed_vehicles}/{t.total_vehicles} 台</span>
                  <span>违章 {t.violations_found} 条</span>
                  <span style={{ color: '#999', fontSize: 12 }}>{t.completed_at ? new Date(t.completed_at).toLocaleString() : '-'}</span>
                </Space>
              </Card>
            ))}
            {!loading && historyTasks.length === 0 && <Empty description="暂无历史任务" />}
          </div>
        ),
      },
    ]} />

    <Modal title="新建查询任务" open={createOpen} onOk={handleCreateTask} onCancel={() => setCreateOpen(false)}
      confirmLoading={createLoading} okText="创建">
      <div style={{ marginBottom: 8 }}>选择要查询的公司：</div>
      <Select
        showSearch
        placeholder="搜索并选择公司"
        optionFilterProp="label"
        style={{ width: '100%' }}
        value={selectedCompanyId}
        onChange={setSelectedCompanyId}
        options={companies.map((c) => ({ label: c.name, value: c.id }))}
      />
    </Modal>

    {/* Chat modal for active task sessions */}
    <Modal
      title={`${chatTask?.company_name || ''} - 查询对话${chatTask ? ` (任务 #${chatTask.id})` : ''}`}
      open={chatOpen}
      onCancel={() => { setChatOpen(false); setChatTask(null); setChatMessages([]); }}
      footer={null}
      width={500}
    >
      <div style={{ height: 300, overflowY: 'auto', border: '1px solid #f0f0f0', borderRadius: 8, padding: 12, marginBottom: 12, background: '#fafafa' }}>
        {chatMessages.map((m, i) => (
          <div key={i} style={{
            marginBottom: 8,
            textAlign: m.isUser ? 'right' : 'left',
          }}>
            <div style={{
              display: 'inline-block',
              padding: '6px 12px',
              borderRadius: 8,
              background: m.isUser ? '#1677ff' : '#fff',
              color: m.isUser ? '#fff' : '#333',
              maxWidth: '85%',
              wordBreak: 'break-word',
              border: m.isUser ? 'none' : '1px solid #e8e8e8',
            }}>
              {m.text}
            </div>
          </div>
        ))}
        {chatMessages.length === 0 && (
          <div style={{ textAlign: 'center', color: '#999', paddingTop: 40 }}>等待 Claude 响应...</div>
        )}
      </div>
      <Space.Compact style={{ width: '100%' }}>
        <Input
          placeholder="输入消息..."
          value={chatInput}
          onChange={e => setChatInput(e.target.value)}
          onPressEnter={sendChatMessage}
        />
        <Button type="primary" icon={<SendOutlined />} onClick={sendChatMessage}>发送</Button>
      </Space.Compact>
    </Modal>
  </div>
  );
}

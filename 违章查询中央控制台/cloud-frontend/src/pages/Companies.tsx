import { useEffect, useState, useRef } from 'react';
import { Table, Button, Modal, Form, Input, Select, Space, Tag, message, Popconfirm, Image, Steps } from 'antd';
import { PlusOutlined, EditOutlined, DeleteOutlined, PlayCircleOutlined, QrcodeOutlined, SendOutlined } from '@ant-design/icons';
import api from '../api';

interface Company {
  id: number;
  name: string;
  short_name: string | null;
  province: string;
  contact_name: string;
  contact_phone: string;
  account_status: string;
  last_query_at: string | null;
  notify_chat_name: string | null;
}

const PROVINCES = [
  '四川', '福建', '广东', '浙江', '江苏', '上海', '北京',
  '湖北', '湖南', '山东', '河南', '河北', '安徽', '江西', '陕西', '重庆',
].map(v => ({ label: v, value: v }));

export default function Companies() {
  const [data, setData] = useState<Company[]>([]);
  const [loading, setLoading] = useState(false);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Company | null>(null);
  const [form] = Form.useForm();
  const [provinceFilter, setProvinceFilter] = useState<string>();
  const [statusFilter, setStatusFilter] = useState<string>();
  const [nodes, setNodes] = useState<{ id: number; node_name: string; status: string }[]>([]);
  const [bindings, setBindings] = useState<Record<number, { node_id: number; node_name: string }>>({});
  const [qrModalOpen, setQrModalOpen] = useState(false);
  const [qrImage, setQrImage] = useState('');
  const [qrCompanyName, setQrCompanyName] = useState('');
  const [loginConfirmOpen, setLoginConfirmOpen] = useState(false);
  const [loginCompany, setLoginCompany] = useState<Company | null>(null);
  const [loginLoading, setLoginLoading] = useState(false);
  const [queryConfirmOpen, setQueryConfirmOpen] = useState(false);
  const [queryCompany, setQueryCompany] = useState<Company | null>(null);
  const [queryLoading, setQueryLoading] = useState(false);
  const [pendingLoginCompanies, setPendingLoginCompanies] = useState<Set<string>>(new Set());
  const [loginPath, setLoginPath] = useState<string>(''); // 'keepalive' | 'session'
  const [sessionId, setSessionId] = useState('');
  const [chatMessages, setChatMessages] = useState<{ text: string; isUser?: boolean }[]>([]);
  const [chatInput, setChatInput] = useState('');
  const [chatTaskId, setChatTaskId] = useState<number | null>(null);
  const [chatSessionId, setChatSessionId] = useState<string>('');
  const [keepaliveSteps, setKeepaliveSteps] = useState<{ step: string; status: string }[]>([]);

  const fetch = (p = page, pf = provinceFilter, sf = statusFilter) => {
    setLoading(true);
    api.get('/api/companies', { params: { page: p, pageSize: 20, province: pf, account_status: sf } })
      .then(({ data: d }) => {
        setData(d.data);
        setTotal(d.total);
        // Load existing bindings for all companies
        Promise.all(d.data.map((c: Company) =>
          api.get(`/api/companies/${c.id}/bind`).then(({ data: b }) => b && { companyId: c.id, ...b }).catch(() => null)
        )).then((results) => {
          const map: Record<number, { node_id: number; node_name: string }> = {};
          results.filter(Boolean).forEach((b: any) => {
            if (b) map[b.companyId] = { node_id: b.node_id, node_name: b.node_name };
          });
          setBindings(map);
        });
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => { fetch(); }, []);

  // Load nodes for binding
  useEffect(() => {
    api.get('/api/nodes').then(({ data: d }) => setNodes(d.data || []));
  }, []);

  // WebSocket for QR code and keepalive
  useEffect(() => {
    const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws?client=frontend`;
    const socket = new WebSocket(wsUrl);
    socket.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === 'qr_code') {
          setQrCompanyName(msg.company_name);
          setQrImage(`data:image/png;base64,${msg.image_base64}`);
          setQrModalOpen(true);
        } else if (msg.type === 'login_ok') {
          message.success(`${msg.company_name} 登录成功`);
          setPendingLoginCompanies(prev => { const next = new Set(prev); next.delete(msg.company_name); return next; });
          setQrModalOpen(false);
          setQrImage('');
          setLoginPath('');
          setChatMessages([]);
          setKeepaliveSteps([]);
          fetch();
        } else if (msg.type === 'login_failed') {
          message.error(`${msg.company_name} 登录失败: ${msg.reason}`);
          setPendingLoginCompanies(prev => { const next = new Set(prev); next.delete(msg.company_name); return next; });
        } else if (msg.type === 'keepalive_status') {
          fetch();
        } else if (msg.type === 'keepalive_login_progress') {
          setKeepaliveSteps(prev => {
            const filtered = prev.filter(s => s.step !== msg.progress);
            return [...filtered, { step: msg.progress, status: 'done' }];
          });
          if (!qrModalOpen) {
            setQrCompanyName(msg.company_name);
            setQrModalOpen(true);
          }
        } else if (msg.type === 'keepalive_login_result') {
          if (msg.ok) {
            message.success(`${msg.company_name} 保活登录成功`);
          } else {
            message.warning(`${msg.company_name} 保活登录失败: ${msg.reason}，请尝试手动登录`);
          }
        } else if (msg.type === 'session_chunk') {
          // Support both formats: msg.text (node_agent.py plain text) and msg.event (stream-json)
          let text = (msg.text as string) || '';
          if (!text) {
            const event = msg.event || {};
            const etype = event.type || '';
            if (etype === 'assistant') {
              const blocks = event.message?.content || [];
              for (const b of blocks) {
                if (b.type === 'text' && b.text) text += b.text;
                else if (b.type === 'tool_use') text += `[调用工具: ${b.name}]`;
              }
            } else if (etype === 'user') {
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
          // Auto-show dialog
          if (!qrModalOpen && loginCompany) {
            setQrModalOpen(true);
            setLoginPath('session');
          }
        } else if (msg.type === 'session_created') {
          // A new session was created - auto-show the chat dialog
          handleAutoShowLogin(msg.company_name || loginCompany?.name || '', 'session');
        } else if (msg.type === 'session_marker') {
          if (msg.marker === 'QR_READY' && msg.image_base64) {
            setQrCompanyName(msg.payload || loginCompany?.name || '');
            setQrImage(`data:image/png;base64,${msg.image_base64}`);
            setQrModalOpen(true);
            setLoginPath('session');
          } else if (msg.marker === 'LOGIN_OK') {
            message.success(`${loginCompany?.name || ''} 登录成功`);
            fetch();
          } else if (msg.marker === 'LOGIN_FAILED') {
            message.error(`${loginCompany?.name || ''} 登录失败: ${msg.payload}`);
          }
        } else if (msg.type === 'session_done') {
          setChatMessages(prev => [...prev, { text: `[会话结束: ${msg.reason}]` }]);
        } else if (msg.type === 'session_error') {
          setChatMessages(prev => [...prev, { text: `[错误: ${msg.error}]` }]);
        }
      } catch { /* ignore */ }
    };
    return () => socket.close();
  }, []);

  // Handle login button click: reuse active session or show confirmation
  const handleLogin = (record: Company) => {
    // Already showing QR/chat modal for this company — just bring to front (modal is open)
    if (qrModalOpen && qrCompanyName === record.name) {
      return;
    }
    // Login already in progress for this company — reopen the modal directly
    if (pendingLoginCompanies.has(record.name)) {
      setLoginCompany(record);
      setQrCompanyName(record.name);
      setLoginConfirmOpen(false);
      setQrModalOpen(true);
      return;
    }
    // No active login — show confirmation first
    setLoginCompany(record);
    setLoginConfirmOpen(true);
  };

  // Auto-show login dialog when WS events indicate a session started for our company
  const handleAutoShowLogin = (companyName: string, path: string) => {
    if (loginCompany && loginCompany.name === companyName) {
      // Already in the flow for this company
      if (!qrModalOpen) {
        setQrModalOpen(true);
      }
      return;
    }
    // Find the company and start showing progress
    const c = data.find((x: Company) => x.name === companyName);
    if (c) {
      setLoginCompany(c);
      setLoginPath(path);
      setQrCompanyName(companyName);
      setQrModalOpen(true);
      setLoginConfirmOpen(false);
    }
  };

  // Step 2: Actually trigger login
  const startLogin = async () => {
    if (!loginCompany) return;
    setLoginLoading(true);
    setPendingLoginCompanies(prev => new Set(prev).add(loginCompany.name));
    try {
      const { data: result } = await api.post('/api/sync/trigger-login', { company_name: loginCompany.name, company_id: loginCompany.id });
      const serverMode = result.path || 'keepalive';
      setLoginConfirmOpen(false);
      setLoginPath(serverMode);
      setSessionId(result.session_id || '');
      setChatMessages([]);
      setKeepaliveSteps(serverMode === 'keepalive' ? [{ step: '已发送登录指令，等待响应...', status: 'process' }] : []);
      // Always open the streaming modal to show progress and wait for QR
      setQrImage('');
      setQrModalOpen(true);
    } catch (err: any) {
      message.error(err?.response?.data?.error || '触发登录失败');
    } finally {
      setLoginLoading(false);
    }
  };

  // Send chat message to active Claude session via API
  const sendChatMessage = async () => {
    if (!chatInput.trim() || !loginCompany) return;
    const text = chatInput.trim();
    setChatMessages(prev => [...prev, { text, isUser: true }]);
    setChatInput('');
    try {
      await api.post('/api/sync/session-message', {
        company_id: loginCompany.id,
        task_id: chatTaskId,
        session_id: chatSessionId,
        text,
      });
    } catch {
      setChatMessages(prev => [...prev, { text: '[发送失败]' }]);
    }
  };

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    setModalOpen(true);
  };

  const openEdit = (record: Company) => {
    setEditing(record);
    form.setFieldsValue(record);
    setModalOpen(true);
  };

  const handleSave = async () => {
    const values = await form.validateFields();
    if (editing) {
      await api.put(`/api/companies/${editing.id}`, values);
      message.success('更新成功');
    } else {
      await api.post('/api/companies', values);
      message.success('创建成功');
    }
    setModalOpen(false);
    fetch();
  };

  const handleDelete = async (id: number) => {
    await api.delete(`/api/companies/${id}`);
    message.success('删除成功');
    fetch();
  };

  // Query button click: check for active task first, then decide
  const handleQueryClick = async (record: Company) => {
    // Check if there's already an active task for this company
    try {
      const { data } = await api.get('/api/tasks', { params: { company_id: record.id, status: '进行中', pageSize: 1 } });
      if (data.data && data.data.length > 0) {
        // Active task exists — open session dialog directly
        const task = data.data[0];
        setLoginCompany(record);
        setLoginPath('session');
        setQrCompanyName(record.name);
        setChatTaskId(task.id);
        setChatSessionId(task.claude_session_id || '');
        setQrModalOpen(true);
        // Load session history
        if (task.claude_session_id) {
          try {
            const { data: history } = await api.get(`/api/sync/session-history-by-session/${task.claude_session_id}`);
            setChatMessages((history.data || []).map((m: any) => ({ text: m.content, isUser: m.role === 'user' })));
          } catch { /* ignore */ }
        }
        message.info(`已连接到任务 #${task.id} 的会话`);
        return;
      }
    } catch { /* fall through to confirmation */ }
    // No active task — show confirmation dialog
    setQueryCompany(record);
    setQueryConfirmOpen(true);
  };

  const handleStartQuery = async (record: Company) => {
    try {
      const { data: newTask } = await api.post('/api/tasks', { company_id: record.id });
      message.success('查询任务已创建并分发');
      // Open chat dialog for the new task
      setLoginCompany(record);
      setLoginPath('session');
      setQrCompanyName(record.name);
      setChatTaskId(newTask.id);
      setChatSessionId(newTask.claude_session_id || '');
      setChatMessages([]);
      setQrModalOpen(true);
    } catch (err: unknown) {
      const res = (err as { response?: { data?: { error?: string; message?: string; existingTask?: { id: number; claude_session_id?: string } } } })?.response?.data;
      if (res?.error === 'ACTIVE_TASK_EXISTS') {
        // Reuse existing session — open chat dialog with history
        const existingTask = res.existingTask!;
        setLoginCompany(record);
        setLoginPath('session');
        setQrCompanyName(record.name);
        setChatTaskId(existingTask.id);
        const sid = existingTask.claude_session_id || '';
        setChatSessionId(sid);
        setQrModalOpen(true);
        // Load session history
        if (sid) {
          try {
            const { data: history } = await api.get(`/api/sync/session-history-by-session/${sid}`);
            const msgs = (history.data || []).map((m: any) => ({
              text: m.content,
              isUser: m.role === 'user',
            }));
            setChatMessages(msgs);
          } catch { /* ignore */ }
        }
        message.info(`已连接到任务 #${existingTask.id} 的会话`);
      } else {
        message.error(res?.message || res?.error || '创建失败');
      }
    }
  };

  const columns = [
    { title: 'ID', dataIndex: 'id', width: 60 },
    { title: '公司名称', dataIndex: 'name', width: 260, ellipsis: true },
    { title: '简称', dataIndex: 'short_name', width: 120 },
    { title: '省份', dataIndex: 'province', width: 80 },
    {
      title: '账号状态', dataIndex: 'account_status', width: 90,
      render: (s: string) => <Tag color={s === 'online' ? 'green' : 'default'}>{s === 'online' ? '已登录' : '未登录'}</Tag>,
    },
    {
      title: '设备状态', width: 90,
      render: (_: unknown, record: Company) => {
        const binding = bindings[record.id];
        if (!binding) return <Tag color="default">未绑定</Tag>;
        const node = nodes.find(n => n.id === binding.node_id);
        const online = node?.status === 'online';
        return <Tag color={online ? 'green' : 'red'}>{online ? '在线' : '离线'}</Tag>;
      },
    },
    {
      title: '绑定设备', width: 200,
      render: (_: unknown, record: Company) => {
        const binding = bindings[record.id];
        return (
          <Select
            size="small"
            placeholder="选择设备"
            allowClear
            style={{ width: 160 }}
            value={binding?.node_id}
            onChange={async (nodeId) => {
              try {
                if (nodeId) {
                  await api.put(`/api/companies/${record.id}/bind`, { node_id: nodeId });
                  setBindings(prev => ({ ...prev, [record.id]: { node_id: nodeId, node_name: nodes.find(n => n.id === nodeId)?.node_name || '' } }));
                  message.success('绑定成功');
                } else {
                  await api.delete(`/api/companies/${record.id}/bind`);
                  setBindings(prev => { const next = { ...prev }; delete next[record.id]; return next; });
                  message.success('已解绑');
                }
              } catch { message.error('操作失败'); }
            }}
            options={nodes.map(n => ({
              label: n.display_name
                ? `${n.display_name} (${n.node_name})${n.status === 'online' ? ' ●在线' : ' ○离线'}`
                : `${n.node_name}${n.status === 'online' ? ' ●在线' : ' ○离线'}`,
              value: n.id,
            }))}
          />
        );
      },
    },
    { title: '联系人', dataIndex: 'contact_name', width: 100 },
    { title: '电话', dataIndex: 'contact_phone', width: 130 },
    {
      title: '操作', width: 280,
      render: (_: unknown, record: Company) => (
        <Space>
          <Button size="small" icon={<PlayCircleOutlined />} onClick={() => handleQueryClick(record)}>查询</Button>
          {record.account_status === 'offline' && (
            <Button size="small" icon={<QrcodeOutlined />} onClick={() => handleLogin(record)}>登录</Button>
          )}
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(record)} />
          <Popconfirm title="确定删除?" onConfirm={() => handleDelete(record.id)}>
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Select placeholder="筛选省份" allowClear style={{ width: 120 }} options={PROVINCES.map((p) => ({ label: p.label, value: p.value }))}
          value={provinceFilter} onChange={(v) => { setProvinceFilter(v); setPage(1); fetch(1, v, statusFilter); }} />
        <Select placeholder="筛选状态" allowClear style={{ width: 120 }}
          options={[{ label: '在线', value: 'online' }, { label: '离线', value: 'offline' }]}
          value={statusFilter} onChange={(v) => { setStatusFilter(v); setPage(1); fetch(1, provinceFilter, v); }} />
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增公司</Button>
      </Space>

      <Table rowKey="id" columns={columns} dataSource={data} loading={loading}
        pagination={{ current: page, total, pageSize: 20, onChange: (p) => { setPage(p); fetch(p); } }} />

      {/* Query confirmation modal */}
      <Modal
        title={`${queryCompany?.name || ''} - 开始违章查询`}
        open={queryConfirmOpen}
        onCancel={() => { setQueryConfirmOpen(false); setQueryCompany(null); }}
        footer={null}
        width={400}
      >
        <div style={{ textAlign: 'center', padding: '20px 0' }}>
          <p style={{ fontSize: 14, color: '#666', marginBottom: 20 }}>
            将为 <strong>{queryCompany?.name}</strong> 启动违章查询任务
          </p>
          <p style={{ fontSize: 12, color: '#999', marginBottom: 8 }}>
            系统将通过绑定的设备自动执行 12123 查询
          </p>
          {queryCompany && bindings[queryCompany.id] && (
            <p style={{ fontSize: 12, color: '#1677ff', marginBottom: 16 }}>
              设备：{bindings[queryCompany.id].node_name}
            </p>
          )}
          {queryCompany && !bindings[queryCompany.id] && (
            <p style={{ fontSize: 12, color: '#ff4d4f', marginBottom: 16 }}>
              ⚠️ 该公司未绑定设备，请先在控制台中绑定
            </p>
          )}
          <Button
            type="primary"
            size="large"
            icon={<PlayCircleOutlined />}
            loading={queryLoading}
            onClick={async () => {
              if (!queryCompany) return;
              setQueryLoading(true);
              setQueryConfirmOpen(false);
              try {
                await handleStartQuery(queryCompany);
              } finally {
                setQueryLoading(false);
              }
            }}
            block
          >
            开始查询
          </Button>
        </div>
      </Modal>

      {/* Step 1: Login confirm modal with "开始登录流程" button */}
      <Modal
        title={`${loginCompany?.name || ''} - 扫码登录`}
        open={loginConfirmOpen}
        onCancel={() => { setLoginConfirmOpen(false); setLoginCompany(null); }}
        footer={null}
        width={400}
      >
        <div style={{ textAlign: 'center', padding: '20px 0' }}>
          <p style={{ fontSize: 14, color: '#666', marginBottom: 20 }}>
            将为 <strong>{loginCompany?.name}</strong> 启动扫码登录流程
          </p>
          <p style={{ fontSize: 12, color: '#999', marginBottom: 24 }}>
            点击下方按钮后，系统将通过绑定的 skill 设备打开登录页面并生成二维码
          </p>
          <Button
            type="primary"
            size="large"
            icon={<QrcodeOutlined />}
            loading={loginLoading}
            onClick={startLogin}
            block
          >
            开始登录流程
          </Button>
        </div>
      </Modal>

      {/* Step 2: Login dialog — QR (keepalive) or Chat (session) */}
      <Modal
        title={loginPath === 'session'
          ? `${qrCompanyName || loginCompany?.name} - 查询对话${chatTaskId ? ` (任务 #${chatTaskId})` : ''}`
          : `${qrCompanyName} - 扫码登录`}
        open={qrModalOpen}
        onCancel={() => { setQrModalOpen(false); setQrImage(''); setLoginPath(''); setChatMessages([]); setKeepaliveSteps([]); setChatTaskId(null); setChatSessionId(''); }}
        footer={null}
        width={loginPath === 'session' ? 500 : 400}
      >
        {loginPath === 'session' ? (
          // ── Session chat dialog ──
          <div>
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
          </div>
        ) : (
          // ── Keepalive QR display ──
          <div>
            {keepaliveSteps.length > 0 && (
              <Steps direction="vertical" size="small" current={keepaliveSteps.length - 1}
                items={keepaliveSteps.map(s => ({ title: s.step, status: s.status === 'done' ? 'finish' : 'process' } as any))}
                style={{ marginBottom: 16 }} />
            )}
            {qrImage ? (
              <div style={{ textAlign: 'center' }}>
                <Image src={qrImage} alt="二维码" style={{ maxWidth: 300, cursor: 'zoom-in' }} />
                <p style={{ marginTop: 12, color: '#666' }}>请用 12123 App 扫描二维码登录</p>
              </div>
            ) : (
              <div style={{ textAlign: 'center', padding: 40, color: '#999' }}>等待二维码生成...</div>
            )}
          </div>
        )}
      </Modal>

      <Modal title={editing ? '编辑公司' : '新增公司'} open={modalOpen} onOk={handleSave} onCancel={() => setModalOpen(false)} width={600}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="公司名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="short_name" label="简称">
            <Input />
          </Form.Item>
          <Form.Item name="province" label="省份" rules={[{ required: true }]}>
            <Select options={PROVINCES} />
          </Form.Item>
          <Form.Item name="contact_name" label="联系人" rules={[{ required: true, message: '请输入联系人' }]}>
            <Input />
          </Form.Item>
          <Form.Item name="contact_phone" label="联系电话" rules={[{ required: true, message: '请输入联系电话' }]}>
            <Input />
          </Form.Item>
          <Form.Item name="notify_chat_name" label="通知群名称" help="扫码通知发送到群，并@联系人；留空则发送给联系人个人">
            <Input placeholder="例如：违章通知群" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

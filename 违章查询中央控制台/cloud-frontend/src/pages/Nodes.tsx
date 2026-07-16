import { useEffect, useState } from 'react';
import { Card, Row, Col, Tag, Slider, InputNumber, Space, message, Button, Modal, Form, Input, Popconfirm, Descriptions, Progress, Table } from 'antd';
import { DesktopOutlined, PlusOutlined, DeleteOutlined, DashboardOutlined } from '@ant-design/icons';
import api from '../api';

interface Node {
  id: number;
  node_name: string;
  display_name: string;
  hostname: string | null;
  status: string;
  max_concurrency: number;
  active_sessions: number;
  memory_total_gb: number | null;
  cpu_cores: number | null;
  last_heartbeat: string | null;
  last_sync_at: string | null;
}

interface RuntimeInfo {
  node_name: string;
  hostname: string;
  status: string;
  active_sessions: number;
  cpu_percent: number;
  cpu_count: number;
  memory_total_gb: number;
  memory_used_gb: number;
  memory_percent: number;
  disk_total_gb: number;
  disk_used_gb: number;
  disk_percent: number;
  net_bytes_sent_mb: number;
  net_bytes_recv_mb: number;
  uptime_seconds: number;
  process_count: number;
  processes: Array<{ name: string; description: string; pid: number; cpu_percent: number; mem_percent: number; rss_mb: number; command: string }> | null;
  last_heartbeat: string;
  last_sync_at: string;
}

export default function Nodes() {
  const [nodes, setNodes] = useState<Node[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  const [runtimeModalOpen, setRuntimeModalOpen] = useState(false);
  const [runtimeInfo, setRuntimeInfo] = useState<RuntimeInfo | null>(null);
  const [runtimeLoading, setRuntimeLoading] = useState(false);
  const [runtimeNodeName, setRuntimeNodeName] = useState('');

  const fetch = () => api.get('/api/nodes').then(({ data }) => setNodes(data.data));

  useEffect(() => { fetch(); const t = setInterval(fetch, 10000); return () => clearInterval(t); }, []);

  const updateConcurrency = async (node: Node, val: number) => {
    await api.patch(`/api/nodes/${node.id}`, { max_concurrency: val });
    message.success('已更新');
    fetch();
  };

  const handleCreate = async () => {
    const values = await form.validateFields();
    await api.post('/api/nodes', values);
    message.success(`设备 "${values.node_name}" 已创建。部署时请将此 ID 填入 node_agent 配置。`);
    setModalOpen(false);
    form.resetFields();
    fetch();
  };

  const handleDelete = async (node: Node) => {
    await api.delete(`/api/nodes/${node.id}`);
    message.success(`设备 "${node.display_name || node.node_name}" 已删除`);
    fetch();
  };

  const showRuntime = async (node: Node) => {
    setRuntimeNodeName(node.display_name || node.node_name);
    setRuntimeModalOpen(true);
    setRuntimeLoading(true);
    try {
      const { data } = await api.get(`/api/nodes/${node.id}/runtime`);
      setRuntimeInfo(data);
    } catch {
      setRuntimeInfo(null);
    } finally {
      setRuntimeLoading(false);
    }
  };

  const formatUptime = (seconds: number) => {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d > 0) return `${d}天 ${h}小时 ${m}分钟`;
    if (h > 0) return `${h}小时 ${m}分钟`;
    return `${m}分钟`;
  };

  const formatBytes = (mb: number | string) => {
    const n = Number(mb);
    if (n >= 1024) return `${(n / 1024).toFixed(2)} GB`;
    return `${n.toFixed(1)} MB`;
  };

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
          新增设备
        </Button>
      </div>
      <Row gutter={[16, 16]}>
        {nodes.map((node) => (
          <Col key={node.id} xs={24} sm={12} lg={8}>
            <Card
              title={<span><DesktopOutlined /> {node.display_name || node.node_name}</span>}
              extra={
              <Space>
                <Tag color={node.status === 'online' ? 'green' : 'red'}>{node.status === 'online' ? '在线' : '离线'}</Tag>
                <Popconfirm
                  title="确定删除此设备？"
                  description="已绑定该设备的公司将被自动解绑"
                  onConfirm={() => handleDelete(node)}
                  okText="确定删除"
                  cancelText="取消"
                  okType="danger"
                >
                  <Button size="small" danger icon={<DeleteOutlined />} />
                </Popconfirm>
              </Space>
            }
            >
              <div style={{ marginBottom: 4, fontSize: 12, color: '#666' }}>
                设备ID: <strong>{node.node_name}</strong>
              </div>
              <div style={{ marginBottom: 4, fontSize: 12, color: '#999' }}>
                {node.hostname ? `hostname: ${node.hostname}` : '等待首次连接'}
              </div>
              <div style={{ marginBottom: 8 }}>
                <Button size="small" icon={<DashboardOutlined />} onClick={() => showRuntime(node)}>
                  运行情况
                </Button>
              </div>
              <div style={{ marginBottom: 8 }}>活跃进程: <strong>{node.active_sessions} / {node.max_concurrency}</strong></div>
              <div style={{ marginBottom: 8 }}>内存: {node.memory_total_gb ?? '-'}GB | CPU: {node.cpu_cores ?? '-'} 核</div>
              <div style={{ marginBottom: 4, fontSize: 12, color: '#999' }}>
                最后心跳: {node.last_heartbeat ? new Date(node.last_heartbeat).toLocaleString() : 'N/A'}
              </div>
              <div style={{ marginBottom: 12, fontSize: 12, color: '#999' }}>
                最后同步: {node.last_sync_at ? new Date(node.last_sync_at).toLocaleString() : 'N/A'}
              </div>
              <div>
                <span style={{ fontSize: 12 }}>并发上限:</span>
                <Space>
                  <Slider style={{ width: 120 }} min={1} max={30} value={node.max_concurrency}
                    onChange={(v) => updateConcurrency(node, v)} />
                  <InputNumber size="small" min={1} max={30} value={node.max_concurrency}
                    onChange={(v) => v && updateConcurrency(node, v)} style={{ width: 60 }} />
                </Space>
              </div>
            </Card>
          </Col>
        ))}
        {nodes.length === 0 && (
          <Col span={24}><div style={{ textAlign: 'center', padding: 60, color: '#999' }}>暂无注册设备，请点击「新增设备」预注册</div></Col>
        )}
      </Row>

      <Modal
        title={`${runtimeNodeName} - 运行情况`}
        open={runtimeModalOpen}
        onCancel={() => { setRuntimeModalOpen(false); setRuntimeInfo(null); }}
        footer={<Button onClick={() => setRuntimeModalOpen(false)}>关闭</Button>}
        width={850}
        loading={runtimeLoading}
        style={{ top: 20 }}
      >
        {runtimeInfo ? (
          <div>
            <Descriptions column={2} size="small" bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label="设备ID">{runtimeInfo.node_name}</Descriptions.Item>
              <Descriptions.Item label="主机名">{runtimeInfo.hostname || '-'}</Descriptions.Item>
              <Descriptions.Item label="状态">
                <Tag color={runtimeInfo.status === 'online' ? 'green' : 'red'}>
                  {runtimeInfo.status === 'online' ? '在线' : '离线'}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="活跃会话">{runtimeInfo.active_sessions}</Descriptions.Item>
              <Descriptions.Item label="进程数">{runtimeInfo.process_count}</Descriptions.Item>
              <Descriptions.Item label="运行时长">{formatUptime(runtimeInfo.uptime_seconds)}</Descriptions.Item>
            </Descriptions>

            <Descriptions column={1} size="small" bordered style={{ marginBottom: 16 }} title="系统资源">
              <Descriptions.Item label="CPU">
                <div>
                  <Progress percent={Math.round(Number(runtimeInfo.cpu_percent))} size="small"
                    format={() => `${Number(runtimeInfo.cpu_percent).toFixed(1)}%`} />
                  <span style={{ fontSize: 12, color: '#999' }}>{runtimeInfo.cpu_count} 核心</span>
                </div>
              </Descriptions.Item>
              <Descriptions.Item label="内存">
                <div>
                  <Progress percent={Math.round(Number(runtimeInfo.memory_percent))} size="small"
                    format={() => `${Number(runtimeInfo.memory_used_gb).toFixed(2)} / ${Number(runtimeInfo.memory_total_gb).toFixed(2)} GB`} />
                </div>
              </Descriptions.Item>
              <Descriptions.Item label="磁盘">
                <div>
                  <Progress percent={Math.round(Number(runtimeInfo.disk_percent))} size="small"
                    format={() => `${Number(runtimeInfo.disk_used_gb).toFixed(2)} / ${Number(runtimeInfo.disk_total_gb).toFixed(2)} GB`} />
                </div>
              </Descriptions.Item>
            </Descriptions>

            <Descriptions column={2} size="small" bordered style={{ marginBottom: 16 }} title="网络">
              <Descriptions.Item label="发送">{formatBytes(runtimeInfo.net_bytes_sent_mb)}</Descriptions.Item>
              <Descriptions.Item label="接收">{formatBytes(runtimeInfo.net_bytes_recv_mb)}</Descriptions.Item>
            </Descriptions>

            <Descriptions column={2} size="small" bordered title="时间戳">
              <Descriptions.Item label="最后心跳">
                {runtimeInfo.last_heartbeat ? new Date(runtimeInfo.last_heartbeat).toLocaleString() : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="最后同步">
                {runtimeInfo.last_sync_at ? new Date(runtimeInfo.last_sync_at).toLocaleString() : '-'}
              </Descriptions.Item>
            </Descriptions>

            {runtimeInfo.processes && runtimeInfo.processes.length > 0 && (
              <div style={{ marginTop: 16 }}>
                <div style={{ fontWeight: 500, marginBottom: 8, fontSize: 14 }}>
                  全部进程 ({runtimeInfo.processes.length})
                </div>
                <Table
                  dataSource={runtimeInfo.processes.map((p: any, i: number) => ({ ...p, key: i }))}
                  columns={[
                    { title: '进程', dataIndex: 'name', width: 100 },
                    { title: '说明', dataIndex: 'description', width: 160, ellipsis: true,
                      render: (v: string) => v ? <span style={{color:'#888',fontSize:12}}>{v}</span> : null },
                    { title: 'PID', dataIndex: 'pid', width: 55 },
                    { title: 'CPU%', dataIndex: 'cpu_percent', width: 55, render: (v: any) => Number(v).toFixed(1) },
                    { title: 'MEM%', dataIndex: 'mem_percent', width: 55, render: (v: any) => Number(v).toFixed(1) },
                    { title: 'RSS', dataIndex: 'rss_mb', width: 65, render: (v: any) => Number(v).toFixed(0) + ' MB' },
                    { title: '命令', dataIndex: 'command', ellipsis: true, width: 250 },
                  ]}
                  size="small"
                  pagination={false}
                  scroll={{ x: 740 }}
                />
              </div>
            )}
          </div>
        ) : (
          <div style={{ textAlign: 'center', padding: 40, color: '#999' }}>无法获取运行数据</div>
        )}
      </Modal>

      <Modal
        title="新增设备"
        open={modalOpen}
        onOk={handleCreate}
        onCancel={() => { setModalOpen(false); form.resetFields(); }}
        width={450}
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="node_name"
            label="设备ID"
            rules={[{ required: true, message: '请输入设备ID' }]}
            extra="与部署 node_agent 时配置的设备 ID 保持一致，用于 skill 与控制台的通信匹配"
          >
            <Input placeholder="如: chengdu-server-01" />
          </Form.Item>
          <Form.Item
            name="display_name"
            label="设备名称"
            extra="便于识别的显示名称，如不填则显示设备ID"
          >
            <Input placeholder="如: 成都-服务器01" />
          </Form.Item>
          <Form.Item
            name="max_concurrency"
            label="并发上限"
            initialValue={15}
          >
            <InputNumber min={1} max={30} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

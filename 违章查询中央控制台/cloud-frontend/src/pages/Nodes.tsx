import { useEffect, useState } from 'react';
import { Card, Row, Col, Tag, Slider, InputNumber, Space, message, Button, Modal, Form, Input, Popconfirm } from 'antd';
import { DesktopOutlined, PlusOutlined, DeleteOutlined } from '@ant-design/icons';
import api from '../api';

interface Node {
  id: number;
  node_name: string;
  hostname: string | null;
  status: string;
  max_concurrency: number;
  active_sessions: number;
  memory_total_gb: number | null;
  cpu_cores: number | null;
  last_heartbeat: string | null;
  last_sync_at: string | null;
}

export default function Nodes() {
  const [nodes, setNodes] = useState<Node[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

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
    message.success(`设备 "${values.node_name}" 已创建。部署时请将此名称填入 node_agent 配置。`);
    setModalOpen(false);
    form.resetFields();
    fetch();
  };

  const handleDelete = async (node: Node) => {
    await api.delete(`/api/nodes/${node.id}`);
    message.success(`设备 "${node.node_name}" 已删除`);
    fetch();
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
              title={<span><DesktopOutlined /> {node.node_name}</span>}
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
              <div style={{ marginBottom: 4, fontSize: 12, color: '#999' }}>
                {node.hostname ? `hostname: ${node.hostname}` : '等待首次连接'}
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
        title="新增设备"
        open={modalOpen}
        onOk={handleCreate}
        onCancel={() => { setModalOpen(false); form.resetFields(); }}
        width={450}
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="node_name"
            label="设备名称"
            rules={[{ required: true, message: '请输入设备名称' }]}
            extra="与控制台「设备管理」中预注册的名称一致，部署 node_agent 时需填入相同的设备 ID"
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

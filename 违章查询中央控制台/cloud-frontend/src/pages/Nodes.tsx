import { useEffect, useState } from 'react';
import { Card, Row, Col, Tag, Slider, InputNumber, Space, message } from 'antd';
import { CloudServerOutlined, DesktopOutlined } from '@ant-design/icons';
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
}

export default function Nodes() {
  const [nodes, setNodes] = useState<Node[]>([]);

  const fetch = () => api.get('/api/nodes').then(({ data }) => setNodes(data.data));

  useEffect(() => { fetch(); const t = setInterval(fetch, 10000); return () => clearInterval(t); }, []);

  const updateConcurrency = async (node: Node, val: number) => {
    await api.patch(`/api/nodes/${node.id}`, { max_concurrency: val });
    message.success('已更新');
    fetch();
  };

  return (
    <Row gutter={[16, 16]}>
      {nodes.map((node) => (
        <Col key={node.id} xs={24} sm={12} lg={8}>
          <Card
            title={<span><DesktopOutlined /> {node.node_name}</span>}
            extra={<Tag color={node.status === 'online' ? 'green' : 'red'}>{node.status === 'online' ? '在线' : '离线'}</Tag>}
          >
            <div style={{ marginBottom: 8 }}>活跃进程: <strong>{node.active_sessions} / {node.max_concurrency}</strong></div>
            <div style={{ marginBottom: 8 }}>内存: {node.memory_total_gb}GB | CPU: {node.cpu_cores} 核</div>
            <div style={{ marginBottom: 12, fontSize: 12, color: '#999' }}>
              最后心跳: {node.last_heartbeat ? new Date(node.last_heartbeat).toLocaleString() : 'N/A'}
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
        <Col span={24}><div style={{ textAlign: 'center', padding: 60, color: '#999' }}>暂无注册设备</div></Col>
      )}
    </Row>
  );
}

import { useEffect, useState } from 'react';
import { Card, Row, Col, Statistic, Tag } from 'antd';
import { BankOutlined, SyncOutlined, WarningOutlined, CheckCircleOutlined, CloudServerOutlined, CarOutlined } from '@ant-design/icons';
import api from '../api';

interface Overview {
  total_companies: number;
  active_jobs: number;
  violations_today: number;
  completed_jobs: number;
  online_nodes: number;
  total_vehicles: number;
  total_violations: number;
  unprocessed_violations: number;
}

export default function Dashboard() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [activeTasks, setActiveTasks] = useState<unknown[]>([]);

  useEffect(() => {
    api.get('/api/dashboard/overview').then(({ data }) => setOverview(data));
    api.get('/api/tasks', { params: { status: '进行中', pageSize: 20 } }).then(({ data }) => setActiveTasks(data.data));
  }, []);

  return (
    <div>
      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} md={6}>
          <Card><Statistic title="公司总数" value={overview?.total_companies || 0} prefix={<BankOutlined />} /></Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card><Statistic title="活跃任务" value={overview?.active_jobs || 0} prefix={<SyncOutlined spin />} valueStyle={{ color: '#1677ff' }} /></Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card><Statistic title="车辆总数" value={overview?.total_vehicles || 0} prefix={<CarOutlined />} /></Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card><Statistic title="在线节点" value={overview?.online_nodes || 0} prefix={<CloudServerOutlined />} /></Card>
        </Col>
      </Row>
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} sm={12} md={6}>
          <Card><Statistic title="违章总数" value={overview?.total_violations || 0} prefix={<WarningOutlined />} /></Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card><Statistic title="未处理违章" value={overview?.unprocessed_violations || 0} prefix={<WarningOutlined />} valueStyle={{ color: '#cf1322' }} /></Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card><Statistic title="今日新增" value={overview?.violations_today || 0} prefix={<WarningOutlined />} valueStyle={{ color: '#faad14' }} /></Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card><Statistic title="已完成任务" value={overview?.completed_jobs || 0} prefix={<CheckCircleOutlined />} valueStyle={{ color: '#52c41a' }} /></Card>
        </Col>
      </Row>

      <Card title="活跃任务" style={{ marginTop: 24 }}>
        {activeTasks.map((task: Record<string, unknown>) => (
          <Card.Grid key={task.id as number} style={{ width: '33.33%', padding: 16 }}>
            <div style={{ fontWeight: 'bold', marginBottom: 8 }}>{task.company_name as string}</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <Tag color="processing">{task.progress as string}</Tag>
              <span style={{ fontSize: 12, color: '#999' }}>{(task.processed_vehicles as number) || 0}/{(task.total_vehicles as number) || '?'} 台</span>
            </div>
            <div style={{ background: '#f5f5f5', borderRadius: 4, height: 6, overflow: 'hidden' }}>
              <div style={{ background: '#1677ff', height: '100%', width: `${(task.total_vehicles as number) > 0 ? ((task.processed_vehicles as number) / (task.total_vehicles as number)) * 100 : 0}%`, transition: 'width 0.3s' }} />
            </div>
            <div style={{ fontSize: 12, color: '#666', marginTop: 4 }}>
              {task.progress_desc as string || '等待中...'}
            </div>
          </Card.Grid>
        ))}
        {activeTasks.length === 0 && <div style={{ textAlign: 'center', padding: 40, color: '#999' }}>暂无活跃任务</div>}
      </Card>
    </div>
  );
}

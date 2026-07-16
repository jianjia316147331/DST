import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { Table, Card, Typography, Tag, Button, Space } from 'antd';
import { ArrowLeftOutlined } from '@ant-design/icons';
import api from '../api';

const { Title } = Typography;

interface Violation {
  id: number;
  plate_number: string;
  violation_time: string;
  violation_location: string;
  violation_behavior: string;
  fine_amount: number;
  points: number;
  handling_status: string;
  payment_status: string;
}

interface VehicleInfo {
  id: number;
  plate_number: string;
  company_name: string;
  plate_type_label: string;
  status_label: string;
}

export default function VehicleViolations() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [vehicle, setVehicle] = useState<VehicleInfo | null>(null);
  const [violations, setViolations] = useState<Violation[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    (async () => {
      if (!id) return;
      try {
        const [vRes, vioRes] = await Promise.all([
          api.get(`/api/vehicles/${id}`),
          api.get('/api/violations', { params: { plate_number: '', company_id: '', pageSize: 200 } }),
        ]);
        setVehicle(vRes.data);
        // Filter violations for this vehicle
        const plate = vRes.data?.plate_number;
        if (plate) {
          const { data: vData } = await api.get('/api/violations', {
            params: { plate_number: plate, pageSize: 200 },
          });
          setViolations(vData.data || []);
        }
      } catch {
        // ignore
      }
    })();
  }, [id]);

  const columns = [
    { title: '违章时间', dataIndex: 'violation_time', key: 'violation_time',
      render: (t: string) => t ? new Date(t).toLocaleString('zh-CN') : '-',
    },
    { title: '违章地点', dataIndex: 'violation_location', key: 'violation_location', ellipsis: true },
    { title: '违章行为', dataIndex: 'violation_behavior', key: 'violation_behavior', ellipsis: true },
    { title: '罚款(元)', dataIndex: 'fine_amount', key: 'fine_amount' },
    { title: '扣分', dataIndex: 'points', key: 'points' },
    { title: '处理状态', dataIndex: 'handling_status', key: 'handling_status',
      render: (text: string) => <Tag>{text || '-'}</Tag>,
    },
    { title: '缴费状态', dataIndex: 'payment_status', key: 'payment_status',
      render: (text: string) => <Tag color={text === '已缴款' ? 'green' : 'orange'}>{text || '-'}</Tag>,
    },
  ];

  return (
    <>
      <Space style={{ marginBottom: 16 }}>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/vehicles')}>返回</Button>
      </Space>
      <Title level={4}>
        {vehicle?.plate_number || '车辆'} 违章记录
        {vehicle?.company_name && <span style={{ fontSize: 14, color: '#999', marginLeft: 12 }}>{vehicle.company_name}</span>}
      </Title>
      <Table rowKey="id" dataSource={violations} columns={columns} loading={loading}
        pagination={{ pageSize: 50, showSizeChanger: true }} />
    </>
  );
}

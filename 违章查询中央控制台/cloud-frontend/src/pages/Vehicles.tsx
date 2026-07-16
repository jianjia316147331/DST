import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Table, Card, Input, Select, Space, Typography, Tag } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import api from '../api';

const { Title } = Typography;

interface Vehicle {
  id: number;
  company_id: number;
  company_name: string;
  plate_number: string;
  plate_type_label: string;
  status_label: string;
  unprocessed_count: number;
  inspection_date: string;
  tag_batch_id: string;
  updated_at: string;
}

export default function Vehicles() {
  const [data, setData] = useState<Vehicle[]>([]);
  const [loading, setLoading] = useState(false);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [search, setSearch] = useState('');
  const navigate = useNavigate();

  const fetchData = async () => {
    setLoading(true);
    try {
      const { data: res } = await api.get('/api/vehicles', {
        params: { page, pageSize, plate_number: search || undefined },
      });
      setData(res.data);
      setTotal(res.total);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchData(); }, [page, pageSize]);

  const columns = [
    { title: '车牌号', dataIndex: 'plate_number', key: 'plate_number',
      render: (text: string, record: Vehicle) => (
        <a onClick={() => navigate(`/vehicles/${record.id}`)}>{text}</a>
      ),
    },
    { title: '公司', dataIndex: 'company_name', key: 'company_name' },
    { title: '车辆类型', dataIndex: 'plate_type_label', key: 'plate_type_label' },
    { title: '状态', dataIndex: 'status_label', key: 'status_label',
      render: (text: string) => <Tag>{text || '-'}</Tag>,
    },
    { title: '未处理违章', dataIndex: 'unprocessed_count', key: 'unprocessed_count',
      render: (count: number) => count > 0 ? <Tag color="red">{count}</Tag> : <Tag color="green">0</Tag>,
    },
    { title: '批次', dataIndex: 'tag_batch_id', key: 'tag_batch_id', ellipsis: true },
    { title: '最近更新', dataIndex: 'updated_at', key: 'updated_at',
      render: (t: string) => t ? new Date(t).toLocaleString('zh-CN') : '-',
    },
  ];

  return (
    <>
      <Title level={4} style={{ marginBottom: 16 }}>车辆管理</Title>
      <Space style={{ marginBottom: 16 }}>
        <Input
          prefix={<SearchOutlined />}
          placeholder="搜索车牌号"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onPressEnter={fetchData}
          allowClear
        />
      </Space>
      <Table
        rowKey="id"
        dataSource={data}
        columns={columns}
        loading={loading}
        pagination={{ current: page, pageSize, total, onChange: (p, ps) => { setPage(p); setPageSize(ps); }, showSizeChanger: true }}
      />
    </>
  );
}

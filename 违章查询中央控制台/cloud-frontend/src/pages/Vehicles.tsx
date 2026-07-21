import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Table, Button, Input, Select, Space, Typography, Tag } from 'antd';
import { SearchOutlined, ClearOutlined } from '@ant-design/icons';
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
  tag: string;
  tag_batch_id: string;
  last_query_time: string;
  updated_at: string;
}

interface Company {
  id: number;
  name: string;
}

export default function Vehicles() {
  const [data, setData] = useState<Vehicle[]>([]);
  const [loading, setLoading] = useState(false);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [search, setSearch] = useState('');
  const [companyId, setCompanyId] = useState<number | undefined>();
  const [statusLabel, setStatusLabel] = useState<string | undefined>();
  const [companies, setCompanies] = useState<Company[]>([]);
  const navigate = useNavigate();

  // Fetch company list for filter dropdown
  useEffect(() => {
    api.get('/api/companies', { params: { pageSize: 200 } }).then(({ data: res }) => {
      setCompanies(res.data || []);
    }).catch(() => {});
  }, []);

  const fetchData = async () => {
    setLoading(true);
    try {
      const { data: res } = await api.get('/api/vehicles', {
        params: {
          page, pageSize,
          plate_number: search || undefined,
          company_id: companyId || undefined,
          status_label: statusLabel || undefined,
        },
      });
      setData(res.data);
      setTotal(res.total);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchData(); }, [page, pageSize, companyId, statusLabel]);

  const columns = [
    { title: '车牌号', dataIndex: 'plate_number', key: 'plate_number',
      render: (text: string, record: Vehicle) => (
        <a onClick={() => navigate(`/vehicles/${record.id}`)}>{text}</a>
      ),
    },
    { title: '标签', dataIndex: 'tag', key: 'tag',
      render: (text: string) => text ? <Tag>{text}</Tag> : '-',
    },
    { title: '公司', dataIndex: 'company_name', key: 'company_name', width: 220, ellipsis: true },
    { title: '车辆类型', dataIndex: 'plate_type_label', key: 'plate_type_label' },
    { title: '状态', dataIndex: 'status_label', key: 'status_label',
      render: (text: string) => <Tag>{text || '-'}</Tag>,
    },
    { title: '未处理违章', dataIndex: 'unprocessed_count', key: 'unprocessed_count',
      render: (count: number) => count > 0 ? <Tag color="red">{count}</Tag> : <Tag color="green">0</Tag>,
    },
    { title: '批次', dataIndex: 'tag_batch_id', key: 'tag_batch_id', ellipsis: true,
      render: (text: string) => text || '-',
    },
    { title: '查询时间', dataIndex: 'last_query_time', key: 'last_query_time',
      render: (t: string) => t ? new Date(t).toLocaleString('zh-CN') : '-',
    },
    { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at',
      render: (t: string) => t ? new Date(t).toLocaleString('zh-CN') : '-',
    },
  ];

  return (
    <>
      <Title level={4} style={{ marginBottom: 16 }}>车辆管理</Title>
      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          prefix={<SearchOutlined />}
          placeholder="搜索车牌号"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onPressEnter={fetchData}
          allowClear
          style={{ width: 160 }}
        />
        <Select
          placeholder="选择公司"
          value={companyId}
          onChange={(v) => { setCompanyId(v); setPage(1); }}
          allowClear
          style={{ width: 200 }}
          options={companies.map((c) => ({ label: c.name, value: c.id }))}
          filterOption={(input, option) => (option?.label as string || '').includes(input)}
        />
        <Select
          placeholder="车辆状态"
          value={statusLabel}
          onChange={(v) => { setStatusLabel(v); setPage(1); }}
          allowClear
          style={{ width: 140 }}
          options={[
            { label: '正常', value: '正常' },
            { label: '违法未处理', value: '违法未处理' },
            { label: '逾期未检验', value: '逾期未检验' },
            { label: '锁定', value: '锁定' },
            { label: '转出', value: '转出' },
            { label: '注销', value: '注销' },
          ]}
        />
        <Button
          icon={<ClearOutlined />}
          onClick={() => { setSearch(''); setCompanyId(undefined); setStatusLabel(undefined); setPage(1); }}
        >
          清空
        </Button>
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

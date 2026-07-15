import { useEffect, useState } from 'react';
import { Table, Button, Modal, Form, Select, Input, Space, Switch, message, Popconfirm } from 'antd';
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons';
import api from '../api';

export default function Schedules() {
  const [data, setData] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [companies, setCompanies] = useState<{ id: number; name: string }[]>([]);
  const [form] = Form.useForm();

  const fetch = () => {
    setLoading(true);
    api.get('/api/schedules').then(({ data: d }) => setData(d.data)).finally(() => setLoading(false));
  };

  useEffect(() => {
    fetch();
    api.get('/api/companies', { params: { pageSize: 500 } }).then(({ data: d }) => setCompanies(d.data));
  }, []);

  const handleCreate = async () => {
    const values = await form.validateFields();
    await api.post('/api/schedules', values);
    message.success('创建成功');
    setModalOpen(false);
    form.resetFields();
    fetch();
  };

  const columns = [
    { title: '公司', dataIndex: 'company_name', width: 200 },
    { title: 'Cron 表达式', dataIndex: 'cron_expression', width: 150 },
    { title: '下次执行', dataIndex: 'next_run_at', width: 180, render: (v: string) => v ? new Date(v).toLocaleString() : '-' },
    { title: '上次执行', dataIndex: 'last_triggered_at', width: 180, render: (v: string) => v ? new Date(v).toLocaleString() : '-' },
    {
      title: '启用', dataIndex: 'enabled', width: 80,
      render: (v: boolean, record: Record<string, unknown>) => (
        <Switch checked={v} onChange={(checked) => api.put(`/api/schedules/${record.id}`, { enabled: checked }).then(fetch)} />
      ),
    },
    {
      title: '操作', width: 80,
      render: (_: unknown, record: Record<string, unknown>) => (
        <Popconfirm title="确定删除?" onConfirm={() => api.delete(`/api/schedules/${record.id}`).then(fetch)}>
          <Button size="small" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ];

  return (
    <div>
      <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)} style={{ marginBottom: 16 }}>新增计划</Button>
      <Table rowKey="id" columns={columns} dataSource={data} loading={loading} />

      <Modal title="新增定时计划" open={modalOpen} onOk={handleCreate} onCancel={() => setModalOpen(false)}>
        <Form form={form} layout="vertical">
          <Form.Item name="company_id" label="公司" rules={[{ required: true }]}>
            <Select showSearch optionFilterProp="label" options={companies.map((c) => ({ label: c.name, value: c.id }))} />
          </Form.Item>
          <Form.Item name="cron_expression" label="Cron 表达式" rules={[{ required: true }]}
            extra="如: 0 2 * * 1 (每周一凌晨2点)">
            <Input placeholder="0 2 * * 1" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

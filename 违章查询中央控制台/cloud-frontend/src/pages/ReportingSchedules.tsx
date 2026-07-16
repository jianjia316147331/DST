import { useEffect, useState } from 'react';
import { Table, Button, Modal, Form, Select, Space, Switch, message, Popconfirm, Tag } from 'antd';
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons';
import api from '../api';

const FREQUENCIES = [
  { label: '每小时', value: 'hourly' },
  { label: '每 3 小时', value: 'every_3h' },
  { label: '每 6 小时', value: 'every_6h' },
  { label: '每 8 小时', value: 'every_8h' },
  { label: '每 12 小时', value: 'every_12h' },
  { label: '每天 1 次 (08:00)', value: 'daily' },
  { label: '自定义时间', value: 'custom' },
];

const FREQ_TAGS: Record<string, string> = {
  hourly: 'blue', every_3h: 'geekblue', every_6h: 'cyan',
  every_8h: 'green', every_12h: 'orange', daily: 'purple', custom: 'default',
};

export default function ReportingSchedules() {
  const [data, setData] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<any>(null);
  const [nodes, setNodes] = useState<any[]>([]);
  const [form] = Form.useForm();
  const [frequency, setFrequency] = useState('daily');

  const fetch = () => {
    setLoading(true);
    api.get('/api/reporting-schedules').then(({ data: d }) => setData(d.data)).finally(() => setLoading(false));
  };

  useEffect(() => {
    fetch();
    api.get('/api/nodes').then(({ data: d }) => setNodes(d.data || []));
  }, []);

  const handleSave = async () => {
    const values = await form.validateFields();
    const body: any = { node_id: values.node_id, enabled: values.enabled !== false };
    if (values.frequency === 'custom') {
      body.frequency = 'custom';
      body.times = (values.custom_times || '').split(',').map((s: string) => s.trim()).filter(Boolean);
      if (body.times.length === 0) {
        message.error('自定义时间不能为空');
        return;
      }
    } else {
      body.frequency = values.frequency || 'daily';
    }
    if (editing) {
      await api.put(`/api/reporting-schedules/${editing.id}`, body);
      message.success('已更新');
    } else {
      await api.post('/api/reporting-schedules', body);
      message.success('已创建');
    }
    setModalOpen(false);
    form.resetFields();
    setEditing(null);
    setFrequency('daily');
    fetch();
  };

  const openCreate = () => {
    setEditing(null);
    setFrequency('daily');
    form.resetFields();
    form.setFieldsValue({ frequency: 'daily', enabled: true });
    setModalOpen(true);
  };

  const openEdit = (record: any) => {
    setEditing(record);
    const freq = record.frequency || 'custom';
    setFrequency(freq);
    form.setFieldsValue({
      node_id: record.node_id,
      frequency: freq,
      enabled: !!record.enabled,
      custom_times: freq === 'custom' ? (typeof record.times === 'string' ? JSON.parse(record.times) : record.times).join(', ') : '',
    });
    setModalOpen(true);
  };

  const columns = [
    { title: 'ID', dataIndex: 'id', width: 50 },
    { title: '设备', dataIndex: 'node_name', width: 150 },
    {
      title: '同步频率', dataIndex: 'frequency', width: 120,
      render: (v: string) => <Tag color={FREQ_TAGS[v] || 'default'}>{FREQUENCIES.find(f => f.value === v)?.label || v}</Tag>,
    },
    {
      title: '执行时间', dataIndex: 'times', width: 200,
      render: (v: any) => {
        const times = typeof v === 'string' ? JSON.parse(v) : v;
        return Array.isArray(times) ? times.slice(0, 6).join(', ') + (times.length > 6 ? ` ...共${times.length}个` : '') : '-';
      },
    },
    {
      title: '启用', dataIndex: 'enabled', width: 70,
      render: (v: boolean, record: any) => (
        <Switch checked={v} onChange={(checked) => api.put(`/api/reporting-schedules/${record.id}`, { enabled: checked, frequency: record.frequency || 'custom' }).then(fetch)} />
      ),
    },
    {
      title: '操作', width: 120,
      render: (_: any, record: any) => (
        <Space>
          <Button size="small" onClick={() => openEdit(record)}>编辑</Button>
          <Popconfirm title="确定删除?" onConfirm={() => api.delete(`/api/reporting-schedules/${record.id}`).then(fetch)}>
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Button type="primary" icon={<PlusOutlined />} onClick={openCreate} style={{ marginBottom: 16 }}>新增同步计划</Button>
      <Table rowKey="id" columns={columns} dataSource={data} loading={loading} />

      <Modal
        title={editing ? '编辑同步计划' : '新增同步计划'}
        open={modalOpen}
        onOk={handleSave}
        onCancel={() => { setModalOpen(false); form.resetFields(); setEditing(null); }}
        width={500}
      >
        <Form form={form} layout="vertical" initialValues={{ frequency: 'daily', enabled: true }}>
          <Form.Item name="node_id" label="设备" rules={[{ required: true, message: '请选择设备' }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择设备"
              options={nodes.map((n: any) => ({ label: n.display_name || n.node_name, value: n.id }))}
            />
          </Form.Item>
          <Form.Item name="frequency" label="同步频率" rules={[{ required: true }]}>
            <Select
              options={FREQUENCIES}
              onChange={(v) => setFrequency(v)}
            />
          </Form.Item>
          {frequency === 'custom' && (
            <Form.Item name="custom_times" label="自定义时间" extra="多个时间用逗号分隔，如: 08:00, 14:00, 20:00">
              <Select mode="tags" placeholder="输入时间点后回车" />
            </Form.Item>
          )}
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

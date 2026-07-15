import { useEffect, useState } from 'react';
import { Table, Button, Modal, Form, Input, Select, Space, Tag, message, Popconfirm } from 'antd';
import { PlusOutlined, EditOutlined, DeleteOutlined, PlayCircleOutlined } from '@ant-design/icons';
import api from '../api';

interface Company {
  id: number;
  name: string;
  short_name: string | null;
  province: string;
  province_url: string;
  feishu_contact_id: string | null;
  contact_name: string | null;
  contact_phone: string | null;
  account_status: string;
  last_query_at: string | null;
}

const PROVINCES = [
  { label: '四川', value: '四川', url: 'sc.122.gov.cn' },
  { label: '福建', value: '福建', url: 'fj.122.gov.cn' },
  { label: '广东', value: '广东', url: 'gd.122.gov.cn' },
  { label: '浙江', value: '浙江', url: 'zj.122.gov.cn' },
  { label: '江苏', value: '江苏', url: 'js.122.gov.cn' },
  { label: '上海', value: '上海', url: 'sh.122.gov.cn' },
  { label: '北京', value: '北京', url: 'bj.122.gov.cn' },
  { label: '湖北', value: '湖北', url: 'hb.122.gov.cn' },
  { label: '湖南', value: '湖南', url: 'hn.122.gov.cn' },
  { label: '山东', value: '山东', url: 'sd.122.gov.cn' },
  { label: '河南', value: '河南', url: 'he.122.gov.cn' },
  { label: '河北', value: '河北', url: 'hb.122.gov.cn' },
  { label: '安徽', value: '安徽', url: 'ah.122.gov.cn' },
  { label: '江西', value: '江西', url: 'jx.122.gov.cn' },
  { label: '陕西', value: '陕西', url: 'sn.122.gov.cn' },
  { label: '重庆', value: '重庆', url: 'cq.122.gov.cn' },
];

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

  const fetch = (p = page, pf = provinceFilter, sf = statusFilter) => {
    setLoading(true);
    api.get('/api/companies', { params: { page: p, pageSize: 20, province: pf, account_status: sf } })
      .then(({ data: d }) => { setData(d.data); setTotal(d.total); })
      .finally(() => setLoading(false));
  };

  useEffect(() => { fetch(); }, []);

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

  const handleStartQuery = async (record: Company) => {
    try {
      await api.post('/api/tasks', { company_id: record.id });
      message.success('任务已创建');
    } catch (err: unknown) {
      const res = (err as { response?: { data?: { error?: string; existingTask?: { id: number } } } })?.response?.data;
      if (res?.error === 'ACTIVE_TASK_EXISTS') {
        Modal.confirm({
          title: '该公司已有查询任务进行中',
          content: `检测到任务 #${res.existingTask!.id} 正在进行中。是否终止前序任务并启动新任务？`,
          okText: '终止并启动',
          cancelText: '取消',
          okType: 'danger',
          onOk: async () => {
            await api.post('/api/tasks/force-start', { company_id: record.id });
            message.success('新任务已创建');
          },
        });
      } else {
        message.error(res?.error || '创建失败');
      }
    }
  };

  const columns = [
    { title: 'ID', dataIndex: 'id', width: 60 },
    { title: '公司名称', dataIndex: 'name', ellipsis: true },
    { title: '简称', dataIndex: 'short_name', width: 120 },
    { title: '省份', dataIndex: 'province', width: 80 },
    {
      title: '状态', dataIndex: 'account_status', width: 80,
      render: (s: string) => <Tag color={s === 'online' ? 'green' : 'default'}>{s === 'online' ? '在线' : '离线'}</Tag>,
    },
    { title: '联系人', dataIndex: 'contact_name', width: 100 },
    { title: '电话', dataIndex: 'contact_phone', width: 130 },
    {
      title: '操作', width: 200,
      render: (_: unknown, record: Company) => (
        <Space>
          <Button size="small" icon={<PlayCircleOutlined />} onClick={() => handleStartQuery(record)}>查询</Button>
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

      <Modal title={editing ? '编辑公司' : '新增公司'} open={modalOpen} onOk={handleSave} onCancel={() => setModalOpen(false)} width={600}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="公司名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="short_name" label="简称">
            <Input />
          </Form.Item>
          <Form.Item name="province" label="省份" rules={[{ required: true }]}>
            <Select options={PROVINCES.map((p) => ({ label: p.label, value: p.value }))}
              onChange={(v) => { const p = PROVINCES.find((x) => x.value === v); form.setFieldValue('province_url', p?.url); }} />
          </Form.Item>
          <Form.Item name="province_url" label="12123 URL" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="contact_name" label="联系人"><Input /></Form.Item>
          <Form.Item name="contact_phone" label="联系电话"><Input /></Form.Item>
          <Form.Item name="feishu_contact_id" label="飞书联系人ID"><Input /></Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

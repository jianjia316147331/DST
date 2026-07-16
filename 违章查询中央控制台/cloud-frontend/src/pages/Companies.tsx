import { useEffect, useState } from 'react';
import { Table, Button, Modal, Form, Input, Select, Space, Tag, message, Popconfirm, Image } from 'antd';
import { PlusOutlined, EditOutlined, DeleteOutlined, PlayCircleOutlined, QrcodeOutlined } from '@ant-design/icons';
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
  const [nodes, setNodes] = useState<{ id: number; node_name: string; status: string }[]>([]);
  const [bindings, setBindings] = useState<Record<number, { node_id: number; node_name: string }>>({});
  const [qrModalOpen, setQrModalOpen] = useState(false);
  const [qrImage, setQrImage] = useState('');
  const [qrCompanyName, setQrCompanyName] = useState('');
  const [loginConfirmOpen, setLoginConfirmOpen] = useState(false);
  const [loginCompany, setLoginCompany] = useState<Company | null>(null);
  const [loginLoading, setLoginLoading] = useState(false);

  const fetch = (p = page, pf = provinceFilter, sf = statusFilter) => {
    setLoading(true);
    api.get('/api/companies', { params: { page: p, pageSize: 20, province: pf, account_status: sf } })
      .then(({ data: d }) => { setData(d.data); setTotal(d.total); })
      .finally(() => setLoading(false));
  };

  useEffect(() => { fetch(); }, []);

  // Load nodes for binding
  useEffect(() => {
    api.get('/api/nodes').then(({ data: d }) => setNodes(d.data || []));
  }, []);

  // WebSocket for QR code and keepalive
  useEffect(() => {
    const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws?client=frontend`;
    const socket = new WebSocket(wsUrl);
    socket.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === 'qr_code') {
          setQrCompanyName(msg.company_name);
          setQrImage(`data:image/png;base64,${msg.image_base64}`);
          setQrModalOpen(true);
        } else if (msg.type === 'login_ok') {
          message.success(`${msg.company_name} 登录成功`);
          setQrModalOpen(false);
          setQrImage('');
          fetch();
        } else if (msg.type === 'login_failed') {
          message.error(`${msg.company_name} 登录失败: ${msg.reason}`);
        } else if (msg.type === 'keepalive_status') {
          // Refresh to show updated account_status
          fetch();
        }
      } catch { /* ignore */ }
    };
    return () => socket.close();
  }, []);

  // Step 1: Open login confirm modal
  const handleLogin = (record: Company) => {
    setLoginCompany(record);
    setLoginConfirmOpen(true);
  };

  // Step 2: Actually trigger login
  const startLogin = async () => {
    if (!loginCompany) return;
    setLoginLoading(true);
    try {
      await api.post('/api/sync/trigger-login', { company_name: loginCompany.name, company_id: loginCompany.id });
      setLoginConfirmOpen(false);
      message.success(`已向 ${loginCompany.name} 绑定的设备发送扫码登录指令，等待二维码...`);
    } catch (err: any) {
      message.error(err?.response?.data?.error || '触发登录失败');
    } finally {
      setLoginLoading(false);
    }
  };

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
    {
      title: '绑定设备', width: 180,
      render: (_: unknown, record: Company) => {
        const binding = bindings[record.id];
        return (
          <Select
            size="small"
            placeholder="选择设备"
            style={{ width: 160 }}
            value={binding?.node_id}
            onChange={async (nodeId) => {
              try {
                await api.put(`/api/companies/${record.id}/bind`, { node_id: nodeId });
                setBindings(prev => ({ ...prev, [record.id]: { node_id: nodeId, node_name: nodes.find(n => n.id === nodeId)?.node_name || '' } }));
                message.success('绑定成功');
              } catch { message.error('绑定失败'); }
            }}
            options={nodes.map(n => ({
              label: `${n.node_name}${n.status === 'online' ? ' ●在线' : ' ○离线'}`,
              value: n.id,
            }))}
          />
        );
      },
    },
    { title: '联系人', dataIndex: 'contact_name', width: 100 },
    { title: '电话', dataIndex: 'contact_phone', width: 130 },
    {
      title: '操作', width: 280,
      render: (_: unknown, record: Company) => (
        <Space>
          <Button size="small" icon={<PlayCircleOutlined />} onClick={() => handleStartQuery(record)}>查询</Button>
          {record.account_status === 'offline' && (
            <Button size="small" icon={<QrcodeOutlined />} onClick={() => handleLogin(record)}>登录</Button>
          )}
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

      {/* Step 1: Login confirm modal with "开始登录流程" button */}
      <Modal
        title={`${loginCompany?.name || ''} - 扫码登录`}
        open={loginConfirmOpen}
        onCancel={() => { setLoginConfirmOpen(false); setLoginCompany(null); }}
        footer={null}
        width={400}
      >
        <div style={{ textAlign: 'center', padding: '20px 0' }}>
          <p style={{ fontSize: 14, color: '#666', marginBottom: 20 }}>
            将为 <strong>{loginCompany?.name}</strong> 启动扫码登录流程
          </p>
          <p style={{ fontSize: 12, color: '#999', marginBottom: 24 }}>
            点击下方按钮后，系统将通过绑定的 skill 设备打开登录页面并生成二维码
          </p>
          <Button
            type="primary"
            size="large"
            icon={<QrcodeOutlined />}
            loading={loginLoading}
            onClick={startLogin}
            block
          >
            开始登录流程
          </Button>
        </div>
      </Modal>

      {/* Step 2: QR code display modal */}
      <Modal
        title={`${qrCompanyName} - 扫码登录`}
        open={qrModalOpen}
        onCancel={() => { setQrModalOpen(false); setQrImage(''); }}
        footer={null}
        width={400}
      >
        {qrImage ? (
          <div style={{ textAlign: 'center' }}>
            <Image src={qrImage} alt="二维码" style={{ maxWidth: 300 }} preview={false} />
            <p style={{ marginTop: 12, color: '#666' }}>请用 12123 App 扫描二维码登录</p>
          </div>
        ) : (
          <div style={{ textAlign: 'center', padding: 40, color: '#999' }}>等待二维码生成...</div>
        )}
      </Modal>

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

import { useEffect, useState } from 'react';
import { Card, Table, Button, Modal, Form, Input, Space, Switch, message, Popconfirm } from 'antd';
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons';
import api from '../api';

interface WhitelistUser {
  id: number;
  phone: string;
  name: string | null;
  enabled: boolean;
}

export default function Settings() {
  const [users, setUsers] = useState<WhitelistUser[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  const fetch = () => api.get('/api/whitelist').then(({ data }) => setUsers(data.data));

  useEffect(() => { fetch(); }, []);

  const handleAdd = async () => {
    const values = await form.validateFields();
    await api.post('/api/whitelist', values);
    message.success('添加成功');
    setModalOpen(false);
    form.resetFields();
    fetch();
  };

  const toggleUser = async (user: WhitelistUser) => {
    await api.patch(`/api/whitelist/${user.id}`, { enabled: !user.enabled });
    fetch();
  };

  const columns = [
    { title: '手机号', dataIndex: 'phone', width: 150 },
    { title: '姓名', dataIndex: 'name', width: 120 },
    {
      title: '启用', dataIndex: 'enabled', width: 80,
      render: (v: boolean, record: WhitelistUser) => <Switch checked={v} onChange={() => toggleUser(record)} />,
    },
    { title: '添加时间', dataIndex: 'created_at', width: 180, render: (v: string) => new Date(v).toLocaleString() },
    {
      title: '操作', width: 80,
      render: (_: unknown, record: WhitelistUser) => (
        <Popconfirm title="确定移除?" onConfirm={() => api.delete(`/api/whitelist/${record.id}`).then(fetch)}>
          <Button size="small" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ];

  return (
    <div>
      <Card title="用户白名单" extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>添加用户</Button>}>
        <Table rowKey="id" columns={columns} dataSource={users} pagination={false} />
      </Card>

      <Modal title="添加白名单用户" open={modalOpen} onOk={handleAdd} onCancel={() => setModalOpen(false)}>
        <Form form={form} layout="vertical">
          <Form.Item name="phone" label="手机号" rules={[{ required: true, pattern: /^1[3-9]\d{9}$/, message: '请输入有效手机号' }]}>
            <Input placeholder="13800138000" />
          </Form.Item>
          <Form.Item name="name" label="姓名">
            <Input placeholder="可选" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

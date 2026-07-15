import { useEffect, useState } from 'react';
import { Table, Select, Space, Tag, Input } from 'antd';
import api from '../api';

const LEVEL_COLORS: Record<string, string> = { INFO: 'blue', WARN: 'orange', ERROR: 'red' };
const CATEGORY_COLORS: Record<string, string> = { system: 'default', task: 'blue', command: 'purple' };

export default function Logs() {
  const [data, setData] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(false);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [level, setLevel] = useState<string>();
  const [category, setCategory] = useState<string>();
  const [taskId, setTaskId] = useState('');

  const fetch = (p = page) => {
    setLoading(true);
    api.get('/api/logs', { params: { page: p, pageSize: 50, level, category, task_id: taskId || undefined } })
      .then(({ data: d }) => { setData(d.data); setTotal(d.total); })
      .finally(() => setLoading(false));
  };

  useEffect(() => { fetch(); }, [level, category, taskId]);

  const columns = [
    { title: 'ID', dataIndex: 'id', width: 70 },
    { title: '时间', dataIndex: 'created_at', width: 170, render: (v: string) => new Date(v).toLocaleString() },
    { title: '级别', dataIndex: 'level', width: 70, render: (v: string) => <Tag color={LEVEL_COLORS[v]}>{v}</Tag> },
    { title: '类别', dataIndex: 'category', width: 80, render: (v: string) => <Tag color={CATEGORY_COLORS[v]}>{v}</Tag> },
    { title: '任务ID', dataIndex: 'task_id', width: 70 },
    { title: '消息', dataIndex: 'message', ellipsis: true },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Select placeholder="级别" allowClear style={{ width: 100 }} value={level} onChange={setLevel}
          options={[{ label: 'INFO', value: 'INFO' }, { label: 'WARN', value: 'WARN' }, { label: 'ERROR', value: 'ERROR' }]} />
        <Select placeholder="类别" allowClear style={{ width: 120 }} value={category} onChange={setCategory}
          options={[{ label: '系统', value: 'system' }, { label: '任务', value: 'task' }, { label: '指令', value: 'command' }]} />
        <Input placeholder="任务ID" style={{ width: 100 }} value={taskId} onChange={(e) => setTaskId(e.target.value)} allowClear />
      </Space>
      <Table rowKey="id" columns={columns} dataSource={data} loading={loading}
        pagination={{ current: page, total, pageSize: 50, onChange: (p: number) => { setPage(p); fetch(p); } }} />
    </div>
  );
}

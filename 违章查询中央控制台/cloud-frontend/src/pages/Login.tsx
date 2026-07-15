import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, Input, Button, message, Typography } from 'antd';
import { PhoneOutlined } from '@ant-design/icons';
import { useAuthStore } from '../stores/auth';

const { Title } = Typography;

export default function Login() {
  const [phone, setPhone] = useState('');
  const [loading, setLoading] = useState(false);
  const login = useAuthStore((s) => s.login);
  const navigate = useNavigate();

  const handleLogin = async () => {
    if (!/^1[3-9]\d{9}$/.test(phone)) {
      message.error('请输入有效的手机号');
      return;
    }
    setLoading(true);
    try {
      await login(phone);
      message.success('登录成功');
      navigate('/');
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { error?: string } } })?.response?.data?.error || '登录失败';
      message.error(msg);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh', background: '#f0f2f5' }}>
      <Card style={{ width: 400 }}>
        <Title level={3} style={{ textAlign: 'center', marginBottom: 32 }}>违章查询中央控制台</Title>
        <Input
          size="large"
          prefix={<PhoneOutlined />}
          placeholder="输入手机号登录"
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
          onPressEnter={handleLogin}
          maxLength={11}
        />
        <Button type="primary" size="large" block loading={loading} onClick={handleLogin} style={{ marginTop: 16 }}>
          登录
        </Button>
        <div style={{ textAlign: 'center', marginTop: 12, color: '#999', fontSize: 12 }}>
          仅白名单用户可登录
        </div>
      </Card>
    </div>
  );
}

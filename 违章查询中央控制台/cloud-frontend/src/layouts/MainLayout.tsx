import { useState } from 'react';
import { Outlet, useNavigate, useLocation } from 'react-router-dom';
import { Layout, Menu, Button, Dropdown, theme } from 'antd';
import {
  DashboardOutlined,
  BankOutlined,
  ScheduleOutlined,
  ClockCircleOutlined,
  CloudServerOutlined,
  FileTextOutlined,
  SettingOutlined,
  UserOutlined,
  DownloadOutlined,
  AppleOutlined,
  WindowsOutlined,
  LogoutOutlined,
} from '@ant-design/icons';
import { useAuthStore } from '../stores/auth';

const { Sider, Content, Header } = Layout;

const menuItems = [
  { key: '/', icon: <DashboardOutlined />, label: '工作台' },
  { key: '/companies', icon: <BankOutlined />, label: '企业管理' },
  { key: '/tasks', icon: <ScheduleOutlined />, label: '查询任务' },
  { key: '/schedules', icon: <ClockCircleOutlined />, label: '定时计划' },
  { key: '/nodes', icon: <CloudServerOutlined />, label: '设备管理' },
  { key: '/logs', icon: <FileTextOutlined />, label: '系统日志' },
  {
    key: '/settings',
    icon: <SettingOutlined />,
    label: '设置',
    children: [
      { key: '/settings', label: '用户' },
    ],
  },
];

export default function MainLayout() {
  const [collapsed, setCollapsed] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout } = useAuthStore();
  const { token: { colorBgContainer } } = theme.useToken();

  // Determine selected + open keys for nested menu
  const selectedKeys = [location.pathname];
  const openKeys = location.pathname.startsWith('/settings') ? ['/settings'] : [];

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider collapsible collapsed={collapsed} onCollapse={setCollapsed}>
        <div style={{ height: 48, margin: 16, color: '#fff', textAlign: 'center', fontWeight: 'bold', fontSize: collapsed ? 14 : 18, whiteSpace: 'nowrap', overflow: 'hidden' }}>
          {collapsed ? '违章' : '违章查询控制台'}
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={selectedKeys}
          defaultOpenKeys={openKeys}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Layout>
        <Header style={{ padding: '0 24px', background: colorBgContainer, display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 12 }}>
          <Dropdown
            menu={{
              items: [
                { key: 'mac', icon: <AppleOutlined />, label: 'macOS 版', onClick: () => window.open('/api/tray-app/download?platform=mac', '_blank') },
                { key: 'win', icon: <WindowsOutlined />, label: 'Windows 版', onClick: () => window.open('/api/tray-app/download?platform=win', '_blank') },
              ],
            }}
          >
            <Button type="link" icon={<DownloadOutlined />}>
              托盘插件下载
            </Button>
          </Dropdown>
          <span>{user?.name || user?.phone || '未登录'}</span>
          <Button type="text" icon={<LogoutOutlined />} onClick={logout}>退出</Button>
        </Header>
        <Content style={{ margin: 16, padding: 24, background: colorBgContainer, borderRadius: 8, minHeight: 280, overflow: 'auto' }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}

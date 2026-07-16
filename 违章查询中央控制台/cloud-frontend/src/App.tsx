import { useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { ConfigProvider, App as AntApp } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { useAuthStore } from './stores/auth';
import MainLayout from './layouts/MainLayout';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Companies from './pages/Companies';
import Tasks from './pages/Tasks';
import Schedules from './pages/Schedules';
import Nodes from './pages/Nodes';
import Logs from './pages/Logs';
import Settings from './pages/Settings';
import Vehicles from './pages/Vehicles';
import VehicleViolations from './pages/VehicleViolations';
import ReportingSchedules from './pages/ReportingSchedules';

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const token = useAuthStore((s) => s.token);
  if (!token) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export default function App() {
  const checkAuth = useAuthStore((s) => s.checkAuth);
  const token = useAuthStore((s) => s.token);

  useEffect(() => { checkAuth(); }, []);

  return (
    <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#1677ff' } }}>
      <AntApp>
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={token ? <Navigate to="/" replace /> : <Login />} />
            <Route path="/" element={<ProtectedRoute><MainLayout /></ProtectedRoute>}>
              <Route index element={<Dashboard />} />
              <Route path="companies" element={<Companies />} />
              <Route path="tasks" element={<Tasks />} />
              <Route path="schedules" element={<Schedules />} />
              <Route path="reporting-schedules" element={<ReportingSchedules />} />
              <Route path="nodes" element={<Nodes />} />
              <Route path="logs" element={<Logs />} />
              <Route path="settings" element={<Settings />} />
              <Route path="vehicles" element={<Vehicles />} />
              <Route path="vehicles/:id" element={<VehicleViolations />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </AntApp>
    </ConfigProvider>
  );
}

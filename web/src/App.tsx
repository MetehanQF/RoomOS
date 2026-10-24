import { Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { DashboardPage } from './pages/Dashboard'
import { LightsPage } from './pages/Lights'
import { SystemPage } from './pages/System'
import { RoutinesPage } from './pages/Routines'
import { ActivityPage } from './pages/Activity'
import { DevicesPage } from './pages/Devices'
import { DeviceDetailPage } from './pages/DeviceDetail'
import { PanelPage } from './pages/Panel'
import { NotificationsPage } from './pages/Notifications'
import { AuthBoundary } from './components/AuthBoundary'

export default function App() {
  return <AuthBoundary><Routes><Route element={<Layout />}><Route index element={<Navigate to="/home" replace />} /><Route path="home" element={<DashboardPage />} /><Route path="panel" element={<PanelPage />} /><Route path="lights" element={<LightsPage />} /><Route path="routines" element={<RoutinesPage />} /><Route path="notifications" element={<NotificationsPage />} /><Route path="activity" element={<ActivityPage />} /><Route path="devices" element={<DevicesPage />} /><Route path="devices/:deviceId" element={<DeviceDetailPage />} /><Route path="system" element={<SystemPage />} /></Route><Route path="*" element={<Navigate to="/home" replace />} /></Routes></AuthBoundary>
}

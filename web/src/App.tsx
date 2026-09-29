import { Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { HomePage } from './pages/Home'
import { LightsPage } from './pages/Lights'
import { SystemPage } from './pages/System'
import { AuthBoundary } from './components/AuthBoundary'

export default function App() {
  return <AuthBoundary><Routes><Route element={<Layout />}><Route index element={<Navigate to="/home" replace />} /><Route path="home" element={<HomePage />} /><Route path="lights" element={<LightsPage />} /><Route path="system" element={<SystemPage />} /></Route><Route path="*" element={<Navigate to="/home" replace />} /></Routes></AuthBoundary>
}

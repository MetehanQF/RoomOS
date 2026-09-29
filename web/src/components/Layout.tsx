import { NavLink, Outlet } from 'react-router-dom'
import { Icon, type IconName } from './Icons'
import { BrandLogo } from './Brand'

const nav: { to: string; label: string; icon: IconName }[] = [
  { to: '/home', label: 'Home', icon: 'home' },
  { to: '/lights', label: 'Lights', icon: 'lights' },
  { to: '/system', label: 'System', icon: 'system' },
]

export function Layout() {
  return <div className="app-shell">
    <div className="ambient ambient-one" /><div className="ambient ambient-two" />
    <aside className="navigation">
      <div className="brand"><span className="brand-mark"><BrandLogo /></span><div><strong>RoomOS</strong><small>Personal space</small></div></div>
      <nav aria-label="Primary navigation">{nav.map((item) => <NavLink key={item.to} to={item.to}><Icon name={item.icon} /><span>{item.label}</span></NavLink>)}</nav>
      <div className="navigation-foot"><span className="core-pulse" /><div><strong>RoomOS Core</strong><small>Private · Local</small></div></div>
    </aside>
    <main className="content"><Outlet /></main>
  </div>
}

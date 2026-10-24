import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { Icon } from './Icons'
import { navigationItems } from '../navigation'
import { BrandLogo } from './Brand'
import { useConnectionStatus } from '../hooks'
import { activateWaitingWorker, isStandalone, type PwaUpdateState } from '../pwa'
import { IdleController } from '../idle'
import { useAuthStatus } from '../auth-context'
import type { ConnectionStatus } from '../types'
import { useClientTelemetry } from '../client-telemetry'
import { readPanelSettings, shouldOpenPanel } from '../panel-mode'
import { api } from '../api'
import { listenForWorkerNavigation } from '../push-client'

const NO_SCOPES: string[] = []

export function Layout() {
  const connection = useConnectionStatus()
  const scopes = useAuthStatus()?.device?.scopes ?? NO_SCOPES
  const items = navigationItems(scopes)
  useClientTelemetry(scopes, connection === 'connected', api.sendTelemetry)
  const path = useLocation().pathname
  const isHome = path === '/home'
  const isPanel = path === '/panel'
  const navigate = useNavigate()
  useEffect(() => listenForWorkerNavigation('serviceWorker' in navigator ? navigator.serviceWorker : null, navigate), [navigate])
  const launched = useRef(false)
  useEffect(() => {
    // 7E: a device that chose panel mode opens in it on a fresh start; leaving it is never undone.
    if (launched.current) return
    launched.current = true
    let settings
    try { settings = readPanelSettings(window.localStorage) } catch { return }
    if (shouldOpenPanel(settings, path)) navigate('/panel', { replace: true })
  }, [navigate, path])
  const [update, setUpdate] = useState<PwaUpdateState>('current')
  const [idle, setIdle] = useState(false)
  const [standalone] = useState(() => isStandalone())
  useEffect(() => {
    const receiveUpdate = (event: Event) => setUpdate((event as CustomEvent<PwaUpdateState>).detail)
    window.addEventListener('roomos:pwa-update', receiveUpdate)
    return () => window.removeEventListener('roomos:pwa-update', receiveUpdate)
  }, [])
  useEffect(() => {
    const controller = new IdleController(setIdle)
    const activity = () => controller.activity()
    controller.start()
    const events: Array<keyof WindowEventMap> = ['pointerdown', 'keydown', 'touchstart']
    events.forEach((name) => window.addEventListener(name, activity, { passive: true }))
    return () => { controller.stop(); events.forEach((name) => window.removeEventListener(name, activity)) }
  }, [])
  return <div className={`app-shell connection-${connection} ${idle ? 'is-idle' : ''} ${standalone ? 'is-standalone' : ''} ${isPanel ? 'is-panel-mode' : ''}`} data-connection={connection}>
    <div className="ambient ambient-one" /><div className="ambient ambient-two" />
    {!isPanel && <aside className="navigation">
      <div className="brand"><span className="brand-mark"><BrandLogo /></span><div><strong>RoomOS</strong><small>Personal space</small></div></div>
      <nav aria-label="Primary navigation" style={{ '--nav-items': items.length } as CSSProperties}>{items.map((item) => <NavLink key={item.to} to={item.to}><Icon name={item.icon} /><span>{item.label}</span></NavLink>)}</nav>
      <div className="navigation-foot"><span className="core-pulse" /><div><strong>RoomOS Core</strong><small>{connection === 'connected' ? 'Connected · Private' : connection === 'reconnecting' ? 'Reconnecting…' : 'Offline'}</small></div></div>
    </aside>}
    <main className="content">
      <LifecycleBanners quiet={isHome || isPanel || path === '/lights'} connection={connection} update={update} onReload={() => void activateWaitingWorker()} />
      <Outlet />
    </main>
  </div>
}

export function LifecycleBanners({ connection, update, onReload, quiet = false }: { quiet?: boolean; connection: ConnectionStatus; update: PwaUpdateState; onReload: () => void }) {
  return <div className="lifecycle-banners" aria-live={quiet ? undefined : 'polite'}>
    {connection !== 'connected' && <div className={`connection-banner is-${connection}`} role={quiet ? undefined : connection === 'offline' ? 'alert' : 'status'}><Icon name="wifi" /><div><strong>{connection === 'reconnecting' ? 'Reconnecting…' : 'RoomOS offline'}</strong><span>{connection === 'reconnecting' ? 'Refreshing live state when the private connection returns.' : 'Controls unavailable · visible state may be stale.'}</span></div></div>}
    {update === 'available' && <div className="update-banner" role={quiet ? undefined : 'status'}><Icon name="system" /><div><strong>RoomOS update available</strong><span>Reload when you are ready.</span></div><button onClick={onReload}>Reload</button></div>}
  </div>
}

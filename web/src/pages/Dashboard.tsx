import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { useAuthStatus } from '../auth-context'
import { formatLocalClock } from '../home-dashboard'
import { HomeStore, type HomeApi } from '../home-store'
import { LightsStore, type LightsApi } from '../lights-store'
import { DevicesStore, bindDevicesLifecycle } from '../devices-store'
import { SystemStore } from '../system-store'
import { SharedRequests } from '../shared-requests'
import { roomosLifecycle } from '../lifecycle'
import { summarizeMainLights } from '../main-lights'
import {
  WIDGETS, SIZE_LABEL, defaultLayout, moveWidget, readLayout, resizeWidget, sameLayout, setWidgetVisible, toggleFavorite,
  widgetAllowed, writeLayout, type DashboardLayout, type PerformanceChoice, type WidgetId, type WidgetSize,
} from '../dashboard-layout'
import { StatusPill } from '../components/Common'
import { Icon } from '../components/Icons'
import {
  AlertsWidget, DevicesWidget, LightsWidget, OverviewWidget, PerformanceWidget, RoutinesWidget, ServicesWidget, WidgetBoundary,
} from '../components/DashboardWidgets'
import type { LightsActions } from './Lights'
import type { MainLightsActions } from './PanelCards'
import type { ConnectionStatus, RoomOSEvent, Routine } from '../types'
import type { SceneId } from '../home-state'
import '../dashboard.css'

/* Phase 7L — Dashboard 2.0 (/home). Reuses HomeStore (control, scenes, routines, alerts, health),
   LightsStore (per-light and main-group commands, never optimistic), DevicesStore and a small
   SystemStore over the one shared WebSocket. Bootstrap happens once per connection; events update
   in place. The only timers are minute/half-minute clocks for relative times, paused when hidden. */

function dashboardApis(shared: SharedRequests): { home: HomeApi; lights: LightsApi } {
  const lights = shared.share('lights', () => api.lights())
  const health = shared.share('health', () => api.health())
  return {
    home: {
      health, lights, scenes: api.scenes, systemHealth: api.systemHealth, alerts: api.alerts, routines: api.routines, automations: api.automations,
      camera: async () => ({ authorized: false, camera: null }), // no camera widget in 7L: never read it
      activateScene: api.activateScene, allPower: api.allPower, runRoutine: api.runRoutine,
    },
    lights: {
      lights, health, power: api.power, brightness: api.brightness, color: api.color, temperature: api.temperature,
      effect: api.effect, allPower: api.allPower, activateScene: api.activateScene,
      mainPower: api.mainPower, mainBrightness: api.mainBrightness, mainTemperature: api.mainTemperature,
    },
  }
}

type LifecycleStore = { load: () => Promise<void>; setConnection: (status: ConnectionStatus) => void; handleEvent: (event: RoomOSEvent) => void }

/** Same binding as Home, Lights and Panel: bootstrap on connect, events in place, no polling. */
function useStoreLifecycle(store: LifecycleStore, hasData: () => boolean, enabled = true) {
  const hasDataRef = useRef(hasData)
  useEffect(() => { hasDataRef.current = hasData })
  useEffect(() => {
    if (!enabled) return undefined
    if (!roomosLifecycle) { void store.load(); return undefined }
    if (roomosLifecycle.snapshot === 'connected') void store.load()
    const offStatus = roomosLifecycle.subscribeStatus((status) => {
      store.setConnection(status)
      if (status === 'offline' && !hasDataRef.current()) void store.load()
    })
    const offEvents = roomosLifecycle.subscribeEvents((event) => store.handleEvent(event), () => store.load(), { refreshOnEvent: false })
    return () => { offStatus(); offEvents() }
  }, [store, enabled])
}

function useDashboardStores() {
  const auth = useAuthStatus()
  const scopeKey = (auth?.device?.scopes ?? []).join(' ')
  const scopes = useMemo(() => (scopeKey ? scopeKey.split(' ') : []), [scopeKey])
  const resolved = auth?.authenticated === true
  const [shared] = useState(() => new SharedRequests())
  const [apis] = useState(() => dashboardApis(shared))
  // Declared first: the request generation moves before any store reacts to the same status change.
  useEffect(() => roomosLifecycle?.subscribeStatus(() => shared.invalidate()), [shared])
  const initial = roomosLifecycle?.snapshot ?? 'reconnecting'
  const home = useMemo(() => new HomeStore(apis.home, scopes, resolved, initial), [apis, scopes, resolved]) // eslint-disable-line react-hooks/exhaustive-deps
  const lights = useMemo(() => new LightsStore(apis.lights, scopes, resolved, initial), [apis, scopes, resolved]) // eslint-disable-line react-hooks/exhaustive-deps
  const systemAllowed = scopes.includes('system:read')
  const system = useMemo(() => new SystemStore(api.system, systemAllowed, initial), [systemAllowed]) // eslint-disable-line react-hooks/exhaustive-deps
  const [devices] = useState(() => new DevicesStore({ devices: api.devices }, initial))
  const deviceAccess = scopes.includes('devices:read') || scopes.includes('system:read')
  const lightsAllowed = scopes.includes('lights:read')
  const homeLifecycle = useMemo<LifecycleStore>(() => ({ load: () => home.load(), setConnection: (s) => home.setConnection(s), handleEvent: (e) => home.handleEvent(e) }), [home])
  useStoreLifecycle(homeLifecycle, () => home.getSnapshot().loaded)
  useStoreLifecycle(lights, () => Boolean(lights.getSnapshot().lights), lightsAllowed)
  useStoreLifecycle(system, () => Boolean(system.getSnapshot().data), systemAllowed)
  useEffect(() => {
    if (!deviceAccess) return undefined
    if (!roomosLifecycle) { void devices.load(); return undefined }
    return bindDevicesLifecycle(devices, roomosLifecycle)
  }, [deviceAccess, devices])
  return {
    auth, scopes, home, lights, system, deviceAccess,
    homeSnapshot: useSyncExternalStore(home.subscribe, home.getSnapshot),
    lightsSnapshot: useSyncExternalStore(lights.subscribe, lights.getSnapshot),
    systemSnapshot: useSyncExternalStore(system.subscribe, system.getSnapshot),
    devicesSnapshot: useSyncExternalStore(devices.subscribe, devices.getSnapshot),
  }
}

function safeStorage(): Storage | null {
  try { return typeof window === 'undefined' ? null : window.localStorage } catch { return null }
}

function Clock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    let timer = 0
    const tick = () => { setNow(new Date()); timer = window.setTimeout(tick, 60_000 - (Date.now() % 60_000)) }
    const visibility = () => { window.clearTimeout(timer); if (document.visibilityState === 'visible') tick() }
    visibility()
    document.addEventListener('visibilitychange', visibility)
    return () => { window.clearTimeout(timer); document.removeEventListener('visibilitychange', visibility) }
  }, [])
  const local = formatLocalClock(now)
  return <time className="dash-clock" dateTime={now.toISOString()}><strong>{local.time}</strong><span>{local.date}</span></time>
}

const CONNECTION_LABEL: Record<ConnectionStatus, string> = { connected: 'Live', reconnecting: 'Reconnecting…', offline: 'Offline' }

/* ---------- edit chrome ---------- */

function EditChrome({ id, index, count, size, onMove, onSize, onHide, onDragStart }: {
  id: WidgetId; index: number; count: number; size: WidgetSize
  onMove: (to: number) => void; onSize: (size: WidgetSize) => void; onHide: () => void; onDragStart: (event: PointerEvent<HTMLButtonElement>) => void
}) {
  const definition = WIDGETS[id]
  const keyMove = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') { event.preventDefault(); onMove(index - 1) }
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') { event.preventDefault(); onMove(index + 1) }
  }
  return <div className="dash-edit-chrome">
    <button type="button" className="dash-grip" aria-label={`Move ${definition.title}. Drag, or use the arrow keys`} onPointerDown={onDragStart} onKeyDown={keyMove}><Icon name="grip" /></button>
    <strong>{definition.title}</strong>
    <div className="dash-edit-actions">
      <button type="button" aria-label={`Move ${definition.title} up`} disabled={index === 0} onClick={() => onMove(index - 1)}><Icon name="arrowUp" /></button>
      <button type="button" aria-label={`Move ${definition.title} down`} disabled={index === count - 1} onClick={() => onMove(index + 1)}><Icon name="arrowDown" /></button>
      {definition.sizes.length > 1 && <label className="dash-size"><span className="sr-only">{definition.title} size</span>
        <select value={size} onChange={(event) => onSize(event.target.value as WidgetSize)}>{definition.sizes.map((item) => <option key={item} value={item}>{SIZE_LABEL[item]}</option>)}</select></label>}
      {definition.pinned
        ? <span className="dash-pinned" title="Always visible: keeps health and alerts one glance away">Always shown</span>
        : <button type="button" aria-label={`Hide ${definition.title}`} onClick={onHide}><Icon name="eyeOff" /></button>}
    </div>
  </div>
}

/* ---------- page ---------- */

export function DashboardPage() {
  const { auth, scopes, home, lights, deviceAccess, homeSnapshot, lightsSnapshot, systemSnapshot, devicesSnapshot } = useDashboardStores()
  const deviceId = auth?.device?.id ?? null
  const [saved, setSaved] = useState<DashboardLayout>(() => readLayout(safeStorage(), deviceId))
  const [draft, setDraft] = useState<DashboardLayout | null>(null)
  const [notice, setNotice] = useState<{ tone: 'success' | 'error'; text: string } | null>(null)
  const [announce, setAnnounce] = useState('')
  const [dragging, setDragging] = useState<WidgetId | null>(null)
  // Auth resolves after the first render on a cold start: load this device's own layout then.
  const [layoutOwner, setLayoutOwner] = useState(deviceId)
  if (layoutOwner !== deviceId) { setLayoutOwner(deviceId); setSaved(readLayout(safeStorage(), deviceId)); setDraft(null) }

  const editing = draft !== null
  const layout = draft ?? saved
  const connection = homeSnapshot.connection
  const control = useMemo(() => home.control(), [home, homeSnapshot]) // eslint-disable-line react-hooks/exhaustive-deps -- derives from the snapshot
  const summary = useMemo(() => summarizeMainLights(lightsSnapshot.lights), [lightsSnapshot.lights])
  const ambient = lightsSnapshot.lights?.find((light) => light.id === 'desk_ambient') ?? null
  const lightsLive = lightsSnapshot.connection === 'connected' && lightsSnapshot.trusted && lightsSnapshot.control?.home_assistant === 'connected'

  const lightActions: LightsActions = useMemo(() => ({
    can: (light, action) => lights.can(light, action), canAllOn: lights.canAllOn(), canAllOff: lights.canAllOff(), reason: lights.page().reason,
    power: (light, on) => { void lights.power(light, on) }, brightness: (light, value) => { void lights.brightness(light, value) },
    color: (light, rgb) => { void lights.color(light, rgb) }, temperature: (light, kelvin) => { void lights.temperature(light, kelvin) },
    effect: (light, effect) => { void lights.effect(light, effect) }, allOn: () => { void lights.allOn() }, allOff: () => { void lights.allOff() },
    dismiss: () => lights.dismiss(),
  }), [lights, lightsSnapshot]) // eslint-disable-line react-hooks/exhaustive-deps -- permissions derive from the snapshot
  const mainActions: MainLightsActions = useMemo(() => ({
    can: (action) => lights.canMain(action),
    power: (on) => { void lights.mainPower(on) }, brightness: (percent) => { void lights.mainBrightness(percent) }, temperature: (kelvin) => { void lights.mainTemperature(kelvin) },
  }), [lights, lightsSnapshot]) // eslint-disable-line react-hooks/exhaustive-deps

  // Stable callbacks: a device or system event re-renders the page, but must not re-render widgets
  // whose own data did not change (memoised widgets compare props by identity).
  const dismissLights = useCallback(() => lights.dismiss(), [lights])
  const dismissHome = useCallback(() => home.dismissFeedback(), [home])
  const activateScene = useCallback((id: SceneId) => { void home.activateScene(id) }, [home])
  const runRoutine = useCallback((id: string) => { void home.runRoutine(id) }, [home])
  const routineGuard = useCallback((routine: Routine) => home.routineGuard(routine), [home, homeSnapshot]) // eslint-disable-line react-hooks/exhaustive-deps -- guard reads the snapshot

  const persist = useCallback((next: DashboardLayout, message: string | null) => {
    if (writeLayout(safeStorage(), deviceId, next)) { setSaved(next); if (message) setNotice({ tone: 'success', text: message }); return true }
    setNotice({ tone: 'error', text: deviceId ? 'This browser refused to store the layout (private mode or storage full). Nothing was saved.' : 'Sign-in state is still loading; try again in a moment.' })
    return false
  }, [deviceId])

  const onFavorite = useCallback((id: string) => { persist(toggleFavorite(saved, id), null) }, [persist, saved])
  const onChoice = useCallback((choice: PerformanceChoice) => {
    if (editing) setDraft((current) => current && { ...current, performance: choice })
    else persist({ ...saved, performance: choice }, null)
  }, [editing, persist, saved])

  const move = (id: WidgetId, to: number) => {
    setDraft((current) => {
      if (!current) return current
      const next = moveWidget(current, id, to)
      if (next !== current) setAnnounce(`${WIDGETS[id].title} moved to position ${next.widgets.findIndex((item) => item.id === id) + 1} of ${next.widgets.length}.`)
      return next
    })
  }
  const startDrag = (id: WidgetId) => (event: PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    setDragging(id)
  }
  const dragMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!dragging) return
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>('[data-widget]')?.dataset.widget as WidgetId | undefined
    if (!target || target === dragging || !draft) return
    move(dragging, draft.widgets.findIndex((item) => item.id === target))
  }
  const endDrag = () => setDragging(null)

  const save = () => {
    if (!draft) return
    if (persist(draft, 'Dashboard layout saved on this device.')) setDraft(null)
  }
  const cancel = () => { setDraft(null); setNotice(null) }
  const reset = () => { setDraft(defaultLayout()); setAnnounce('Default layout restored. Save to keep it.') }

  const widgets: Record<WidgetId, ReactNode> = {
    system_overview: <OverviewWidget home={homeSnapshot} devices={devicesSnapshot} devicesAllowed={deviceAccess} system={systemSnapshot} connection={connection} />,
    lights: <LightsWidget snapshot={lightsSnapshot} summary={summary} ambient={ambient} live={lightsLive} main={mainActions} light={lightActions} onDismiss={dismissLights} />,
    alerts: <AlertsWidget snapshot={homeSnapshot} connection={connection} size={layout.widgets.find((item) => item.id === 'alerts')?.size ?? 'md'} />,
    devices: <DevicesWidget snapshot={devicesSnapshot} />,
    services: <ServicesWidget system={systemSnapshot} home={homeSnapshot} connection={connection} />,
    performance: <PerformanceWidget devices={devicesSnapshot} choice={layout.performance} onChoice={onChoice} connection={connection} />,
    routines: <RoutinesWidget snapshot={homeSnapshot} control={control} favorites={saved.favorites} guard={routineGuard}
      onScene={activateScene} onRun={runRoutine} onFavorite={onFavorite} onDismiss={dismissHome} />,
  }

  const allowed = layout.widgets.filter((item) => widgetAllowed(item.id, scopes))
  const shown = allowed.filter((item) => item.visible)
  const hidden = allowed.filter((item) => !item.visible)
  const prohibited = layout.widgets.filter((item) => !widgetAllowed(item.id, scopes))
  const dirty = draft !== null && !sameLayout(draft, saved)

  useEffect(() => {
    if (!notice || notice.tone !== 'success') return undefined
    const timer = window.setTimeout(() => setNotice(null), 4_000)
    return () => window.clearTimeout(timer)
  }, [notice])

  return <section className={`page dash-page ${editing ? 'is-editing' : ''}`} aria-labelledby="dash-title">
    <header className="dash-header">
      <div className="dash-title"><p className="eyebrow">Your room</p><h1 id="dash-title">Dashboard</h1></div>
      <Clock />
      <div className="dash-header-actions">
        <StatusPill status={connection === 'connected' ? 'online' : connection === 'offline' ? 'offline' : 'stale'} label={CONNECTION_LABEL[connection]} />
        {!editing && <Link className="dash-button is-quiet" to="/panel"><Icon name="phone" />Panel</Link>}
        {!editing && <button type="button" className="dash-button" onClick={() => { setNotice(null); setDraft(structuredClone(saved)) }}><Icon name="edit" />Edit dashboard</button>}
      </div>
    </header>

    {editing && <div className="dash-editbar" role="region" aria-label="Edit dashboard">
      <p><strong>Editing layout</strong><span>Drag the handle or use the arrows. Changes apply on this device only.</span></p>
      <div className="dash-editbar-actions">
        <button type="button" className="dash-button is-quiet" onClick={reset}>Reset to default</button>
        <button type="button" className="dash-button is-quiet" onClick={cancel}>Cancel</button>
        <button type="button" className="dash-button is-primary" onClick={save} disabled={!dirty}>Save</button>
      </div>
      {(hidden.length > 0 || prohibited.length > 0) && <div className="dash-tray">
        {hidden.map((item) => <button key={item.id} type="button" className="dash-tray-item" onClick={() => setDraft((current) => current && setWidgetVisible(current, item.id, true))}>
          <Icon name="eye" /><span><strong>Show {WIDGETS[item.id].title}</strong><small>{WIDGETS[item.id].description}</small></span></button>)}
        {prohibited.map((item) => <div key={item.id} className="dash-tray-item is-locked" aria-disabled="true"><Icon name="eyeOff" />
          <span><strong>{WIDGETS[item.id].title}</strong><small>Not available on this device · needs {WIDGETS[item.id].scopes.join(' or ')}</small></span></div>)}
      </div>}
    </div>}
    {notice && <p className={`dash-notice is-${notice.tone}`} role={notice.tone === 'error' ? 'alert' : 'status'}>{notice.text}</p>}

    <div className={`dash-grid ${dragging ? 'is-dragging' : ''}`} onPointerMove={editing ? dragMove : undefined} onPointerUp={endDrag} onPointerCancel={endDrag}>
      {shown.map((item) => {
        const index = layout.widgets.findIndex((entry) => entry.id === item.id)
        return <article key={item.id} data-widget={item.id} className={`dash-widget size-${item.size} is-${item.id} ${dragging === item.id ? 'is-dragged' : ''}`} aria-label={WIDGETS[item.id].title}>
          {editing && <EditChrome id={item.id} index={index} count={layout.widgets.length} size={item.size}
            onMove={(to) => move(item.id, to)} onSize={(size) => setDraft((current) => current && resizeWidget(current, item.id, size))}
            onHide={() => { setDraft((current) => current && setWidgetVisible(current, item.id, false)); setAnnounce(`${WIDGETS[item.id].title} hidden. Show it again from the list above.`) }}
            onDragStart={startDrag(item.id)} />}
          {/* While editing, widget controls are inert so a layout gesture can never toggle a light. */}
          <div className="dash-widget-body" inert={editing || undefined}><WidgetBoundary title={WIDGETS[item.id].title}>{widgets[item.id]}</WidgetBoundary></div>
        </article>
      })}
    </div>
    {!editing && hidden.length > 0 && <p className="dash-meta dash-hidden-hint">{hidden.length} widget{hidden.length === 1 ? ' is' : 's are'} hidden · <button type="button" className="dash-linkish" onClick={() => setDraft(structuredClone(saved))}>Edit dashboard</button> to show {hidden.length === 1 ? 'it' : 'them'}.</p>}
    <div className="sr-only" aria-live="polite" aria-atomic="true">
      {announce}
      {homeSnapshot.announcement && <span key={homeSnapshot.announcement.id}>{homeSnapshot.announcement.text}</span>}
      {lightsSnapshot.announcement && <span key={`l${lightsSnapshot.announcement.id}`}>{lightsSnapshot.announcement.text}</span>}
    </div>
  </section>
}

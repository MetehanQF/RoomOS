import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { useAuthStatus } from '../auth-context'
import { Icon } from '../components/Icons'
import { StatusPill } from '../components/Common'
import { DevicesStore, bindDevicesLifecycle } from '../devices-store'
import { formatLocalClock } from '../home-dashboard'
import { canActivate, healthSummary, roomState, type SceneId } from '../home-state'
import { HomeStore, type HomeApi } from '../home-store'
import { loadCameraAccess } from '../camera-ui'
import { roomosLifecycle } from '../lifecycle'
import { IdleController } from '../idle'
import { LightsStore, type LightsApi } from '../lights-store'
import { displayName } from '../lights-state'
import { stateLine } from '../lights-state'
import { LightDetail, type LightsActions } from './Lights'
import {
  PanelWakeLockController, browserWakeLockEnvironment, enterPanelFullscreen, exitPanelFullscreen,
  readPanelSettings, resolveNight, writePanelSettings, type PanelSettings, type WakeLockStatus,
} from '../panel-mode'
import { panelDeviceTone, panelDevices, panelRoutines, panelScenes } from '../panel-state'
import { DEFAULT_PANEL_LAYOUT, normalizePanelLayout, type PanelCardId, type PanelLayout } from '../panel-layout'
import { summarizeMainLights } from '../main-lights'
import { mainLightsStatusLabel } from '../main-lights'
import { AMBIENT_CARDS, AMBIENT_IDLE_CHOICES, formatAmbientClock, readAmbientSettings, writeAmbientSettings, type AmbientSettings } from '../ambient-screen'
import { SharedRequests } from '../shared-requests'
import { PanelAlertIndicator, PanelAmbient, PanelCriticalStrip, PanelMainLights, PanelRoutines, PanelScenes, PanelSystem, type MainLightsActions } from './PanelCards'
import { PanelLayoutEditor } from './PanelLayoutEditor'
import { AmbientScreen } from './AmbientScreen'
import type { ConnectionStatus, RoomOSEvent } from '../types'

const AMBIENT_ID = 'desk_ambient'

/* Phase 7E — Phone 2 panel. Reuses HomeStore (scenes, routines, health), LightsStore (per-light
   control, never optimistic) and DevicesStore over the one shared WebSocket. No polling: the
   only timer is a once-a-minute clock that stops while the page is hidden. */

/** HomeStore and LightsStore both bootstrap /lights and /health: one shared request per connection. */
function panelApis(shared: SharedRequests): { home: HomeApi; lights: LightsApi } {
  const lights = shared.share('lights', () => api.lights())
  const health = shared.share('health', () => api.health())
  return {
    home: {
      health, lights, scenes: api.scenes, systemHealth: api.systemHealth, alerts: api.alerts,
      routines: api.routines, automations: api.automations, camera: loadCameraAccess, activateScene: api.activateScene, allPower: api.allPower,
    },
    lights: {
      lights, health, power: api.power, brightness: api.brightness, color: api.color,
      temperature: api.temperature, effect: api.effect, allPower: api.allPower, activateScene: api.activateScene,
      mainPower: api.mainPower, mainBrightness: api.mainBrightness, mainTemperature: api.mainTemperature,
    },
  }
}

/** Minute clock; paused while hidden (Android freezes background tabs anyway). */
function useMinuteClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    let first = 0, interval = 0
    const stop = () => { window.clearTimeout(first); window.clearInterval(interval); first = interval = 0 }
    const start = () => {
      stop(); setNow(new Date())
      first = window.setTimeout(() => { setNow(new Date()); interval = window.setInterval(() => setNow(new Date()), 60_000) }, 60_000 - (Date.now() % 60_000))
    }
    const visibility = () => (document.visibilityState === 'visible' ? start() : stop())
    visibility()
    document.addEventListener('visibilitychange', visibility)
    return () => { stop(); document.removeEventListener('visibilitychange', visibility) }
  }, [])
  return now
}

function useOnline() {
  const [online, setOnline] = useState(() => (typeof navigator === 'undefined' ? true : navigator.onLine))
  useEffect(() => {
    const update = () => setOnline(navigator.onLine)
    window.addEventListener('online', update); window.addEventListener('offline', update)
    return () => { window.removeEventListener('online', update); window.removeEventListener('offline', update) }
  }, [])
  return online
}

function useSettings() {
  const [settings, setSettings] = useState<PanelSettings>(() => readPanelSettings(window.localStorage))
  const change = (patch: Partial<PanelSettings>) => setSettings((current) => {
    const next = { ...current, ...patch }
    writePanelSettings(window.localStorage, next)
    return next
  })
  return [settings, change] as const
}

function useAmbientSettings(legacyMinutes: number) {
  const [ambient, setAmbient] = useState<AmbientSettings>(() => readAmbientSettings(window.localStorage, legacyMinutes))
  const changeAmbient = (patch: Partial<AmbientSettings>) => setAmbient((current) => {
    const next = { ...current, ...patch }
    writeAmbientSettings(window.localStorage, next)
    return next
  })
  return [ambient, changeAmbient] as const
}

function useWakeLock(enabled: boolean) {
  const [status, setStatus] = useState<WakeLockStatus>('off')
  const [controller] = useState(() => new PanelWakeLockController(browserWakeLockEnvironment(), enabled, setStatus))
  useEffect(() => { controller.start(); return () => controller.stop() }, [controller])
  useEffect(() => { controller.setEnabled(enabled) }, [controller, enabled])
  return status
}

/** Clock screensaver after N idle minutes (0 = off) or on demand; any touch dismisses it. */
function useScreenSaver(minutes: number) {
  const [saver, setSaver] = useState(false)
  useEffect(() => {
    if (!minutes) return undefined
    const controller = new IdleController((idle) => { if (idle) setSaver(true) }, minutes * 60_000)
    const activity = () => controller.activity()
    const events: Array<keyof WindowEventMap> = ['pointerdown', 'keydown', 'touchstart']
    controller.start()
    events.forEach((name) => window.addEventListener(name, activity, { passive: true }))
    return () => { controller.stop(); events.forEach((name) => window.removeEventListener(name, activity)) }
  }, [minutes])
  return [saver, setSaver] as const
}

type LifecycleStore = { load: () => Promise<void>; setConnection: (status: ConnectionStatus) => void; handleEvent: (event: RoomOSEvent) => void }

/** Same binding as the Home and Lights pages: bootstrap on connect, events in place, no polling. */
function useStoreLifecycle(store: LifecycleStore, hasData: () => boolean) {
  const hasDataRef = useRef(hasData)
  useEffect(() => { hasDataRef.current = hasData })
  useEffect(() => {
    if (!roomosLifecycle) { void store.load(); return }
    if (roomosLifecycle.snapshot === 'connected') void store.load()
    const offStatus = roomosLifecycle.subscribeStatus((status) => {
      store.setConnection(status)
      if (status === 'offline' && !hasDataRef.current()) void store.load()
    })
    const offEvents = roomosLifecycle.subscribeEvents((event) => store.handleEvent(event), () => store.load(), { refreshOnEvent: false })
    return () => { offStatus(); offEvents() }
  }, [store])
}

function usePanelStores() {
  const auth = useAuthStatus()
  const scopeKey = (auth?.device?.scopes ?? []).join(' ')
  const resolved = auth?.authenticated === true
  const [shared] = useState(() => new SharedRequests())
  const [apis] = useState(() => panelApis(shared))
  // Declared before the store bindings: React runs effects in order, so the generation moves
  // before any store reacts to the same status change.
  useEffect(() => roomosLifecycle?.subscribeStatus(() => shared.invalidate()), [shared])
  const home = useMemo(() => new HomeStore(apis.home, scopeKey ? scopeKey.split(' ') : [], resolved, roomosLifecycle?.snapshot ?? 'reconnecting'), [apis, scopeKey, resolved])
  const lights = useMemo(() => new LightsStore(apis.lights, scopeKey ? scopeKey.split(' ') : [], resolved, roomosLifecycle?.snapshot ?? 'reconnecting'), [apis, scopeKey, resolved])
  const [devices] = useState(() => new DevicesStore({ devices: api.devices }, roomosLifecycle?.snapshot ?? 'reconnecting'))
  const homeSnapshot = useSyncExternalStore(home.subscribe, home.getSnapshot)
  const lightsSnapshot = useSyncExternalStore(lights.subscribe, lights.getSnapshot)
  const devicesSnapshot = useSyncExternalStore(devices.subscribe, devices.getSnapshot)
  const homeLifecycle = useMemo<LifecycleStore>(() => ({ load: () => home.load(), setConnection: (s) => home.setConnection(s), handleEvent: (e) => home.handleEvent(e) }), [home])
  useStoreLifecycle(homeLifecycle, () => home.getSnapshot().loaded)
  useStoreLifecycle(lights, () => Boolean(lights.getSnapshot().lights))
  const scopes = scopeKey.split(' ')
  const deviceAccess = scopes.includes('devices:read') || scopes.includes('system:read')
  useEffect(() => {
    if (!deviceAccess) return undefined
    if (!roomosLifecycle) { void devices.load(); return undefined }
    return bindDevicesLifecycle(devices, roomosLifecycle)
  }, [deviceAccess, devices])
  return { home, lights, homeSnapshot, lightsSnapshot, devicesSnapshot, deviceAccess }
}

const WAKE_TEXT: Record<WakeLockStatus, string> = {
  unsupported: 'Not supported by this browser', off: 'The screen may turn off normally', requesting: 'Asking the browser…',
  active: 'Screen kept on while the panel is visible', released: 'Released by Android · retried when visible', denied: 'Refused by the browser (battery saver?)',
}

export function PanelSettingsSheet({ settings, change, ambient, changeAmbient, wake, onClose }: { settings: PanelSettings; change: (patch: Partial<PanelSettings>) => void; ambient: AmbientSettings; changeAmbient: (patch: Partial<AmbientSettings>) => void; wake: WakeLockStatus; onClose: () => void }) {
  const [fullscreen, setFullscreen] = useState<string | null>(null)
  const segment = <T extends string | number>(label: string, value: T, options: Array<[T, string]>, onPick: (value: T) => void) => <fieldset className="panel-segment"><legend>{label}</legend>
    <div role="group">{options.map(([option, text]) => <button key={String(option)} type="button" aria-pressed={value === option} className={value === option ? 'is-active' : ''} onClick={() => onPick(option)}>{text}</button>)}</div></fieldset>
  const toggle = (label: string, detail: string, value: boolean, onToggle: () => void, disabled = false) => <button type="button" className={`panel-toggle ${value ? 'is-on' : ''}`} role="switch" aria-checked={value} disabled={disabled} onClick={onToggle}>
    <span><strong>{label}</strong><small>{detail}</small></span><i aria-hidden="true" /></button>
  const toggleFullscreen = async () => {
    if (document.fullscreenElement) { await exitPanelFullscreen(); setFullscreen('Full screen off'); return }
    const result = await enterPanelFullscreen(settings.orientation)
    setFullscreen(!result.fullscreen ? 'Full screen is not available here' : settings.orientation === 'auto' ? 'Full screen on'
      : result.orientationLocked ? `Full screen · ${settings.orientation} locked` : 'Full screen on · orientation lock refused')
  }
  return <div className="panel-settings">
    <header><h2>Panel settings</h2><button type="button" className="sheet-close" onClick={onClose}>Close</button></header>
    {segment('Theme', settings.theme, [['day', 'Day'], ['night', 'Night'], ['auto', 'Auto 21–07']], (theme) => change({ theme }))}
    {toggle('Keep screen on', WAKE_TEXT[wake], settings.wake, () => change({ wake: !settings.wake }), wake === 'unsupported')}
    {toggle('Open RoomOS in panel mode', 'Stored on this device only', settings.launch, () => change({ launch: !settings.launch }))}
    <section className="ambient-settings" aria-label="Ambient Screen settings">
      <h3>Ambient Screen</h3>
      {toggle('Ambient Screen', 'Saat ekranı bu Phone 2 tarayıcı profilinde saklanır', ambient.enabled, () => changeAmbient({ enabled: !ambient.enabled }))}
      {segment('Open after', ambient.idleMinutes, AMBIENT_IDLE_CHOICES.map((minutes) => [minutes, `${minutes} min`]), (idleMinutes) => changeAmbient({ idleMinutes }))}
      {segment('Clock', ambient.clock, [['large', 'Large'], ['compact', 'Compact']], (clock) => changeAmbient({ clock }))}
      {toggle('Show date', 'Saatin altında gün ve tarih', ambient.date, () => changeAmbient({ date: !ambient.date }))}
      {segment('Appearance', ambient.tone, [['dark', 'Dark'], ['very-dark', 'Very dark']], (tone) => changeAmbient({ tone }))}
      <fieldset className="ambient-card-settings"><legend>Information cards</legend>{AMBIENT_CARDS.map((card) => {
        const labels = { lights: 'Light status', system: 'System status', routines: 'Active routines' }
        const index = ambient.cards.indexOf(card)
        const move = (offset: number) => {
          const cards = [...ambient.cards]
          const other = index + offset
          if (index < 0 || other < 0 || other >= cards.length) return
          ;[cards[index], cards[other]] = [cards[other], cards[index]]
          changeAmbient({ cards })
        }
        return <div className="ambient-card-setting" key={card}><button type="button" role="switch" aria-checked={index >= 0} onClick={() => changeAmbient({ cards: index >= 0 ? ambient.cards.filter((item) => item !== card) : [...ambient.cards, card] })}>{labels[card]}</button><button type="button" aria-label={`Move ${labels[card]} up`} disabled={index <= 0} onClick={() => move(-1)}>↑</button><button type="button" aria-label={`Move ${labels[card]} down`} disabled={index < 0 || index === ambient.cards.length - 1} onClick={() => move(1)}>↓</button></div>
      })}</fieldset>
    </section>
    {segment('Orientation in full screen', settings.orientation, [['auto', 'Auto'], ['portrait', 'Portrait'], ['landscape', 'Landscape']], (orientation) => change({ orientation }))}
    <div className="panel-fullscreen">
      <button type="button" onClick={() => void toggleFullscreen()}><Icon name="system" />Full screen on / off</button>
      {fullscreen && <small role="status">{fullscreen}</small>}
    </div>
    <p className="panel-limits">Android decides screen brightness and when the screen sleeps (battery saver, power button, timeouts). RoomOS cannot change brightness or guarantee the screen stays on.</p>
  </div>
}

export function PanelPage() {
  const navigate = useNavigate()
  const { home, lights, homeSnapshot, lightsSnapshot, devicesSnapshot, deviceAccess } = usePanelStores()
  const [settings, change] = useSettings()
  const [ambientSettings, changeAmbient] = useAmbientSettings(settings.saverMinutes)
  const [layout, setLayout] = useState<PanelLayout>(() => structuredClone(DEFAULT_PANEL_LAYOUT))
  const wake = useWakeLock(settings.wake)
  const now = useMinuteClock()
  const online = useOnline()
  const [saver, setSaver] = useScreenSaver(ambientSettings.enabled ? ambientSettings.idleMinutes : 0)
  const [sheet, setSheet] = useState<'light' | 'settings' | 'layout' | null>(null)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const night = resolveNight(settings.theme, now)
  const clock = formatLocalClock(now)
  const ambientClock = formatAmbientClock(now)
  const health = healthSummary(homeSnapshot.resources.systemHealth.data, homeSnapshot.resources.systemHealth.status === 'error')
  const routines = panelRoutines(homeSnapshot.resources.routines.data)
  const visibleDevices = panelDevices(devicesSnapshot.devices)
  const summary = summarizeMainLights(lightsSnapshot.lights)
  const ambient = lightsSnapshot.lights?.find((light) => light.id === AMBIENT_ID) ?? null
  const live = lightsSnapshot.connection === 'connected' && lightsSnapshot.trusted && lightsSnapshot.control?.home_assistant === 'connected'

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    if (sheet && !dialog.open) dialog.showModal()
    if (!sheet && dialog.open) dialog.close()
  }, [sheet])
  useEffect(() => {
    let active = true
    void api.panelLayout().then((response) => { if (active) setLayout(normalizePanelLayout(response.layout)) }).catch(() => undefined)
    return () => { active = false }
  }, [])
  useEffect(() => () => { void exitPanelFullscreen() }, [])

  const lightActions: LightsActions = {
    can: (light, action) => lights.can(light, action), canAllOn: lights.canAllOn(), canAllOff: lights.canAllOff(), reason: lights.page().reason,
    power: (light, on) => { void lights.power(light, on) }, brightness: (light, value) => { void lights.brightness(light, value) },
    color: (light, rgb) => { void lights.color(light, rgb) }, temperature: (light, kelvin) => { void lights.temperature(light, kelvin) },
    effect: (light, effect) => { void lights.effect(light, effect) }, allOn: () => { void lights.allOn() }, allOff: () => { void lights.allOff() },
    dismiss: () => lights.dismiss(),
  }
  const mainActions: MainLightsActions = {
    can: (action) => lights.canMain(action),
    power: (on) => { void lights.mainPower(on) }, brightness: (percent) => { void lights.mainBrightness(percent) }, temperature: (kelvin) => { void lights.mainTemperature(kelvin) },
  }
  const connectionLabel = !online ? 'No network' : homeSnapshot.connection === 'connected' ? 'Live' : homeSnapshot.connection === 'offline' ? 'Offline' : 'Reconnecting'
  const control = home.control()
  const catalog = homeSnapshot.resources.scenes.data
  const room = roomState(homeSnapshot.resources.lights.data, catalog)
  const sceneTitle = room.mode.kind === 'scene' ? room.mode.name : room.mode.kind === 'off' ? 'Tümü kapalı' : room.mode.kind === 'custom' ? 'Özel ışık' : ''
  const sceneCatalog = [...panelScenes(), { id: 'all_off' as SceneId, label: 'Tümü kapalı', hint: '', icon: 'power' as const }]
  const scenes = layout.scene_order.map((id) => sceneCatalog.find((tile) => tile.id === id)!).map((tile) => ({
    id: tile.id, label: tile.label, icon: tile.icon,
    active: catalog?.active_scene === tile.id,
    pending: homeSnapshot.pending?.kind === 'scene' && homeSnapshot.pending.id === tile.id,
    enabled: !homeSnapshot.pending && canActivate(control, catalog, tile.id),
  }))
  const sceneFeedback = homeSnapshot.feedback
  const alerts = homeSnapshot.resources.alerts.data

  const saveLayout = async (next: PanelLayout) => {
    const response = await api.savePanelLayout(next)
    setLayout(normalizePanelLayout(response.layout))
  }
  const resetLayout = async () => {
    const response = await api.resetPanelLayout()
    setLayout(normalizePanelLayout(response.layout))
  }
  const cards: Record<PanelCardId, ReactNode> = {
    main_lights: <PanelMainLights summary={summary} snapshot={lightsSnapshot} live={live} actions={mainActions} onDismiss={() => lights.dismiss()} />,
    ambient: <PanelAmbient light={ambient} snapshot={lightsSnapshot} actions={lightActions} onMore={() => setSheet('light')} />,
    scenes: <><PanelScenes scenes={scenes} title={sceneTitle} onActivate={(id) => { void home.activateScene(id) }} />
      {sceneFeedback && <div className={`panel-feedback is-${sceneFeedback.tone} panel-scene-feedback`} role="status"><span><strong>{sceneFeedback.title}</strong>{sceneFeedback.detail && <small>{sceneFeedback.detail}</small>}</span>{sceneFeedback.tone !== 'pending' && <button onClick={() => home.dismissFeedback()} aria-label="Dismiss result">×</button>}</div>}</>,
    routines: <PanelRoutines routines={routines} status={homeSnapshot.resources.routines.status} />,
    system: <PanelSystem devices={visibleDevices} access={deviceAccess} status={devicesSnapshot.status} now={now.getTime()} />,
  }

  return <section className={`panel-page ${night ? 'is-night' : ''} ${layout.density === 'compact' ? 'is-compact' : ''}`} data-panel-target="phone2" data-theme={night ? 'night' : 'day'}>
    <header className="panel-header">
      {layout.header.clock && <time className="panel-clock" dateTime={now.toISOString()}><strong>{clock.time}</strong>{layout.header.date && <span>{clock.date}</span>}</time>}
      <div className="panel-header-status">
        {layout.header.connection && <StatusPill status={!online ? 'offline' : homeSnapshot.connection === 'connected' ? 'online' : homeSnapshot.connection === 'offline' ? 'offline' : 'stale'} label={connectionLabel} />}
        <PanelAlertIndicator alerts={alerts} status={homeSnapshot.resources.alerts.status} />
        {layout.header.health && <Link to="/system" className={`panel-health is-${health.tone}`} aria-label={`RoomOS: ${health.label}. System details`}><span className={`panel-dot is-${health.tone}`} aria-hidden="true" /><span>{health.tone === 'healthy' ? 'OK' : health.tone === 'unknown' ? '—' : 'Dikkat'}</span></Link>}
      </div>
    </header>
    <main className="panel-content">
      {!online && <p className="panel-stale is-offline" role="status"><Icon name="wifi" />Bu telefonda ağ yok. Gösterilen her şey son bilinen durum.</p>}
      {online && !live && lightsSnapshot.lights && <p className="panel-stale" role="status"><Icon name="alert" />Son bilinen durum · RoomOS canlı durumu doğrulayana kadar kontroller duraklatıldı.</p>}
      <PanelCriticalStrip alerts={alerts} />
      {layout.cards.filter((id) => !layout.hidden.includes(id)).map((id) => <div className={`panel-card-slot is-${id}`} data-panel-card={id} key={id}>{cards[id]}</div>)}
    </main>
    <nav className="panel-dock" aria-label="Panel">
      <button type="button" onClick={() => setSaver(true)} aria-label="Show clock only"><Icon name="clock" /><span>Saat</span></button>
      <Link to="/lights" aria-label="All light controls"><Icon name="lights" /><span>Işıklar</span></Link>
      <button type="button" onClick={() => setSheet('settings')} aria-label="Panel settings"><Icon name="edit" /><span>Ayarlar</span></button>
      <button type="button" onClick={() => setSheet('layout')} aria-label="Düzeni düzenle"><Icon name="routines" /><span>Düzenle</span></button>
      <button type="button" onClick={() => navigate('/home')} aria-label="Exit panel mode"><Icon name="home" /><span>Çıkış</span></button>
    </nav>
    <dialog ref={dialogRef} className={`light-sheet panel-sheet ${sheet === 'layout' ? 'is-layout-editor' : ''}`} onClose={() => setSheet(null)} aria-label={sheet === 'settings' ? 'Panel settings' : sheet === 'layout' ? 'Panel düzeni' : ambient ? displayName(ambient) : 'Light'}>
      {sheet === 'light' && ambient && <LightDetail key={ambient.id} light={ambient} snapshot={lightsSnapshot} actions={lightActions} onClose={() => setSheet(null)} />}
      {sheet === 'settings' && <PanelSettingsSheet settings={settings} change={change} ambient={ambientSettings} changeAmbient={changeAmbient} wake={wake} onClose={() => setSheet(null)} />}
      {sheet === 'layout' && <PanelLayoutEditor layout={layout} onSave={saveLayout} onReset={resetLayout} onClose={() => setSheet(null)} />}
    </dialog>
    {saver && <AmbientScreen settings={ambientSettings} time={ambientClock.time} date={ambientClock.date}
      main={summary ? mainLightsStatusLabel(summary) : null} ambient={ambient ? stateLine(ambient) : null}
      system={visibleDevices.filter((device) => device.id === 'primary' || device.id === 'secondary').map((device) => `${device.id === 'primary' ? 'Primary' : 'Secondary'} ${panelDeviceTone(device) === 'healthy' ? 'sağlıklı' : panelDeviceTone(device) === 'degraded' ? 'sorunlu' : panelDeviceTone(device) === 'offline' ? 'erişilemiyor' : 'durum belirsiz'}`).join(' · ') || null}
      routine={routines[0]?.name ?? null} critical={alerts?.active?.filter((item) => item.severity === 'critical').length ?? 0}
      live={online && homeSnapshot.connection === 'connected' && lightsSnapshot.connection === 'connected' && lightsSnapshot.trusted}
      onDismiss={() => setSaver(false)} />}
    <div className="sr-only" aria-live="polite" aria-atomic="true">{homeSnapshot.announcement && <span key={homeSnapshot.announcement.id}>{homeSnapshot.announcement.text}</span>}{lightsSnapshot.announcement && <span key={`l${lightsSnapshot.announcement.id}`}>{lightsSnapshot.announcement.text}</span>}</div>
  </section>
}

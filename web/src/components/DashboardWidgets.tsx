import { Component, memo, useEffect, useRef, useState, type CSSProperties, type ErrorInfo, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { sourceLabel } from '../alert-insight'
import { useNow } from '../devices-hooks'
import { dashboardDevices, deviceRow, dnsView, serviceRows, alertSummary, overviewView, performanceOptions, roomLightsLabel, type Tone, type DeviceRowView } from '../dashboard-state'
import type { PerformanceChoice, WidgetSize } from '../dashboard-layout'
import { roomosLifecycle } from '../lifecycle'
import { canActivate, type ControlState, type SceneId } from '../home-state'
import type { HomeSnapshot } from '../home-store'
import type { LightsSnapshot } from '../lights-store'
import { displayName, previewColor, stateLine } from '../lights-state'
import type { MainLightsSummary } from '../main-lights'
import { panelScenes } from '../panel-state'
import { relativeTime } from '../devices-state'
import type { SystemSnapshot } from '../system-store'
import type { DevicesSnapshot } from '../devices-store'
import type { ActionGuard } from '../routines-state'
import type { ConnectionStatus, Light, Routine } from '../types'
import type { MetricHistoryData } from './MetricHistory'
import { Icon, type IconName } from './Icons'
import { BrightnessSlider, ColourControls, KelvinSlider, type LightsActions } from '../pages/Lights'
import { GroupSlider, type MainLightsActions } from '../pages/PanelCards'

/* Phase 7L — Dashboard 2.0 widgets. Every widget renders from an existing store snapshot
   (HomeStore, LightsStore, DevicesStore, SystemStore) over the one shared WebSocket. Only the
   Performance widget reads anything itself, and only while it is on screen. */

export function ToneDot({ tone, label }: { tone: Tone; label?: string }) {
  return <span className={`dash-dot is-${tone}`} role={label ? 'img' : undefined} aria-label={label} aria-hidden={label ? undefined : true} />
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return <div className="dash-skeleton" aria-hidden="true">{Array.from({ length: lines }, (_, index) => <span key={index} style={{ '--w': `${92 - index * 17}%` } as CSSProperties} />)}</div>
}

export function WidgetMessage({ icon = 'alert', title, detail, action }: { icon?: IconName; title: string; detail?: string; action?: ReactNode }) {
  return <div className="dash-message"><Icon name={icon} /><div><strong>{title}</strong>{detail && <span>{detail}</span>}</div>{action}</div>
}

/** One widget failing to render never takes the dashboard down; Retry remounts only that widget. */
export class WidgetBoundary extends Component<{ title: string; children: ReactNode }, { failed: boolean; attempt: number }> {
  state = { failed: false, attempt: 0 }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error(`Dashboard widget "${this.props.title}" failed`, error, info.componentStack) }
  render() {
    if (this.state.failed) {
      return <WidgetMessage title={`${this.props.title} could not be shown`} detail="The rest of the dashboard keeps working."
        action={<button type="button" className="dash-button" onClick={() => this.setState((state) => ({ failed: false, attempt: state.attempt + 1 }))}>Retry</button>} />
    }
    return <div className="dash-widget-content" key={this.state.attempt}>{this.props.children}</div>
  }
}

export type FreshnessBadge = 'live' | 'stale' | 'loading' | 'error' | null

export function WidgetHeader({ title, icon, badge, to, toLabel, children }: { title: string; icon: IconName; badge: FreshnessBadge; to?: string; toLabel?: string; children?: ReactNode }) {
  return <header className="dash-widget-head">
    <span className="dash-widget-icon" aria-hidden="true"><Icon name={icon} /></span>
    <h2>{title}</h2>
    {badge && badge !== 'live' && <span className={`dash-badge is-${badge}`}>{badge === 'stale' ? 'Last known' : badge === 'loading' ? 'Loading' : 'Unavailable'}</span>}
    {children}
    {to && <Link className="dash-widget-link" to={to} aria-label={toLabel ?? `Open ${title}`}><Icon name="chevron" /></Link>}
  </header>
}

/* ---------- System overview ---------- */

export const OverviewWidget = memo(function OverviewWidget({ home, devices, devicesAllowed, system, connection }: {
  home: HomeSnapshot; devices: DevicesSnapshot; devicesAllowed: boolean; system: SystemSnapshot; connection: ConnectionStatus
}) {
  const now = useNow(30_000)
  const view = overviewView({
    connection,
    healthReport: home.resources.systemHealth.data, healthStatus: home.resources.systemHealth.status,
    devices: devices.devices, devicesTrusted: devices.trusted && devices.status === 'ready', devicesAllowed: devicesAllowed && devices.status !== 'forbidden',
    alerts: home.resources.alerts.data, alertsAllowed: home.resources.alerts.status !== 'forbidden',
    system: system.data, systemAllowed: system.status !== 'forbidden', systemLive: system.trusted && connection === 'connected',
    now,
  })
  return <section className={`dash-overview is-${view.tone}`} aria-labelledby="dash-overview-title">
    <div className="dash-overview-head">
      <ToneDot tone={view.tone} />
      <div><p className="eyebrow">System overview</p><h2 id="dash-overview-title">{view.headline}</h2></div>
    </div>
    <div className="dash-overview-tiles">{view.tiles.map((tile) => <Link key={tile.id} to={tile.to} className={`dash-tile is-${tile.tone}`} aria-label={`${tile.label}: ${tile.value}, ${tile.detail}`}>
      <span className="dash-tile-label"><ToneDot tone={tile.tone} />{tile.label}</span>
      <strong>{tile.value}</strong>
      <small>{tile.detail}</small>
    </Link>)}</div>
  </section>
})

/* ---------- Lights ---------- */

export const LightsWidget = memo(function LightsWidget({ snapshot, summary, ambient, live, main, light, onDismiss }: {
  snapshot: LightsSnapshot; summary: MainLightsSummary | null; ambient: Light | null; live: boolean
  main: MainLightsActions; light: LightsActions; onDismiss: () => void
}) {
  const [stripOpen, setStripOpen] = useState(false)
  const loading = !snapshot.lights && snapshot.lightsStatus === 'loading'
  if (loading) return <><WidgetHeader title="Lights" icon="bulb" badge="loading" to="/lights" /><Skeleton lines={4} /></>
  if (!snapshot.lights) return <><WidgetHeader title="Lights" icon="bulb" badge="error" to="/lights" /><WidgetMessage title="Lights could not be loaded" detail="RoomOS will retry when the connection is live." /></>
  const mainState = summary?.power ?? 'unknown'
  const mainPending = snapshot.pending?.lightId === null && snapshot.pending.action.startsWith('main_')
  const mainFeedback = snapshot.feedback?.scope === 'main' ? snapshot.feedback.value : null
  const stripFeedback = ambient && snapshot.feedback?.scope === ambient.id ? snapshot.feedback.value : null
  const stripOn = Boolean(ambient?.available && ambient.on === true)
  const caps = ambient?.capabilities
  return <>
    <WidgetHeader title="Lights" icon="bulb" badge={live ? 'live' : 'stale'} to="/lights" toLabel="Open all light controls" />
    {!live && <p className="dash-note">{light.reason ?? 'Last known state · controls paused until RoomOS is live.'}</p>}
    <div className={`dash-light-group is-${mainState}`} data-group="main_room_lights">
      <div className="dash-light-row">
        <span className="dash-light-meter" aria-hidden="true">{summary?.members.map((member) => <i key={member.id} className={!member.available ? 'is-unavailable' : member.on === true ? 'is-on' : ''} />)}</span>
        <div className="dash-light-text"><strong>Room lights</strong><span>{mainPending ? 'Working…' : summary ? roomLightsLabel(summary) : 'State unknown'}</span><small>4 × L530 · one group</small></div>
        <button type="button" className={`dash-power ${mainState === 'on' ? 'is-on' : mainState === 'partial' ? 'is-partial' : ''}`} disabled={!main.can('power')}
          aria-pressed={mainState === 'on' ? true : mainState === 'partial' ? 'mixed' : false} aria-label={mainState === 'on' ? 'Turn room lights off' : 'Turn room lights on'}
          onClick={() => main.power(mainState !== 'on')}><Icon name="power" /></button>
      </div>
      {summary?.power === 'on' && <div className="dash-light-sliders">
        <GroupSlider id="dash-main-brightness" label="Brightness" value={summary.brightness} unit="%" min={1} max={100} step={1} enabled={main.can('brightness')} onCommit={main.brightness} />
        <GroupSlider id="dash-main-kelvin" label="White temperature" value={summary.kelvin} unit=" K" min={summary.minKelvin} max={summary.maxKelvin} step={50} enabled={main.can('temperature')} onCommit={main.temperature} />
      </div>}
      {summary?.power === 'partial' && <p className="dash-note">Turn all four on to set a shared brightness and temperature.</p>}
      {mainFeedback && mainFeedback.tone !== 'pending' && <Feedback value={mainFeedback} onDismiss={onDismiss} />}
    </div>
    {ambient && <div className={`dash-light-group is-${stripOn ? 'on' : 'off'}`} data-light={ambient.id}>
      <div className="dash-light-row">
        <span className="dash-light-swatch" style={{ '--preview': previewColor(ambient) ?? 'transparent' } as CSSProperties} aria-hidden="true" />
        <div className="dash-light-text"><strong>{displayName(ambient)}</strong><span>{stateLine(ambient)}</span><small>L920 strip</small></div>
        {stripOn && <button type="button" className="dash-icon-button" aria-expanded={stripOpen} aria-controls="dash-strip-controls" onClick={() => setStripOpen((open) => !open)}>{stripOpen ? 'Less' : 'Adjust'}</button>}
        {ambient.available && <button type="button" className={`dash-power ${stripOn ? 'is-on' : ''}`} disabled={!light.can(ambient, 'power')} aria-pressed={stripOn}
          aria-label={stripOn ? `Turn ${displayName(ambient)} off` : `Turn ${displayName(ambient)} on`} onClick={() => light.power(ambient, !stripOn)}><Icon name="power" /></button>}
      </div>
      {stripOn && stripOpen && caps && <div id="dash-strip-controls" className="dash-light-sliders">
        {caps.brightness && ambient.brightness !== null && <BrightnessSlider light={ambient} enabled={light.can(ambient, 'brightness')} onCommit={(value) => light.brightness(ambient, value)} />}
        {caps.temperature && <KelvinSlider light={ambient} enabled={light.can(ambient, 'temperature')} onCommit={(value) => light.temperature(ambient, value)} />}
        {caps.color && <ColourControls light={ambient} enabled={light.can(ambient, 'color')} onCommit={(rgb) => light.color(ambient, rgb)} />}
        {caps.effects && ambient.effects.length > 0 && <Link className="dash-inline-link" to="/lights">Effects on the Lights page <Icon name="chevron" /></Link>}
      </div>}
      {stripFeedback && stripFeedback.tone !== 'pending' && <Feedback value={stripFeedback} onDismiss={onDismiss} />}
    </div>}
  </>
})

function Feedback({ value, onDismiss }: { value: { tone: string; title: string; detail?: string }; onDismiss: () => void }) {
  return <div className={`dash-feedback is-${value.tone}`} role={value.tone === 'error' || value.tone === 'partial' ? 'alert' : 'status'}>
    <span><strong>{value.title}</strong>{value.detail && <small>{value.detail}</small>}</span>
    <button type="button" onClick={onDismiss} aria-label="Dismiss result">×</button>
  </div>
}

/* ---------- Alerts ---------- */

export const AlertsWidget = memo(function AlertsWidget({ snapshot, connection, size }: { snapshot: HomeSnapshot; connection: ConnectionStatus; size: WidgetSize }) {
  const now = useNow(60_000)
  const resource = snapshot.resources.alerts
  if (!resource.data && (resource.status === 'loading' || resource.status === 'idle')) return <><WidgetHeader title="Alerts" icon="alert" badge="loading" to="/notifications" /><Skeleton /></>
  if (!resource.data) return <><WidgetHeader title="Alerts" icon="alert" badge="error" to="/notifications" /><WidgetMessage title="Alert status unavailable" detail="This is not the same as no alerts." /></>
  const summary = alertSummary(resource.data, size === 'sm' ? 2 : size === 'md' ? 3 : 6)
  const live = connection === 'connected' && snapshot.trusted && resource.status === 'ready'
  return <>
    <WidgetHeader title="Alerts" icon="alert" badge={live ? 'live' : 'stale'} to="/notifications" toLabel="Open Notification Center" />
    <div className="dash-alert-counts" role="group" aria-label="Active alerts by severity">
      <span className={`dash-count is-critical ${summary.critical ? 'is-set' : ''}`}><strong>{summary.critical}</strong>Critical</span>
      <span className={`dash-count is-warning ${summary.warning ? 'is-set' : ''}`}><strong>{summary.warning}</strong>Warning</span>
      {summary.info > 0 && <span className="dash-count is-info is-set"><strong>{summary.info}</strong>Info</span>}
    </div>
    {summary.total === 0
      ? <WidgetMessage icon="pulse" title={live ? 'No active alerts' : 'No active alerts when last checked'} detail={live ? 'RoomOS raises an alert only for abnormal, sustained conditions.' : undefined} />
      : <ul className="dash-alert-list">{summary.top.map((alert) => <li key={alert.id}>
          <Link to={`/notifications?alert=${encodeURIComponent(alert.id)}`} className={`dash-alert is-${alert.severity}`}>
            <ToneDot tone={alert.severity === 'critical' ? 'offline' : alert.severity === 'warning' ? 'degraded' : 'unknown'} label={alert.severity} />
            <span><strong>{alert.title}</strong><small>{sourceLabel(alert.source)} · since {relativeTime(alert.started_at, now)}</small></span>
            <Icon name="chevron" />
          </Link></li>)}
        </ul>}
    {summary.total > summary.top.length && <Link className="dash-inline-link" to="/notifications">+{summary.total - summary.top.length} more in Notification Center <Icon name="chevron" /></Link>}
    {summary.latest && <p className="dash-meta">Last change {relativeTime(summary.latest, now)}</p>}
  </>
})

/* ---------- Devices ---------- */

function MetricChip({ metric }: { metric: DeviceRowView['metrics'][number] }) {
  return <span className={`dash-metric ${metric.current ? '' : 'is-stale'} ${metric.high ? 'is-high' : ''}`} title={metric.current ? undefined : 'Last reported value · not current'}
    style={metric.percent === null ? undefined : { '--fill': `${metric.percent}%` } as CSSProperties}>
    <small>{metric.label}</small><strong>{metric.text}</strong>{metric.percent !== null && <i aria-hidden="true" />}
  </span>
}

export const DevicesWidget = memo(function DevicesWidget({ snapshot }: { snapshot: DevicesSnapshot }) {
  const now = useNow(30_000)
  if (snapshot.status === 'loading' && !snapshot.devices.length) return <><WidgetHeader title="Devices" icon="devices" badge="loading" to="/devices" /><Skeleton lines={4} /></>
  if (!snapshot.devices.length) return <><WidgetHeader title="Devices" icon="devices" badge={snapshot.status === 'error' ? 'error' : null} to="/devices" />
    <WidgetMessage title={snapshot.status === 'error' ? 'Devices could not be loaded' : 'No devices with telemetry yet'} /></>
  const rows = dashboardDevices(snapshot.devices).map((device) => deviceRow(device, snapshot.trusted, now))
  return <>
    <WidgetHeader title="Devices" icon="devices" badge={snapshot.trusted ? 'live' : 'stale'} to="/devices" toLabel="Open Devices" />
    <ul className="dash-device-list">{rows.map((row) => <li key={row.id}>
      <Link to={`/devices/${encodeURIComponent(row.id)}`} className={`dash-device is-${row.tone}`} aria-label={`${row.name}: ${row.status}. Open details`}>
        <span className="dash-device-name"><ToneDot tone={row.tone} /><strong>{row.short}</strong><small>{row.status}{row.connection !== 'online' && row.lastSeen !== 'never' ? ` · ${row.lastSeen}` : ''}</small></span>
        {row.metrics.length > 0 ? <span className="dash-device-metrics">{row.metrics.map((metric) => <MetricChip key={metric.key} metric={metric} />)}</span>
          : <span className="dash-device-metrics is-empty">{row.connection === 'offline' ? 'No live values while off' : 'No current values'}</span>}
      </Link></li>)}</ul>
    <p className="dash-meta">{snapshot.trusted ? 'Values update live' : 'Live updates paused · values are last known'}</p>
  </>
})

/* ---------- Services & DNS ---------- */

export const ServicesWidget = memo(function ServicesWidget({ system, home, connection }: { system: SystemSnapshot; home: HomeSnapshot; connection: ConnectionStatus }) {
  const now = useNow(30_000)
  const live = connection === 'connected' && system.trusted
  if (system.status === 'loading' && !system.data) return <><WidgetHeader title="Services & DNS" icon="wifi" badge="loading" to="/system?tab=Services" /><Skeleton lines={5} /></>
  const dns = dnsView(system.data, live, now)
  const rows = serviceRows({ system: system.data, control: home.resources.control.data, connection, live, now })
  return <>
    <WidgetHeader title="Services & DNS" icon="wifi" badge={system.status === 'error' && !system.data ? 'error' : live ? 'live' : 'stale'} to="/system?tab=Network%20%26%20DNS" toLabel="Open Network and DNS" />
    <div className={`dash-dns is-${dns.tone}`}><ToneDot tone={dns.tone} /><strong>{dns.headline}</strong>{dns.checked && <small>probe {dns.checked}</small>}</div>
    {dns.problem && <p className="dash-problem" role="alert"><Icon name="alert" />{dns.problem}</p>}
    {system.status === 'error' && !system.data && <WidgetMessage title="Service status unavailable" detail="Shown again as soon as RoomOS answers." />}
    <ul className="dash-service-list">{rows.map((row) => <li key={row.id} className={`is-${row.tone}`}>
      <ToneDot tone={row.tone} /><span><strong>{row.name}</strong>{row.detail && <small>{row.detail}</small>}</span><em>{row.status}</em>
    </li>)}</ul>
  </>
})

/* ---------- Performance (lazy history) ---------- */

type HistoryResult = { token: string; key: string; status: 'ready' | 'error'; data: MetricHistoryData | null }

/** Counts live connections so a reconnect re-reads history once; the listener runs outside render. */
function useConnectionEpoch() {
  const [epoch, setEpoch] = useState(0)
  useEffect(() => roomosLifecycle?.subscribeStatus((status) => { if (status === 'connected') setEpoch((value) => value + 1) }), [])
  return epoch
}

function useInView<T extends Element>() {
  const ref = useRef<T | null>(null)
  const [inView, setInView] = useState(false)
  useEffect(() => {
    const element = ref.current
    if (!element || typeof IntersectionObserver === 'undefined') { setInView(true); return undefined }
    const observer = new IntersectionObserver((entries) => setInView(entries.some((entry) => entry.isIntersecting)), { rootMargin: '120px' })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  return [ref, inView] as const
}

const METRIC_LABEL: Record<PerformanceChoice['metric'], string> = { cpu_usage: 'CPU', ram_usage: 'RAM', disk_usage: 'Disk' }

function Sparkline({ data, label }: { data: MetricHistoryData; label: string }) {
  const points = data.points
  const values = points.flatMap((point) => point.avg === null ? [] : [point.avg])
  if (!values.length) return <WidgetMessage icon="chart" title="No samples in the last hour" detail="Gaps are missing observations, never zeroes." />
  const low = Math.min(...values), high = Math.max(...values)
  const top = Math.max(100, high), bottom = 0 // percentages: fixed 0–100 scale keeps devices comparable
  const segments: string[][] = [[]]
  points.forEach((point, index) => {
    if (point.avg === null) { if (segments.at(-1)!.length) segments.push([]); return }
    const x = (index / Math.max(1, points.length - 1)) * 300
    const y = 90 - ((point.avg - bottom) / (top - bottom)) * 84
    segments.at(-1)!.push(`${x.toFixed(1)},${y.toFixed(1)}`)
  })
  const latest = [...points].reverse().find((point) => point.avg !== null)?.avg ?? null
  return <figure className="dash-spark">
    <svg viewBox="0 0 300 96" preserveAspectRatio="none" role="img" aria-label={`${label}, last hour: low ${low.toFixed(0)}%, high ${high.toFixed(0)}%${latest === null ? '' : `, latest ${latest.toFixed(0)}%`}.`}>
      <line x1="0" x2="300" y1={90 - 0.9 * 84} y2={90 - 0.9 * 84} className="dash-spark-limit" />
      {segments.filter((segment) => segment.length).map((segment, index) => segment.length === 1
        ? <circle key={index} cx={segment[0].split(',')[0]} cy={segment[0].split(',')[1]} r="2.5" />
        : <polyline key={index} points={segment.join(' ')} />)}
    </svg>
    <figcaption><span>Low <strong>{low.toFixed(0)}%</strong></span><span>High <strong>{high.toFixed(0)}%</strong></span>{latest !== null && <span>Latest <strong>{latest.toFixed(0)}%</strong></span>}<span>{values.length}/{points.length} samples</span></figcaption>
  </figure>
}

export const PerformanceWidget = memo(function PerformanceWidget({ devices, choice, onChoice, connection, load = api.metricHistory }: {
  devices: DevicesSnapshot; choice: PerformanceChoice; onChoice: (choice: PerformanceChoice) => void; connection: ConnectionStatus
  load?: typeof api.metricHistory
}) {
  const [ref, inView] = useInView<HTMLDivElement>()
  const options = performanceOptions(devices.devices)
  const device = options.find((option) => option.id === choice.device) ?? options[0]
  const metric = device && device.metrics.includes(choice.metric) ? choice.metric : device?.metrics[0]
  const key = device && metric ? `${device.id}:${metric}` : null
  const [result, setResult] = useState<HistoryResult | null>(null)
  const [reload, setReload] = useState(0)
  const epoch = useConnectionEpoch()
  const live = connection === 'connected'
  // A request is wanted only while on screen and live: once per selection, reconnect or refresh. No timers.
  const wanted = inView && live && key ? `${key}|${reload}|${epoch}` : null
  const loading = wanted !== null && result?.token !== wanted
  useEffect(() => {
    if (!wanted || !key || !device || !metric) return undefined
    let active = true
    load(device.id, metric, '1h').then((data) => { if (active) setResult({ token: wanted, key, status: 'ready', data }) },
      () => { if (active) setResult((current) => ({ token: wanted, key, status: 'error', data: current?.key === key ? current.data : null })) })
    return () => { active = false }
  }, [wanted]) // eslint-disable-line react-hooks/exhaustive-deps -- `wanted` encodes device, metric, reload and connection epoch
  const data = result?.key === key ? result.data : null
  const failed = !loading && result?.key === key && result.status === 'error'
  const label = device && metric ? `${device.name} ${METRIC_LABEL[metric]}` : 'Performance'
  return <div ref={ref} className="dash-performance">
    <WidgetHeader title="Performance" icon="chart" badge={loading && !data ? 'loading' : failed ? 'error' : !live && data ? 'stale' : null} to={device ? `/devices/${encodeURIComponent(device.id)}` : '/devices'} toLabel={device ? `Open ${device.name} history` : 'Open Devices'}>
      <button type="button" className="dash-icon-button" aria-label="Refresh history" disabled={!live || loading} onClick={() => setReload((value) => value + 1)}><Icon name="refresh" /></button>
    </WidgetHeader>
    {!options.length ? (devices.status === 'loading' ? <Skeleton /> : <WidgetMessage icon="chart" title="No device reports history yet" />) : <>
      <div className="dash-perf-controls">
        <label><span className="sr-only">Device</span><select value={device?.id} onChange={(event) => onChoice({ device: event.target.value, metric: metric ?? 'cpu_usage' })}>
          {options.map((option) => <option key={option.id} value={option.id}>{option.name}</option>)}</select></label>
        <div className="dash-segment" role="group" aria-label="Metric">{device?.metrics.map((item) => <button key={item} type="button" aria-pressed={item === metric} onClick={() => onChoice({ device: device.id, metric: item })}>{METRIC_LABEL[item]}</button>)}</div>
      </div>
      {data ? <Sparkline data={data} label={label} />
        : failed ? <WidgetMessage title="History unavailable" action={<button type="button" className="dash-button" onClick={() => setReload((value) => value + 1)}>Retry</button>} />
        : !live ? <WidgetMessage icon="wifi" title="History loads when RoomOS is live" />
        : <Skeleton lines={2} />}
      <p className="dash-meta">Last hour · 1-minute buckets{data?.generated_at ? ` · as of ${new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(new Date(data.generated_at))}` : ''}</p>
    </>}
  </div>
})

/* ---------- Scenes & routines ---------- */

export const RoutinesWidget = memo(function RoutinesWidget({ snapshot, control, favorites, guard, onScene, onRun, onFavorite, onDismiss }: {
  snapshot: HomeSnapshot; control: ControlState; favorites: readonly string[]
  guard: (routine: Routine) => ActionGuard
  onScene: (id: SceneId) => void; onRun: (id: string) => void; onFavorite: (id: string) => void; onDismiss: () => void
}) {
  const scenes = snapshot.resources.scenes
  const routines = snapshot.resources.routines
  const automations = snapshot.resources.automations
  const catalog = scenes.data
  const pending = snapshot.pending
  const sceneTiles = [...panelScenes(), { id: 'all_off' as SceneId, label: 'All off', hint: '', icon: 'power' as const }]
  const ordered = (routines.data?.routines ?? []).slice().sort((a, b) => {
    const fa = favorites.indexOf(a.id), fb = favorites.indexOf(b.id)
    return (fa < 0 ? 1e6 : fa) - (fb < 0 ? 1e6 : fb) || Number(b.enabled) - Number(a.enabled)
  })
  const feedback = snapshot.feedback
  const live = snapshot.connection === 'connected' && snapshot.trusted
  return <>
    <WidgetHeader title="Scenes & routines" icon="routines" badge={scenes.status === 'loading' && !catalog ? 'loading' : live ? 'live' : 'stale'} to="/routines" toLabel="Open Routines" />
    {scenes.status !== 'forbidden' && (catalog
      ? <div className="dash-scenes" role="group" aria-label="Scenes">{sceneTiles.map((tile) => {
          const active = catalog.active_scene === tile.id
          const isPending = pending?.kind === 'scene' && pending.id === tile.id
          return <button key={tile.id} type="button" className={`dash-scene ${active ? 'is-active' : ''}`} aria-pressed={active} aria-busy={isPending || undefined}
            disabled={pending !== null || !canActivate(control, catalog, tile.id)} onClick={() => onScene(tile.id)}>
            <Icon name={tile.icon} /><span>{isPending ? 'Applying…' : tile.label}</span></button>
        })}</div>
      : scenes.status === 'error' ? <WidgetMessage title="Scenes unavailable" /> : <Skeleton lines={2} />)}
    {control.reason && scenes.status !== 'forbidden' && <p className="dash-note">{control.reason}</p>}
    {routines.status !== 'forbidden' && <div className="dash-routines">
      <h3>Routines</h3>
      {automations.data && <p className="dash-meta">{automations.data.automations.filter((item) => item.enabled).length} smart active · {automations.data.automations.length} total</p>}
      {!routines.data ? (routines.status === 'error' ? <WidgetMessage title="Routines unavailable" /> : <Skeleton lines={2} />)
        : !ordered.length ? <p className="dash-meta">No routines yet.</p>
        : <ul>{ordered.map((routine) => {
            const gate = guard(routine)
            const favorite = favorites.includes(routine.id)
            const running = pending?.kind === 'routine' && pending.id === routine.id
            const last = routine.last_result
            return <li key={routine.id} className={routine.enabled ? '' : 'is-off'}>
              <button type="button" className={`dash-star ${favorite ? 'is-on' : ''}`} aria-pressed={favorite} aria-label={`${favorite ? 'Unpin' : 'Pin'} ${routine.name}`} onClick={() => onFavorite(routine.id)}><Icon name="star" /></button>
              <span><strong>{routine.name}</strong><small>{routine.enabled ? 'On' : 'Off'}{last ? ` · last ${last.status === 'success' ? 'ok' : last.status}` : ''}</small></span>
              {gate.visible && <button type="button" className="dash-run" disabled={!gate.ok} title={gate.reason ?? undefined} aria-busy={running || undefined} onClick={() => onRun(routine.id)}>
                <Icon name="play" />{running ? 'Running…' : 'Run'}</button>}
            </li>
          })}</ul>}
    </div>}
    {feedback && feedback.tone !== 'pending' && <Feedback value={feedback} onDismiss={onDismiss} />}
  </>
})

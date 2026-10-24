/* Phase 7L — pure Dashboard 2.0 selectors over existing store snapshots. No fetching, no timers.
   Rule throughout: a value is only presented as current when the live connection is trusted and
   the source says it is fresh; otherwise it is labelled "last known" and never counted healthy. */

import { effectiveConnection, effectiveFreshness, relativeTime } from './devices-state'
import { serviceDashboardStatus, type DashboardStatus } from './home-dashboard'
import { healthSummary, sortAlerts } from './home-state'
import type { MainLightsSummary } from './main-lights'
import type { AlertCatalog, AlertRecord, ConnectionStatus, DeviceDetail, DeviceSensor, DnsResolverStatus, Health, HealthReport, SystemData } from './types'

export type Tone = 'healthy' | 'degraded' | 'offline' | 'unknown' | 'stale'

/* ---------- devices ---------- */

const DEVICE_ORDER = ['primary', 'secondary', 'workstation', 'phone', 'tablet', 'phone_2']
export const DEVICE_SHORT: Record<string, string> = { primary: 'Primary', secondary: 'Secondary', workstation: 'Workstation', phone: 'Phone', tablet: 'Tab S9 FE+', phone_2: 'Phone 2' }

export function dashboardDevices(devices: readonly DeviceDetail[]) {
  const rank = (id: string) => { const index = DEVICE_ORDER.indexOf(id); return index < 0 ? DEVICE_ORDER.length : index }
  return devices.filter((device) => device.sources.some((source) => source.configured))
    .slice().sort((a, b) => rank(a.id) - rank(b.id) || a.name.localeCompare(b.name))
}

export type DeviceMetricView = { key: 'cpu' | 'ram' | 'disk' | 'temp'; label: string; text: string; percent: number | null; current: boolean; high: boolean }
export type DeviceRowView = {
  id: string; name: string; short: string; type: DeviceDetail['type']
  connection: 'online' | 'offline' | 'unknown'; tone: Tone; status: string
  metrics: DeviceMetricView[]; lastSeen: string; current: boolean
}

const METRIC_SENSORS: ReadonlyArray<{ key: DeviceMetricView['key']; id: string; label: string }> = [
  { key: 'cpu', id: 'cpu_usage', label: 'CPU' },
  { key: 'ram', id: 'ram_usage', label: 'RAM' },
  { key: 'disk', id: 'disk_usage', label: 'Disk' },
  { key: 'temp', id: 'cpu_temperature', label: 'Temp' },
]

/** Plausible chip temperature only: agents report 0 °C (or nonsense) when they cannot read a sensor. */
export function validTemperature(sensor: DeviceSensor | undefined): number | null {
  if (!sensor || sensor.support !== 'supported' || sensor.availability !== 'available' || typeof sensor.value !== 'number') return null
  return Number.isFinite(sensor.value) && sensor.value >= 10 && sensor.value <= 125 ? sensor.value : null
}

export function deviceRow(device: DeviceDetail, trusted: boolean, now: number): DeviceRowView {
  const connection = effectiveConnection(device, trusted, now)
  const metrics: DeviceMetricView[] = []
  if (connection !== 'offline') {
    for (const spec of METRIC_SENSORS) {
      const sensor = device.sensors.find((item) => item.id === spec.id)
      if (!sensor || sensor.support === 'unsupported' || sensor.availability !== 'available') continue
      const current = effectiveFreshness(sensor, device, { now, trusted }) === 'fresh'
      if (spec.key === 'temp') {
        const value = validTemperature(sensor)
        if (value === null) continue
        metrics.push({ key: 'temp', label: spec.label, text: `${Math.round(value)}°C`, percent: null, current, high: value >= 80 })
        continue
      }
      if (sensor.support !== 'supported' || typeof sensor.value !== 'number' || !Number.isFinite(sensor.value)) continue
      const percent = Math.max(0, Math.min(100, sensor.value))
      metrics.push({ key: spec.key, label: spec.label, text: `${Math.round(percent)}%`, percent, current, high: percent >= (spec.key === 'disk' ? 85 : 90) })
    }
  }
  const anyStale = metrics.some((metric) => !metric.current)
  const tone: Tone = connection === 'offline' ? 'offline' : connection === 'unknown' ? 'unknown'
    : anyStale || device.telemetry_freshness === 'stale' ? 'stale' : device.health === 'degraded' ? 'degraded' : 'healthy'
  // Phones report only while RoomOS is open: silence is "Not reporting", never "Offline".
  const status = connection === 'online' ? (tone === 'stale' ? 'Online · some values not current' : 'Online')
    : connection === 'offline' ? (device.type === 'windows' ? 'Off or asleep' : 'Offline')
    : device.type === 'android' ? 'Not reporting' : 'State unknown'
  return {
    id: device.id, name: device.name, short: DEVICE_SHORT[device.id] ?? device.name, type: device.type,
    connection, tone, status, metrics, lastSeen: relativeTime(device.last_seen_at, now), current: trusted && connection === 'online' && !anyStale,
  }
}

const HISTORY_METRICS = ['cpu_usage', 'ram_usage', 'disk_usage'] as const
export type HistoryMetric = typeof HISTORY_METRICS[number]

/** Devices and metrics the history API serves: only `supported` sensors are ever recorded. */
export function performanceOptions(devices: readonly DeviceDetail[]) {
  return dashboardDevices(devices).map((device) => ({
    id: device.id, name: device.name,
    metrics: HISTORY_METRICS.filter((metric) => device.sensors.some((sensor) => sensor.id === metric && sensor.support === 'supported')),
  })).filter((option) => option.metrics.length > 0)
}

/* ---------- services & DNS ---------- */

export type ServiceRowView = { id: string; name: string; tone: Tone; status: string; detail: string | null }
export type DnsView = { tone: Tone; headline: string; problem: string | null; checked: string | null }

const RESOLVER_LABEL = { primary: 'Primary', secondary: 'Secondary' } as const

function resolverRow(resolver: DnsResolverStatus, live: boolean, now: number): ServiceRowView {
  const label = RESOLVER_LABEL[resolver.role]
  const tone: Tone = !live ? 'stale' : resolver.status
  const status = !live ? 'Last known' : resolver.status === 'healthy' ? 'Answering' : resolver.status === 'degraded' ? 'Wrong answers' : resolver.status === 'offline' ? 'Not answering' : 'Not checked'
  const latency = resolver.response_time_ms === null ? null : `${resolver.response_time_ms.toFixed(resolver.response_time_ms < 10 ? 1 : 0)} ms`
  const checked = resolver.last_checked ? `checked ${relativeTime(resolver.last_checked, now)}` : null
  return { id: `dns_${resolver.role}`, name: `DNS ${resolver.role} · ${label}`, tone, status, detail: [latency, checked].filter(Boolean).join(' · ') || null }
}

export function dnsView(system: SystemData | null, live: boolean, now: number): DnsView {
  if (!system) return { tone: 'unknown', headline: 'DNS status unavailable', problem: null, checked: null }
  const cluster = system.dns_cluster
  const checked = cluster.last_checked ? relativeTime(cluster.last_checked, now) : null
  if (!live) return { tone: 'stale', headline: 'DNS · last known', problem: null, checked }
  const down = (['primary', 'secondary'] as const).filter((role) => cluster[role].status !== 'healthy' && cluster[role].status !== 'unknown')
  const problem = down.length === 1
    ? `${RESOLVER_LABEL[down[0]]} DNS is ${cluster[down[0]].status === 'offline' ? 'not answering' : 'returning wrong answers'} · only ${RESOLVER_LABEL[down[0] === 'primary' ? 'secondary' : 'primary']} resolves. Redundancy lost.`
    : down.length === 2 ? 'Neither DNS resolver is answering correctly.' : null
  const headline = cluster.status === 'healthy' ? 'DNS redundant · 2/2' : cluster.status === 'degraded' ? 'DNS degraded · 1/2' : cluster.status === 'offline' ? 'DNS offline · 0/2' : 'DNS not checked yet'
  return { tone: cluster.status, headline, problem, checked }
}

export function serviceRows({ system, control, connection, live, now }: {
  system: SystemData | null; control: Health | null; connection: ConnectionStatus; live: boolean; now: number
}): ServiceRowView[] {
  const rows: ServiceRowView[] = []
  rows.push({ id: 'roomos', name: 'RoomOS Core', tone: connection === 'connected' ? 'healthy' : connection === 'offline' ? 'offline' : 'unknown',
    status: connection === 'connected' ? 'Live' : connection === 'offline' ? 'Offline' : 'Reconnecting', detail: null })
  const ha = control?.home_assistant
  rows.push({ id: 'home_assistant', name: 'Home Assistant', tone: !control || connection !== 'connected' ? 'stale' : ha === 'connected' ? 'healthy' : ha === 'disconnected' ? 'offline' : 'unknown',
    status: !control ? 'Unknown' : connection !== 'connected' ? 'Last known' : ha === 'connected' ? 'Connected' : ha === 'disconnected' ? 'Disconnected' : 'Not configured', detail: null })
  // RoomOS has no trusted MQTT broker health source; say so instead of inventing a state.
  rows.push({ id: 'mqtt', name: 'MQTT broker', tone: 'unknown', status: 'Not monitored', detail: 'No RoomOS health source' })
  if (system) {
    rows.push(resolverRow(system.dns_cluster.primary, live, now), resolverRow(system.dns_cluster.secondary, live, now))
    for (const [id, name] of [['frigate', 'Frigate'], ['nextcloud', 'Nextcloud']] as const) {
      const service = system.services.find((item) => item.id === id)
      const status: DashboardStatus = live ? serviceDashboardStatus(service) : 'stale'
      rows.push({ id, name, tone: status, status: status === 'healthy' ? 'Healthy' : status === 'stale' ? 'Last known' : status === 'offline' ? 'Offline' : status === 'degraded' ? 'Degraded' : 'Unknown', detail: null })
    }
  }
  return rows
}

/* ---------- alerts ---------- */

export type AlertSummaryView = {
  critical: number; warning: number; info: number; total: number
  top: AlertRecord[]; latest: string | null; tone: Tone
}

export function alertSummary(catalog: AlertCatalog | null, limit = 3): AlertSummaryView {
  const active = sortAlerts(catalog?.active ?? [])
  const count = (severity: AlertRecord['severity']) => active.filter((item) => item.severity === severity).length
  const critical = count('critical'), warning = count('warning')
  const latest = active.reduce<string | null>((best, item) => (!best || item.updated_at > best ? item.updated_at : best), null)
  return { critical, warning, info: count('info'), total: active.length, top: active.slice(0, limit), latest, tone: critical ? 'offline' : warning ? 'degraded' : 'healthy' }
}

/* ---------- overview ---------- */

export type OverviewTile = { id: 'health' | 'devices' | 'alerts' | 'dns'; label: string; value: string; detail: string; tone: Tone; to: string }
export type OverviewView = { tone: Tone; headline: string; live: boolean; tiles: OverviewTile[] }

export function overviewView({ connection, healthReport, healthStatus, devices, devicesTrusted, devicesAllowed, alerts, alertsAllowed, system, systemAllowed, systemLive, now }: {
  connection: ConnectionStatus
  healthReport: HealthReport | null; healthStatus: string
  devices: readonly DeviceDetail[]; devicesTrusted: boolean; devicesAllowed: boolean
  alerts: AlertCatalog | null; alertsAllowed: boolean
  system: SystemData | null; systemAllowed: boolean; systemLive: boolean
  now: number
}): OverviewView {
  const live = connection === 'connected'
  const tiles: OverviewTile[] = []
  if (healthStatus !== 'forbidden') {
    const summary = healthSummary(healthReport, healthStatus === 'error' || !live)
    tiles.push({ id: 'health', label: 'RoomOS', value: summary.tone === 'healthy' ? 'OK' : summary.tone === 'degraded' ? 'Attention' : summary.tone === 'offline' ? 'Problem' : '—',
      detail: !live ? 'Not live' : healthReport ? `${healthReport.components.filter((item) => item.status === 'healthy').length}/${healthReport.components.length} checks ok` : 'Checking…',
      tone: !live ? 'stale' : summary.tone, to: '/system' })
  }
  if (devicesAllowed) {
    const rows = dashboardDevices(devices).map((device) => deviceRow(device, devicesTrusted, now))
    const servers = rows.filter((row) => row.id === 'primary' || row.id === 'secondary')
    const online = rows.filter((row) => row.connection === 'online').length
    const serverDown = servers.some((row) => row.connection === 'offline')
    tiles.push({ id: 'devices', label: 'Devices', value: rows.length ? `${online}/${rows.length}` : '—',
      detail: !devicesTrusted ? 'Last known' : serverDown ? `${servers.filter((row) => row.connection === 'offline').map((row) => row.short).join(', ')} offline` : 'online',
      tone: !devicesTrusted ? 'stale' : serverDown ? 'offline' : 'healthy', to: '/devices' })
  }
  if (alertsAllowed) {
    const summary = alertSummary(alerts)
    tiles.push({ id: 'alerts', label: 'Alerts', value: alerts ? String(summary.total) : '—',
      detail: !alerts ? 'Loading…' : `${summary.critical ? `${summary.critical} critical` : summary.warning ? `${summary.warning} warning` : 'All clear'}${live ? '' : ' · last known'}`,
      tone: !alerts ? 'unknown' : live ? summary.tone : 'stale', to: '/notifications' })
  }
  if (systemAllowed) {
    const dns = dnsView(system, systemLive, now)
    tiles.push({ id: 'dns', label: 'DNS', value: !system ? '—' : system.dns_cluster.status === 'healthy' ? '2/2' : system.dns_cluster.status === 'degraded' ? '1/2' : system.dns_cluster.status === 'offline' ? '0/2' : '—',
      detail: dns.problem ? 'Redundancy lost' : systemLive && system?.dns_cluster.status === 'healthy' ? 'Redundant' : dns.tone === 'stale' ? 'Last known' : 'Checking…', tone: dns.tone, to: '/system?tab=Network%20%26%20DNS' })
  }
  const rank: Record<Tone, number> = { offline: 4, degraded: 3, unknown: 2, stale: 1, healthy: 0 }
  const worst = tiles.reduce<Tone>((acc, tile) => (rank[tile.tone] > rank[acc] ? tile.tone : acc), 'healthy')
  const tone: Tone = !live ? (connection === 'offline' ? 'offline' : 'stale') : worst === 'stale' ? 'unknown' : worst
  const headline = !live ? (connection === 'offline' ? 'RoomOS is offline · showing last known state' : 'Reconnecting · showing last known state')
    : tone === 'healthy' ? 'Everything is running normally' : tone === 'offline' ? 'Something needs attention now' : tone === 'degraded' ? 'A few things need attention' : 'Waiting for live data'
  return { tone, headline, live, tiles }
}

/** English status line for the Dashboard (the Panel keeps its own Turkish wording). */
export function roomLightsLabel(summary: MainLightsSummary) {
  if (summary.power === 'unavailable') return 'Unavailable'
  const missing = summary.total - summary.available
  const state = summary.power === 'on' ? 'On' : summary.power === 'partial' ? 'Partly on' : 'Off'
  return `${state} · ${summary.onCount}/${summary.total}${missing ? ` · ${missing} unavailable` : ''}`
}

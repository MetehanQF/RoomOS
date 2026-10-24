import type { DeviceConnection, DeviceDetail, DeviceList, DeviceSensor, DeviceSummary, DeviceTelemetry, DeviceFreshness, PairedClient, PairedClientList } from './types'

/* Device Registry helpers (7D.1) used by the Device Center (7D.4). Read-only; never zero-fills values. */

export const DEVICE_ID = /^[a-z0-9_]{1,40}$/
const CONNECTIONS = ['online', 'offline', 'unknown']
const FRESHNESS = ['fresh', 'stale', 'unknown']
const AVAILABILITY = ['available', 'unavailable', 'unsupported', 'unknown']

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const isTime = (value: unknown) => value === null || (typeof value === 'string' && !Number.isNaN(Date.parse(value)))

function validSensor(value: unknown): value is DeviceSensor {
  return isRecord(value) && typeof value.id === 'string' && AVAILABILITY.includes(String(value.availability))
    && FRESHNESS.includes(String(value.freshness)) && isTime(value.observed_at) && isTime(value.measured_at)
    && ['number', 'boolean', 'text'].includes(String(value.kind))
    && ['supported', 'unverified', 'unsupported'].includes(String(value.support))
    && (value.availability === 'available'
      ? (value.kind === 'number' ? typeof value.value === 'number' && Number.isFinite(value.value)
        : value.kind === 'boolean' ? typeof value.value === 'boolean'
        : typeof value.value === 'string' || (typeof value.value === 'number' && Number.isFinite(value.value)))
      : value.value === null)
}

function validSummary(value: unknown): value is DeviceSummary {
  return isRecord(value) && typeof value.id === 'string' && DEVICE_ID.test(value.id) && typeof value.name === 'string'
    && CONNECTIONS.includes(String(value.connection)) && FRESHNESS.includes(String(value.telemetry_freshness))
    && isTime(value.last_seen_at) && isTime(value.last_telemetry_at) && Array.isArray(value.sources) && isRecord(value.capabilities)
    && value.sources.every((source) => isRecord(source) && typeof source.kind === 'string'
      && typeof source.configured === 'boolean' && CONNECTIONS.includes(String(source.connection))
      && isTime(source.last_seen_at) && typeof source.stale_after_seconds === 'number'
      && Number.isFinite(source.stale_after_seconds) && source.stale_after_seconds > 0)
}

const validDetail = (value: unknown): value is DeviceDetail => validSummary(value)
  && Array.isArray((value as Record<string, unknown>).sensors) && ((value as Record<string, unknown>).sensors as unknown[]).every(validSensor)

export function parseDeviceList(payload: unknown): DeviceList {
  if (!isRecord(payload) || !Array.isArray(payload.devices) || !payload.devices.every(validDetail) || typeof payload.generated_at !== 'string' || !isTime(payload.generated_at)) {
    throw new Error('RoomOS Core returned an incompatible device list.')
  }
  return payload as DeviceList
}

export function parseDeviceDetail(payload: unknown): DeviceDetail {
  if (!validDetail(payload)) {
    throw new Error('RoomOS Core returned an incompatible device.')
  }
  return payload as DeviceDetail
}

export function parseDeviceTelemetry(payload: unknown): DeviceTelemetry {
  if (!isRecord(payload) || typeof payload.device_id !== 'string' || !DEVICE_ID.test(payload.device_id) || typeof payload.generated_at !== 'string' || !isTime(payload.generated_at) || !Array.isArray(payload.sensors) || !payload.sensors.every(validSensor)) {
    throw new Error('RoomOS Core returned incompatible device telemetry.')
  }
  return payload as DeviceTelemetry
}

/**
 * Freshness as the client may display it now. Nothing is "fresh" while the live connection is
 * not trusted. Receipt times (observed_at/last_seen_at) are NOT aged here: the server no longer
 * publishes events when only they advance (7D.1-S2), so it owns fresh→stale/online→unknown
 * transitions and publishes them. Source-stated measured_at is still aged locally because a
 * newer measurement is itself a published change.
 */
export function effectiveFreshness(sensor: DeviceSensor, device: DeviceSummary, { now, trusted }: { now: number; trusted: boolean }): DeviceFreshness {
  if (sensor.availability !== 'available' || sensor.freshness === 'unknown' || !trusted || !Number.isFinite(now)) return 'unknown'
  const observed = Date.parse(sensor.observed_at ?? '')
  if (Number.isFinite(observed) && observed > now + 60_000) return 'unknown' // client clock behind server
  if (sensor.freshness === 'stale') return 'stale'
  if (!sensor.measured_at) return 'fresh'
  const threshold = device.sources.find((source) => source.kind === sensor.source)?.stale_after_seconds
  const measured = Date.parse(sensor.measured_at)
  if (threshold === undefined || Number.isNaN(measured) || measured > now + 60_000) return 'unknown'
  return now - measured >= threshold * 1000 ? 'stale' : 'fresh'
}

export function effectiveConnection(device: DeviceSummary, trusted: boolean, now = Date.now()): DeviceConnection {
  if (!trusted || !Number.isFinite(now)) return 'unknown'
  const seen = device.sources.map((source) => Date.parse(source.last_seen_at ?? '')).filter(Number.isFinite)
  if (seen.some((time) => time > now + 60_000)) return 'unknown' // client clock behind server
  return device.connection
}

const REASONS: Record<string, string> = {
  source_not_configured: 'Not connected to RoomOS yet',
  awaiting_first_report: 'Waiting for the first report',
  source_unavailable: 'Sensor unavailable',
  source_reported_unknown: 'Source reports no value',
  invalid_report: 'Last report was invalid',
  invalid_timestamp: 'Last report had an invalid time',
  clock_discontinuity: 'Clock changed; age unknown',
  not_supported_by_device: 'Not supported on this device',
}

export function connectionLabel(connection: DeviceConnection): string {
  return connection === 'online' ? 'Online' : connection === 'offline' ? 'Offline' : 'Unknown'
}

/** Offline means the source reported the device unreachable, not that it is powered off. */
export function connectionDetail(connection: DeviceConnection, device: Pick<DeviceSummary, 'type' | 'sources'>): string {
  if (!device.sources.some((source) => source.configured)) return 'Telemetry not connected yet'
  if (device.type === 'android') {
    // A phone reports from the RoomOS app; silence (sleep, Doze, app closed, no network) is never "offline".
    const web = device.sources.some((source) => source.configured && source.kind === 'roomos_web')
    if (connection === 'online') return web ? 'RoomOS web app open and reporting' : 'RoomOS app reporting'
    return web ? 'No recent report · the web app reports only while it is open' : 'No recent report · phone asleep, offline or app stopped'
  }
  if (connection === 'online') return device.type === 'windows' ? 'Agent heartbeat live' : 'Reporting'
  if (connection === 'offline') return device.type === 'windows' ? 'Agent disconnected · asleep, off or off-network' : 'Source reports it unreachable'
  return 'No recent report · state cannot be confirmed'
}

export function formatSensorValue(sensor: DeviceSensor): string {
  if (sensor.value === null) return '—'
  if (typeof sensor.value === 'boolean') return sensor.value ? 'Yes' : 'No'
  const text = typeof sensor.value === 'number' ? String(Math.round(sensor.value * 10) / 10) : sensor.value
  return sensor.unit ? `${text}${sensor.unit === '%' || sensor.unit === '°C' ? '' : ' '}${sensor.unit}` : text
}

/** Display text that never presents a stale or unknown value as current. */
export function sensorDisplay(sensor: DeviceSensor, freshness: DeviceFreshness): { text: string; note: string | null; tone: 'current' | 'stale' | 'muted' } {
  if (sensor.availability !== 'available') return { text: '—', note: REASONS[sensor.reason ?? ''] ?? 'No value', tone: 'muted' }
  const value = formatSensorValue(sensor)
  if (freshness === 'fresh') return { text: value, note: null, tone: 'current' }
  return { text: value, note: freshness === 'stale' ? 'Last reported value — not current' : 'Age unknown — not current', tone: 'stale' }
}

const CLIENT_APPS = ['web', 'android', 'unknown']

export function parsePairedClients(payload: unknown): PairedClientList {
  const valid = (value: unknown): value is PairedClient => isRecord(value) && typeof value.id === 'string' && /^[0-9a-f]{16}$/.test(value.id)
    && typeof value.name === 'string' && value.name.length <= 60 && CLIENT_APPS.includes(String(value.app))
    && typeof value.paired_at === 'string' && isTime(value.paired_at) && isTime(value.last_seen_at)
    && typeof value.active_session === 'boolean' && typeof value.current === 'boolean'
  if (!isRecord(payload) || !Array.isArray(payload.clients) || !payload.clients.every(valid)
    || typeof payload.last_seen_resolution_seconds !== 'number' || typeof payload.generated_at !== 'string' || !isTime(payload.generated_at)) {
    throw new Error('RoomOS Core returned an incompatible client list.')
  }
  return payload as PairedClientList
}

/* ----- Device Center presentation (7D.4). Pure; covered by devices-state tests. ----- */

export type SensorIcon = 'cpu' | 'memory' | 'disk' | 'thermometer' | 'battery' | 'plug' | 'zap' | 'pulse' | 'wifi' | 'server' | 'clock' | 'phone'
type SensorMeta = { label: string; icon: SensorIcon; progress?: boolean; yes?: string; no?: string; time?: boolean; values?: Record<string, string> }

const SENSOR_META: Record<string, SensorMeta> = {
  cpu_usage: { label: 'CPU', icon: 'cpu', progress: true },
  ram_usage: { label: 'RAM', icon: 'memory', progress: true },
  gpu_usage: { label: 'GPU', icon: 'cpu', progress: true },
  disk_usage: { label: 'Disk', icon: 'disk', progress: true },
  battery_percent: { label: 'Battery', icon: 'battery', progress: true },
  ac_connected: { label: 'Power adapter', icon: 'plug', yes: 'Plugged in', no: 'On battery' },
  charging: { label: 'Charging', icon: 'zap', yes: 'Charging', no: 'Not charging' },
  cpu_temperature: { label: 'CPU temperature', icon: 'thermometer' },
  gpu_temperature: { label: 'GPU temperature', icon: 'thermometer' },
  battery_temperature: { label: 'Battery temperature', icon: 'thermometer' },
  uptime: { label: 'Uptime', icon: 'clock' },
  last_boot: { label: 'Last boot', icon: 'clock', time: true },
  last_active: { label: 'Last active', icon: 'clock', time: true },
  docker_running: { label: 'Docker', icon: 'server', yes: 'Running', no: 'Stopped' },
  network_connected: { label: 'Network', icon: 'wifi', yes: 'Connected', no: 'Disconnected' },
  wifi_connected: { label: 'Wi-Fi', icon: 'wifi', yes: 'Connected', no: 'Disconnected' },
  panel_connected: { label: 'Panel link', icon: 'wifi', yes: 'Connected', no: 'Disconnected' },
  // 7D.3 Android (native app and PWA)
  power_source: { label: 'Power source', icon: 'plug', values: { ac: 'Charger', usb: 'USB', wireless: 'Wireless', dock: 'Dock', battery: 'Battery' } },
  battery_health: { label: 'Battery health', icon: 'battery', values: { good: 'Good', overheat: 'Overheating', dead: 'Dead', over_voltage: 'Over-voltage', cold: 'Too cold', failure: 'Failure' } },
  ram_total: { label: 'Memory size', icon: 'memory' },
  storage_free: { label: 'Free storage', icon: 'disk' },
  storage_total: { label: 'Storage size', icon: 'disk' },
  network_type: { label: 'Connection type', icon: 'wifi', values: { wifi: 'Wi‑Fi', cellular: 'Mobile data', ethernet: 'Ethernet', vpn: 'VPN', bluetooth: 'Bluetooth', other: 'Other', none: 'No network' } },
  thermal_status: { label: 'Thermal state', icon: 'thermometer', values: { none: 'Normal', light: 'Light', moderate: 'Moderate', severe: 'Severe', critical: 'Critical', emergency: 'Emergency', shutdown: 'Shutting down' } },
  android_version: { label: 'Android', icon: 'phone' },
  device_model: { label: 'Model', icon: 'phone' },
}

export const DISK_WARNING_PERCENT = 90

export function sensorMeta(sensor: Pick<DeviceSensor, 'id'>, device?: Pick<DeviceSummary, 'type'>): SensorMeta {
  const meta = SENSOR_META[sensor.id] ?? { label: sensor.id.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase()), icon: 'pulse' }
  // HASS.Agent reports the system drive only (7D.2-E: Storage attribute of drive C:).
  if (sensor.id === 'disk_usage' && device?.type === 'windows') return { ...meta, label: 'C: drive' }
  if (sensor.id === 'disk_usage' && device?.type === 'android') return { ...meta, label: 'Storage' }
  // Android: "connected" means validated internet; in a browser it is navigator.onLine.
  if (sensor.id === 'network_connected' && device?.type === 'android') return { ...meta, label: 'Internet', yes: 'Online', no: 'No internet' }
  return meta
}

/** Like sensorDisplay, with device-specific wording for booleans (never "Yes/No" for power). */
export function sensorText(sensor: DeviceSensor, freshness: DeviceFreshness, device?: Pick<DeviceSummary, 'type'>) {
  const shown = sensorDisplay(sensor, freshness)
  const meta = sensorMeta(sensor, device)
  if (sensor.availability === 'available' && typeof sensor.value === 'boolean' && meta.yes && meta.no) {
    return { ...shown, text: sensor.value ? meta.yes : meta.no }
  }
  if (sensor.availability === 'available' && meta.values && typeof sensor.value === 'string') {
    return { ...shown, text: meta.values[sensor.value] ?? sensor.value }
  }
  if (sensor.availability === 'available' && meta.time && typeof sensor.value === 'string' && isTime(sensor.value)) {
    return { ...shown, text: SENSOR_TIME.format(new Date(sensor.value)) }
  }
  return shown
}

// Timestamp sensors (last boot / last active) in the home's time zone, like Routines.
const SENSOR_TIME = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Istanbul', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })

/** Sensors the device can report (supported or unverified) vs. ones it never can. */
export function partitionSensors(device: DeviceDetail) {
  const reportable = device.sensors.filter((sensor) => sensor.support !== 'unsupported')
  const unsupported = device.sensors.filter((sensor) => sensor.support === 'unsupported')
  return { reportable, unsupported }
}

export function telemetryConnected(device: Pick<DeviceSummary, 'sources'>) {
  return device.sources.some((source) => source.configured)
}

/** Warns from the last reported value; the caller says whether that value is current. */
export function diskWarning(device: DeviceDetail): { percent: number; label: string; advice: string } | null {
  const disk = device.sensors.find((sensor) => sensor.id === 'disk_usage')
  if (!disk || disk.availability !== 'available' || typeof disk.value !== 'number' || disk.value <= DISK_WARNING_PERCENT) return null
  const advice = device.type === 'windows' ? 'Free up space to keep Windows updates and apps working.' : 'Free up space to keep Android updates, photos and apps working.'
  return { percent: Math.round(disk.value), label: sensorMeta(disk, device).label, advice }
}

/** Newest device-reported measurement time (phones); null when the source states none. */
export function lastMeasured(device: DeviceDetail): string | null {
  const times = device.sensors.map((sensor) => sensor.measured_at).filter((value): value is string => typeof value === 'string' && isTime(value))
  return times.length ? times.reduce((a, b) => (Date.parse(a) > Date.parse(b) ? a : b)) : null
}

/** Relative age of a receipt time. Never invents a time: null stays "never". */
export function relativeTime(value: string | null, now: number): string {
  if (!value) return 'never'
  const at = Date.parse(value)
  if (!Number.isFinite(at) || !Number.isFinite(now)) return 'unknown'
  const seconds = Math.round((now - at) / 1000)
  if (seconds < -60) return 'unknown' // client clock behind the server
  if (seconds < 10) return 'just now'
  if (seconds < 60) return `${seconds} s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 48) return `${hours} h ago`
  return `${Math.round(hours / 24)} d ago`
}

export type DeviceGroup = { id: 'computers' | 'mobile'; title: string; devices: DeviceDetail[] }

/** Stable display order: computers first (laptop before servers), then phones and panels. */
export function groupDevices(devices: DeviceDetail[]): DeviceGroup[] {
  const rank = (device: DeviceDetail) => (device.type === 'windows' ? 0 : device.type === 'linux' ? 1 : 2)
  const sorted = devices.slice().sort((a, b) => rank(a) - rank(b))
  const groups: DeviceGroup[] = [
    { id: 'computers', title: 'Computers', devices: sorted.filter((device) => device.type === 'windows' || device.type === 'linux') },
    { id: 'mobile', title: 'Phones, tablets & panels', devices: sorted.filter((device) => device.type !== 'windows' && device.type !== 'linux') },
  ]
  return groups.filter((group) => group.devices.length > 0)
}

import type { DeviceDetail, DeviceSensor, PairedClient } from './types'

/* Realistic registry payloads shaped like production 241b868 (Workstation via HASS.Agent). */

export const T = Date.parse('2026-10-15T09:00:00Z')
export const iso = (ms: number) => new Date(ms).toISOString()

export const sensor = (id: string, change: Partial<DeviceSensor> = {}): DeviceSensor => ({
  id, kind: 'number', support: 'supported', source: 'home_assistant', value: 10, unit: '%',
  availability: 'available', freshness: 'fresh', observed_at: iso(T - 5_000), measured_at: null, reason: null, ...change,
})

const unsupported = (id: string, kind: DeviceSensor['kind'] = 'number', unit: string | null = null): DeviceSensor => ({
  id, kind, support: 'unsupported', source: null, value: null, unit, availability: 'unsupported', freshness: 'unknown',
  observed_at: null, measured_at: null, reason: 'not_supported_by_device',
})

export function workstation({ connection = 'online', freshness = 'fresh', disk = 99, overrides = {} }: {
  connection?: DeviceDetail['connection']; freshness?: DeviceSensor['freshness']; disk?: number; overrides?: Partial<DeviceDetail>
} = {}): DeviceDetail {
  const live = (id: string, value: number | boolean, change: Partial<DeviceSensor> = {}) => sensor(id, {
    value, freshness, kind: typeof value === 'boolean' ? 'boolean' : 'number', unit: typeof value === 'boolean' ? null : '%', ...change,
  })
  return {
    id: 'workstation', name: 'Workstation', type: 'windows', platform: 'Windows', connection,
    health: connection === 'online' ? 'healthy' : connection === 'offline' ? 'offline' : 'unknown',
    telemetry_freshness: freshness, last_seen_at: iso(T - 12_000), last_telemetry_at: iso(T - 12_000), status_reason: null,
    capabilities: { cpu_usage: 'supported', gpu_usage: 'unverified', cpu_temperature: 'unsupported' },
    sources: [{ kind: 'home_assistant', configured: true, connection, last_seen_at: iso(T - 12_000), stale_after_seconds: 900 }],
    metadata: { role: 'laptop', planned_phase: '7D.2' },
    sensors: [
      live('cpu_usage', 23), live('ram_usage', 92), live('disk_usage', disk), live('battery_percent', 100),
      live('ac_connected', true), live('charging', false), live('gpu_usage', 71.9, { support: 'unverified' }),
      unsupported('last_boot', 'text'), unsupported('cpu_temperature', 'number', '°C'),
      unsupported('gpu_temperature', 'number', '°C'), unsupported('network_connected', 'boolean'),
    ],
    ...overrides,
  }
}

export function phone(id = 'phone', name = 'Phone'): DeviceDetail {
  return {
    id, name, type: 'android', platform: 'Android', connection: 'unknown', health: 'unknown', telemetry_freshness: 'unknown',
    last_seen_at: null, last_telemetry_at: null, status_reason: 'source_not_configured',
    capabilities: { battery_percent: 'supported', cpu_usage: 'unsupported' },
    sources: [{ kind: 'roomos_native', configured: false, connection: 'unknown', last_seen_at: null, stale_after_seconds: 120 }],
    metadata: { role: 'personal_phone', planned_phase: '7D.3' },
    sensors: [
      sensor('battery_percent', { value: null, availability: 'unknown', freshness: 'unknown', observed_at: null, reason: 'source_not_configured' }),
      sensor('charging', { kind: 'boolean', unit: null, value: null, availability: 'unknown', freshness: 'unknown', observed_at: null, reason: 'source_not_configured' }),
      unsupported('cpu_usage', 'number', '%'), unsupported('ram_usage', 'number', '%'),
    ],
  }
}

export function server(id = 'primary', name = 'Raspberry Pi 5'): DeviceDetail {
  const live = (sid: string, value: number | string | boolean, unit: string | null = '%') =>
    sensor(sid, { source: 'system_monitor', value, unit, kind: typeof value === 'string' ? 'text' : typeof value === 'boolean' ? 'boolean' : 'number' })
  return {
    id, name, type: 'linux', platform: 'Linux arm64', connection: 'online', health: 'healthy', telemetry_freshness: 'fresh',
    last_seen_at: iso(T - 20_000), last_telemetry_at: iso(T - 20_000), status_reason: null, capabilities: { cpu_usage: 'supported' },
    sources: [{ kind: 'system_monitor', configured: true, connection: 'online', last_seen_at: iso(T - 20_000), stale_after_seconds: 180 }],
    metadata: { role: 'primary_server' },
    sensors: [live('cpu_usage', 12), live('ram_usage', 40), live('disk_usage', 30), live('cpu_temperature', 51, '°C'),
      live('uptime', '3d 4h', null), live('docker_running', true, null), unsupported('battery_percent', 'number', '%')],
  }
}

export const client = (change: Partial<PairedClient> = {}): PairedClient => ({
  id: '0123456789abcdef', name: 'Phone', app: 'web', paired_at: '2026-09-29T07:35:01Z', last_seen_at: iso(T - 120_000),
  active_session: true, current: false, ...change,
})

/* 7D.3: a phone bound to the RoomOS Android app (native) or PWA, as the registry returns it. */
export function reportingPhone({ app = 'native', connection = 'online', freshness = 'fresh', storage = 93.5 }: {
  app?: 'native' | 'web'; connection?: DeviceDetail['connection']; freshness?: DeviceSensor['freshness']; storage?: number
} = {}): DeviceDetail {
  const source = app === 'native' ? 'roomos_native' : 'roomos_web'
  const measured = iso(T - 25_000)
  const live = (id: string, value: number | boolean | string, unit: string | null = null, change: Partial<DeviceSensor> = {}) => sensor(id, {
    source, value, unit, freshness, measured_at: measured, observed_at: iso(T - 8_000),
    kind: typeof value === 'boolean' ? 'boolean' : typeof value === 'string' ? 'text' : 'number', ...change,
  })
  const common = [live('battery_percent', 76, '%'), live('charging', true), live('network_type', 'wifi'), live('network_connected', true),
    live('android_version', '14'), live('device_model', 'Pixel 7')]
  const nativeOnly = [live('power_source', 'usb'), live('battery_temperature', 31.4, '°C'), live('battery_health', 'good'),
    live('ram_usage', 61.2, '%'), live('ram_total', 7.9, 'GB'), live('disk_usage', storage, '%'), live('storage_free', 8.1, 'GB'),
    live('storage_total', 128, 'GB'), live('thermal_status', 'none', null, { support: 'unverified' })]
  const browserMissing = ['power_source', 'battery_temperature', 'battery_health', 'ram_usage', 'ram_total', 'disk_usage', 'storage_free', 'storage_total', 'thermal_status']
  return {
    id: 'phone', name: 'Phone', type: 'android', platform: 'Android', connection,
    health: connection === 'online' ? 'healthy' : 'unknown', telemetry_freshness: freshness,
    last_seen_at: iso(T - 8_000), last_telemetry_at: iso(T - 8_000), status_reason: null,
    capabilities: { battery_percent: 'supported', cpu_usage: 'unsupported' },
    sources: [{ kind: source, configured: true, connection, last_seen_at: iso(T - 8_000), stale_after_seconds: 90 }],
    metadata: { role: 'personal_phone', reporter: app === 'native' ? 'RoomOS Android app' : 'RoomOS web app (Chrome)' },
    sensors: [...common, ...(app === 'native' ? nativeOnly : browserMissing.map((id) => unsupported(id))),
      unsupported('cpu_usage', 'number', '%'), unsupported('cpu_temperature', 'number', '°C')],
  }
}

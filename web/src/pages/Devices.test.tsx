import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { DeviceCenterView, DevicePermissionPanel } from './Devices'
import { DeviceCard } from '../components/DeviceCard'
import { bindDevicesLifecycle, DevicesStore, type DevicesSnapshot } from '../devices-store'
import type { ConnectionStatus } from '../types'
import {
  connectionDetail, diskWarning, groupDevices, parseDeviceList, parsePairedClients, relativeTime, sensorMeta, sensorText,
} from '../devices-state'
import { navigationItems } from '../navigation'
import { client, iso, phone, reportingPhone, sensor, server, T, workstation } from '../devices-fixtures.test-support'
import type { DeviceDetail, RoomOSEvent } from '../types'

const snap = (change: Partial<DevicesSnapshot> = {}): DevicesSnapshot => ({
  status: 'ready', devices: [workstation(), server(), server('secondary', 'Secondary'), phone()], connection: 'connected', trusted: true,
  loadedAt: T, clients: [client({ current: true, name: 'Phone' }), client({ id: 'fedcba9876543210', name: 'Phone Native', app: 'android' })],
  clientsStatus: 'ready', clientResolutionSeconds: 300, ...change,
})
const view = (change: Partial<DevicesSnapshot> = {}) => renderToStaticMarkup(<MemoryRouter><DeviceCenterView snapshot={snap(change)} now={T} onRetry={() => undefined} /></MemoryRouter>)
const card = (device: DeviceDetail, trusted = true) => renderToStaticMarkup(<DeviceCard device={device} trusted={trusted} now={T} featured />)
const tile = (html: string, id: string) => html.match(new RegExp(`data-sensor="${id}".*?</div>(?:<small[^>]*>.*?</small>)?</div>`))?.[0] ?? ''

describe('Workstation card', () => {
  it('shows every Workstation metric with live freshness when the agent is online', () => {
    const html = card(workstation())
    for (const label of ['CPU', 'RAM', 'GPU', 'C: drive', 'Battery', 'Power adapter', 'Charging']) expect(html).toContain(label)
    expect(tile(html, 'cpu_usage')).toContain('23%')
    expect(tile(html, 'cpu_usage')).toContain('Live')
    expect(tile(html, 'ac_connected')).toContain('Plugged in')
    expect(tile(html, 'charging')).toContain('Not charging')
    expect(tile(html, 'gpu_usage')).toContain('Unverified source')
    expect(html).toContain('Agent heartbeat live')
    expect(html).toContain('Last report 12 s ago')
    expect(html).toContain('does not report measurement times')
  })

  it('never presents stale or offline values as current', () => {
    for (const device of [workstation({ connection: 'offline', freshness: 'stale' }), workstation({ freshness: 'stale' })]) {
      const html = card(device)
      expect(tile(html, 'cpu_usage')).toContain('Stale')
      expect(tile(html, 'cpu_usage')).toContain('Last reported value — not current')
      expect(tile(html, 'cpu_usage')).not.toContain('Live')
    }
    expect(card(workstation({ connection: 'offline', freshness: 'stale' }))).toContain('Agent disconnected')
    // An untrusted live channel (reconnecting) marks everything as not current.
    const paused = card(workstation(), false)
    expect(paused).toContain('Live updates paused')
    expect(tile(paused, 'ram_usage')).toContain('Not current')
    expect(paused).toContain('>Unknown<')
  })

  it('lists unsupported sensors by name and never renders them as 0', () => {
    const html = card(workstation())
    expect(html).toContain('Not reported by this device')
    for (const label of ['Last boot', 'CPU temperature', 'GPU temperature', 'Network']) expect(html).toContain(label)
    expect(html).not.toContain('data-sensor="cpu_temperature"')
    const unavailable = card(workstation({ overrides: { sensors: [sensor('cpu_usage', { value: null, availability: 'unavailable', freshness: 'unknown', reason: 'source_unavailable' })] } }))
    expect(tile(unavailable, 'cpu_usage')).toContain('—')
    expect(tile(unavailable, 'cpu_usage')).toContain('Sensor unavailable')
    expect(tile(unavailable, 'cpu_usage')).not.toContain('0%')
  })

  it('does not reuse the Lights page .device-power button class (style collision)', () => {
    const html = card(workstation())
    expect(html).toContain('class="device-power-row"')
    expect(html).not.toMatch(/class="[^"]*\bdevice-power\b(?!-)/)
  })

  it('warns above 90% disk use, and qualifies a stale reading', () => {
    expect(card(workstation({ disk: 99 }))).toContain('C: drive is 99% full')
    expect(card(workstation({ disk: 99 }))).toContain('Free up space')
    expect(card(workstation({ disk: 99, freshness: 'stale' }))).toContain('current fill level unknown')
    expect(card(workstation({ disk: 90 }))).not.toContain('% full')
    expect(diskWarning(workstation({ disk: 90.4 }))).toEqual({ percent: 90, label: 'C: drive', advice: 'Free up space to keep Windows updates and apps working.' })
    expect(diskWarning(server())).toBeNull()
  })
})

describe('Android phones (7D.3)', () => {
  it('shows native Android telemetry with device and receipt times', () => {
    const html = card(reportingPhone())
    for (const text of ['Online', 'RoomOS app reporting', '76%', 'Charging', 'USB', 'Wi‑Fi', 'Good', '31.4°C', '61.2%', '7.9 GB', '8.1 GB', 'Android', '14', 'Pixel 7', 'Measured 25 s ago · device clock', 'Last report just now']) {
      expect(html).toContain(text)
    }
    expect(html).toContain('Storage is 94% full')
    expect(html).toContain('Free up space to keep Android updates, photos and apps working.')
    expect(html).toContain('roughly every 15 minutes')
    expect(html).not.toContain('Windows')
    // CPU load / temperature are never readable by Android apps: listed, never rendered as values.
    expect(html).toMatch(/Not reported by this device<\/span><small>2/)
  })

  it('shows what the PWA can and cannot read, and never calls a silent phone offline', () => {
    const html = card(reportingPhone({ app: 'web' }))
    expect(html).toContain('RoomOS web app open and reporting')
    expect(html).not.toContain('data-sensor="ram_usage"')
    expect(html).not.toContain('data-sensor="disk_usage"')
    expect(html).toMatch(/Not reported by this device<\/span><small>11/)
    const silent = reportingPhone({ app: 'web', connection: 'unknown', freshness: 'stale' })
    expect(connectionDetail('unknown', silent)).toBe('No recent report · the web app reports only while it is open')
    expect(connectionDetail('unknown', reportingPhone({ connection: 'unknown' }))).toBe('No recent report · phone asleep, offline or app stopped')
    const staleHtml = card(reportingPhone({ connection: 'unknown', freshness: 'stale' }))
    expect(staleHtml).toContain('Last reported value — not current')
    expect(staleHtml).toContain('current fill level unknown')
  })

  it('labels Android storage and internet, and maps enumerated values', () => {
    const phoneDevice = { type: 'android' as const }
    expect(sensorMeta({ id: 'disk_usage' }, phoneDevice).label).toBe('Storage')
    expect(sensorText(sensor('network_connected', { kind: 'boolean', value: false, unit: null }), 'fresh', phoneDevice).text).toBe('No internet')
    expect(sensorText(sensor('network_type', { kind: 'text', value: 'cellular', unit: null }), 'fresh').text).toBe('Mobile data')
    expect(sensorText(sensor('thermal_status', { kind: 'text', value: 'severe', unit: null }), 'fresh').text).toBe('Severe')
    expect(sensorText(sensor('power_source', { kind: 'text', value: 'future_kind', unit: null }), 'fresh').text).toBe('future_kind')
  })
})

describe('other devices and clients', () => {
  it('shows registry phones without inventing telemetry', () => {
    const html = card(phone())
    expect(html).toContain('Not connected')
    expect(html).toContain('planned: 7D.3')
    expect(html).not.toContain('data-sensor=')
    expect(html).not.toMatch(/\d+%/)
    expect(connectionDetail('unknown', phone())).toBe('Telemetry not connected yet')
  })

  it('groups computers before phones and lists paired apps with the current device first', () => {
    expect(groupDevices([phone(), server(), workstation()]).map((g) => g.devices.map((d) => d.id))).toEqual([['workstation', 'primary'], ['phone']])
    const html = view()
    expect(html.indexOf('Workstation')).toBeLessThan(html.indexOf('Raspberry Pi 5'))
    expect(html.indexOf('Raspberry Pi 5')).toBeLessThan(html.indexOf('Phone'))
    expect(html).toContain('This device')
    expect(html).toContain('Android app')
    expect(html).toContain('updated every 5 min')
    expect(view({ clients: [client({ active_session: false, app: 'unknown' })] })).toContain('No active session')
  })

  it('summarises Online / Offline / Unknown / Not connected counts', () => {
    const html = view({ devices: [workstation({ connection: 'offline', freshness: 'stale' }), server(), phone()] })
    expect(html).toMatch(/<strong>1<\/strong><span>Online<\/span>.*<strong>1<\/strong><span>Offline<\/span>.*<strong>0<\/strong><span>Unknown<\/span>.*<strong>1<\/strong><span>Not connected<\/span>/)
  })

  it('escapes operator-entered app names', () => {
    const html = view({ clients: [client({ name: '<img src=x onerror=alert(1)>' })] })
    expect(html).not.toContain('<img src=x')
    expect(html).toContain('&lt;img')
  })
})

describe('page states', () => {
  it('renders loading, permission, error with retry and empty states', () => {
    expect(view({ status: 'loading', devices: [] })).toContain('Reading devices')
    const denied = view({ status: 'forbidden', devices: [] })
    expect(denied).toContain('Device access not enabled')
    expect(denied).toContain('will not need to pair again')
    expect(renderToStaticMarkup(<DevicePermissionPanel />)).not.toContain('Pair this device')
    const error = view({ status: 'error' })
    expect(error).toContain('could not read the device list')
    expect(error).toContain('Retry')
    expect(view({ devices: [], clients: [] })).toContain('No devices to show')
  })

  it('shows Devices in navigation only for apps with devices:read', () => {
    expect(navigationItems(['lights:read', 'system:read']).map((i) => i.label)).toEqual(['Home', 'Lights', 'Routines', 'Notifications', 'System'])
    expect(navigationItems(['devices:read']).map((i) => i.label)).toContain('Devices')
  })
})

describe('presentation helpers', () => {
  it('formats relative receipt times without inventing them', () => {
    expect(relativeTime(null, T)).toBe('never')
    expect(relativeTime(iso(T - 3_000), T)).toBe('just now')
    expect(relativeTime(iso(T - 45_000), T)).toBe('45 s ago')
    expect(relativeTime(iso(T - 600_000), T)).toBe('10 min ago')
    expect(relativeTime(iso(T - 3 * 3_600_000), T)).toBe('3 h ago')
    expect(relativeTime(iso(T + 120_000), T)).toBe('unknown')
    expect(relativeTime('nonsense', T)).toBe('unknown')
  })

  it('words booleans per sensor and labels the Windows disk as C:', () => {
    expect(sensorMeta({ id: 'disk_usage' }, { type: 'windows' }).label).toBe('C: drive')
    expect(sensorMeta({ id: 'disk_usage' }, { type: 'linux' }).label).toBe('Disk')
    expect(sensorText(sensor('ac_connected', { kind: 'boolean', value: false, unit: null }), 'fresh').text).toBe('On battery')
    expect(sensorText(sensor('ac_connected', { kind: 'boolean', value: true, unit: null }), 'stale')).toMatchObject({ text: 'Plugged in', tone: 'stale' })
    // 7D.4.1: timestamps in Istanbul time; a non-time string stays as reported.
    expect(sensorText(sensor('last_active', { kind: 'text', value: '2026-10-15T06:59:37Z', unit: null }), 'fresh').text).toBe('15 Oct, 09:59')
    expect(sensorText(sensor('last_active', { kind: 'text', value: '2026-10-15T06:59:37Z', unit: null }), 'stale')).toMatchObject({ text: '15 Oct, 09:59', tone: 'stale' })
    expect(sensorText(sensor('last_boot', { kind: 'text', value: 'not a time', unit: null }), 'fresh').text).toBe('not a time')
    expect(sensorText(sensor('network_connected', { kind: 'boolean', value: false, unit: null }), 'fresh').text).toBe('Disconnected')
    expect(sensorMeta(sensor('last_active')).label).toBe('Last active')
  })

  it('validates list payloads with sensors and client payloads', () => {
    expect(parseDeviceList({ devices: [workstation(), phone()], generated_at: iso(T) }).devices).toHaveLength(2)
    const { sensors: _sensors, ...summary } = workstation()
    expect(_sensors.length).toBeGreaterThan(0)
    expect(() => parseDeviceList({ devices: [summary], generated_at: iso(T) })).toThrow()
    expect(parsePairedClients({ clients: [client()], last_seen_resolution_seconds: 300, generated_at: iso(T) }).clients).toHaveLength(1)
    for (const bad of [{ id: 'raw-uuid' }, { app: 'ios' }, { name: 'x'.repeat(61) }, { last_seen_at: 'yesterday' }]) {
      expect(() => parsePairedClients({ clients: [client(bad as never)], last_seen_resolution_seconds: 300, generated_at: iso(T) })).toThrow()
    }
  })
})

describe('DevicesStore with paired clients', () => {
  const event = (d: DeviceDetail): RoomOSEvent => ({ type: 'device.updated', timestamp: iso(T), device_id: null, data: { device: d } })

  it('loads devices and clients independently and keeps unchanged device identity', async () => {
    const api = {
      devices: vi.fn(async () => ({ devices: [workstation(), server()], generated_at: iso(T) })),
      clients: vi.fn(async () => { throw Object.assign(new Error('down'), { status: 503 }) }),
    }
    const store = new DevicesStore(api, 'connected', () => T)
    await store.load()
    expect(store.getSnapshot()).toMatchObject({ status: 'ready', clientsStatus: 'error', trusted: true })
    const before = store.getSnapshot().devices
    store.handleEvent(event(workstation({ overrides: { last_seen_at: iso(T) } })))
    const after = store.getSnapshot().devices
    expect(after[0]).not.toBe(before[0])
    expect(after[1]).toBe(before[1]) // the Pi card's props are unchanged: no re-render
  })

  it('does not notify when a reload returns identical clients', async () => {
    const api = { devices: vi.fn(async () => ({ devices: [], generated_at: iso(T) })), clients: vi.fn(async () => ({ clients: [client()], last_seen_resolution_seconds: 300, generated_at: iso(T) })) }
    const store = new DevicesStore(api, 'connected', () => T)
    await store.load()
    const clients = store.getSnapshot().clients
    await store.load()
    expect(store.getSnapshot().clients).toBe(clients)
  })

  it('clears clients when access is lost', async () => {
    let denied = false
    const api = { devices: vi.fn(async () => ({ devices: [], generated_at: iso(T) })), clients: vi.fn(async () => {
      if (denied) throw Object.assign(new Error('no'), { status: 403 })
      return { clients: [client()], last_seen_resolution_seconds: 300, generated_at: iso(T) }
    }) }
    const store = new DevicesStore(api, 'connected', () => T)
    await store.load()
    denied = true
    await store.load()
    expect(store.getSnapshot()).toMatchObject({ clientsStatus: 'forbidden', clients: [] })
  })
})

describe('lifecycle binding loads exactly once per connection epoch', () => {
  const harness = (initial: ConnectionStatus) => {
    let status: ((s: ConnectionStatus) => void) | null = null
    let refresh: (() => Promise<void>) | null = null
    const lifecycle = {
      subscribeStatus: vi.fn((listener: (s: ConnectionStatus) => void) => { status = listener; listener(initial); return () => undefined }),
      subscribeEvents: vi.fn((_l: unknown, r?: () => Promise<void>) => { refresh = r ?? null; return () => undefined }),
    }
    const api = { devices: vi.fn(async () => ({ devices: [workstation()], generated_at: iso(T) })), clients: vi.fn(async () => ({ clients: [], last_seen_resolution_seconds: 300, generated_at: iso(T) })) }
    const store = new DevicesStore(api)
    bindDevicesLifecycle(store, lifecycle as never)
    return { api, store, setStatus: (s: ConnectionStatus) => status?.(s), refresh: () => refresh?.() }
  }

  it('waits for the socket-open refresh instead of loading twice at app start', async () => {
    const h = harness('reconnecting')
    expect(h.api.devices).not.toHaveBeenCalled()
    h.setStatus('connected'); await h.refresh()
    expect(h.api.devices).toHaveBeenCalledTimes(1)
    expect(h.api.clients).toHaveBeenCalledTimes(1)
    expect(h.store.getSnapshot()).toMatchObject({ status: 'ready', trusted: true })
  })

  it('loads immediately when already connected, and once when the socket cannot open', async () => {
    expect(harness('connected').api.devices).toHaveBeenCalledTimes(1)
    const h = harness('reconnecting')
    h.setStatus('offline'); h.setStatus('reconnecting'); h.setStatus('offline')
    expect(h.api.devices).toHaveBeenCalledTimes(1)
    await Promise.resolve()
    expect(h.store.getSnapshot().trusted).toBe(false) // offline data is never trusted as live
  })
})

import { describe, expect, it, vi } from 'vitest'
import { connectionLabel, effectiveConnection, effectiveFreshness, parseDeviceDetail, parseDeviceList, parseDeviceTelemetry, sensorDisplay } from './devices-state'
import { bindDevicesLifecycle, DevicesStore } from './devices-store'
import type { DeviceDetail, DeviceSensor, RoomOSEvent } from './types'

const T = Date.parse('2026-10-14T12:00:00Z')
const iso = (ms: number) => new Date(ms).toISOString()
const sensor = (change: Partial<DeviceSensor> = {}): DeviceSensor => ({
  id: 'cpu_usage', kind: 'number', support: 'supported', source: 'system_monitor', value: 31.5, unit: '%',
  availability: 'available', freshness: 'fresh', observed_at: iso(T), measured_at: null, reason: null, ...change,
})
const device = (change: Partial<DeviceDetail> = {}): DeviceDetail => ({
  id: 'primary', name: 'Raspberry Pi 5', type: 'linux', platform: 'Linux arm64', connection: 'online', health: 'healthy',
  telemetry_freshness: 'fresh', last_seen_at: iso(T), last_telemetry_at: iso(T), status_reason: null,
  capabilities: { cpu_usage: 'supported', battery_percent: 'unsupported' },
  sources: [{ kind: 'system_monitor', configured: true, connection: 'online', last_seen_at: iso(T), stale_after_seconds: 180 }],
  metadata: { role: 'primary_server' }, sensors: [sensor()], ...change,
})
const event = (d: DeviceDetail): RoomOSEvent => ({ type: 'device.updated', timestamp: iso(T), device_id: null, data: { device: d } })

describe('device parsing', () => {
  it('accepts registry payloads and rejects incompatible ones', () => {
    expect(parseDeviceList({ devices: [device()], generated_at: iso(T) }).devices).toHaveLength(1)
    expect(parseDeviceList({ devices: [], generated_at: iso(T) }).devices).toEqual([])
    expect(parseDeviceDetail(device()).sensors[0].value).toBe(31.5)
    expect(parseDeviceTelemetry({ device_id: 'primary', sensors: [sensor()], generated_at: iso(T) }).sensors).toHaveLength(1)
    expect(() => parseDeviceList({ devices: [{ ...device(), id: '../primary' }] })).toThrow()
    expect(() => parseDeviceDetail({ ...device(), sensors: [sensor({ availability: 'unknown', value: 0 })] })).toThrow()
    expect(() => parseDeviceDetail({ ...device(), connection: 'powered_off' })).toThrow()
    expect(() => parseDeviceDetail({ ...device(), last_seen_at: 'yesterday' })).toThrow()
  })
})

describe('freshness and display', () => {
  it('never shows a stale, unknown or untrusted value as current', () => {
    const d = device()
    // Receipt time is not aged client-side (7D.1-S2): the server publishes fresh→stale itself.
    expect(effectiveFreshness(sensor(), d, { now: T + 179_000, trusted: true })).toBe('fresh')
    expect(effectiveFreshness(sensor(), d, { now: T + 3_600_000, trusted: true })).toBe('fresh')
    expect(effectiveFreshness(sensor({ measured_at: iso(T) }), d, { now: T + 179_000, trusted: true })).toBe('fresh')
    expect(effectiveFreshness(sensor({ measured_at: iso(T) }), d, { now: T + 180_000, trusted: true })).toBe('stale')
    expect(effectiveFreshness(sensor(), d, { now: T, trusted: false })).toBe('unknown')
    expect(effectiveFreshness(sensor({ freshness: 'stale' }), d, { now: T, trusted: true })).toBe('stale')
    expect(effectiveFreshness(sensor({ measured_at: iso(T - 300_000) }), d, { now: T, trusted: true })).toBe('stale')
    expect(sensorDisplay(sensor(), 'fresh')).toEqual({ text: '31.5%', note: null, tone: 'current' })
    expect(sensorDisplay(sensor(), 'stale')).toMatchObject({ text: '31.5%', note: 'Last reported value — not current', tone: 'stale' })
    expect(sensorDisplay(sensor(), 'unknown').note).toContain('not current')
  })

  it('labels unsupported and missing values without zero-filling', () => {
    const unsupported = sensor({ id: 'battery_percent', support: 'unsupported', source: null, value: null, availability: 'unsupported', freshness: 'unknown', observed_at: null, reason: 'not_supported_by_device' })
    expect(sensorDisplay(unsupported, 'unknown')).toEqual({ text: '—', note: 'Not supported on this device', tone: 'muted' })
    expect(sensorDisplay(sensor({ value: null, availability: 'unknown', freshness: 'unknown', reason: 'source_not_configured' }), 'unknown').note).toBe('Not connected to RoomOS yet')
    expect(sensorDisplay(sensor({ kind: 'boolean', value: false, unit: null }), 'fresh').text).toBe('No')
  })

  it('labels Online / Offline / Unknown and treats an untrusted connection as unknown', () => {
    expect(connectionLabel('online')).toBe('Online')
    expect(connectionLabel('offline')).toBe('Offline')
    expect(connectionLabel('unknown')).toBe('Unknown')
    expect(effectiveConnection(device(), false)).toBe('unknown')
  })
})

describe('DevicesStore', () => {
  const setup = (list = [device()]) => {
    const api = { devices: vi.fn(async () => ({ devices: list, generated_at: iso(T) })) }
    return { api, store: new DevicesStore(api, 'connected', () => T) }
  }

  it('loads, applies changed events once and ignores duplicates or unknown devices', async () => {
    const { store } = setup()
    const listener = vi.fn(); store.subscribe(listener)
    store.handleEvent(event(device({ health: 'degraded' })))
    expect(listener).not.toHaveBeenCalled() // not loaded yet: the REST load is authoritative
    await store.load()
    expect(store.getSnapshot()).toMatchObject({ status: 'ready', trusted: true })
    listener.mockClear()
    store.handleEvent(event(device()))
    expect(listener).not.toHaveBeenCalled()
    store.handleEvent(event(device({ telemetry_freshness: 'stale' })))
    expect(listener).toHaveBeenCalledTimes(1)
    expect(store.getSnapshot().devices[0].telemetry_freshness).toBe('stale')
    store.handleEvent(event(device({ id: 'phone', name: 'Phone' })))
    expect(store.getSnapshot().devices).toHaveLength(1)
  })

  it('distrusts live state on disconnect until a reload and discards superseded loads', async () => {
    const { api, store } = setup()
    await store.load()
    store.setConnection('reconnecting')
    expect(store.getSnapshot().trusted).toBe(false)
    let resolve!: (value: Awaited<ReturnType<typeof api.devices>>) => void
    api.devices.mockImplementationOnce(() => new Promise((r) => { resolve = r }))
    const pending = store.load()
    store.setConnection('connected')
    resolve({ devices: [device({ health: 'unknown' })], generated_at: iso(T) })
    await pending
    expect(store.getSnapshot().devices[0].health).toBe('healthy')
    await store.load()
    expect(store.getSnapshot()).toMatchObject({ trusted: true, connection: 'connected' })
    expect(api.devices).toHaveBeenCalledTimes(3)
  })

  it('maps forbidden and errors without keeping stale trust', async () => {
    const api = { devices: vi.fn(async () => { throw Object.assign(new Error('no'), { status: 403 }) }) }
    const store = new DevicesStore(api, 'connected')
    await store.load()
    expect(store.getSnapshot()).toMatchObject({ status: 'forbidden', trusted: false, devices: [] })
  })

  it('supports an empty registry', async () => {
    const { store } = setup([])
    await store.load()
    expect(store.getSnapshot()).toMatchObject({ status: 'ready', devices: [] })
  })
})


it('fails closed for future timestamps and invalid sensor payloads', () => {
  expect(effectiveFreshness(sensor(), device(), { now: T - 120_000, trusted: true })).toBe('unknown')
  expect(effectiveFreshness(sensor(), device(), { now: NaN, trusted: true })).toBe('unknown')
  expect(() => parseDeviceDetail(device({ sensors: [sensor({ value: NaN })] }))).toThrow()
  expect(() => parseDeviceTelemetry({ device_id: '../primary', sensors: [], generated_at: iso(T) })).toThrow()
  expect(() => parseDeviceList({ devices: [] })).toThrow()
})

it('clears previously visible devices when authorization is lost', async () => {
  const api = { devices: vi.fn(async () => ({ devices: [device()], generated_at: iso(T) })) }
  const store = new DevicesStore(api, 'connected')
  await store.load()
  api.devices.mockRejectedValueOnce({ status: 403 })
  await store.load()
  expect(store.getSnapshot()).toMatchObject({ devices: [], trusted: false, status: 'forbidden' })
})


it('trusts the server connection verdict and fails closed on client clock skew', () => {
  expect(effectiveConnection(device(), true, T)).toBe('online')
  expect(effectiveConnection(device(), true, T + 180_000)).toBe('online') // server publishes online→unknown
  expect(effectiveConnection(device({ connection: 'unknown' }), true, T)).toBe('unknown')
  expect(effectiveConnection(device(), true, T - 120_000)).toBe('unknown') // client clock behind server
})

it('keeps events received during a REST synchronization instead of overwriting them', async () => {
  let resolve!: (list: { devices: DeviceDetail[]; generated_at: string }) => void
  const store = new DevicesStore({ devices: () => new Promise((r) => { resolve = r }) }, 'connected')
  const load = store.load()
  store.handleEvent(event(device({ health: 'degraded' })))
  resolve({ devices: [device()], generated_at: iso(T) })
  await load
  expect(store.getSnapshot().devices[0].health).toBe('degraded')
})

it('binds to the existing lifecycle with event refresh disabled and cleans up', async () => {
  const offEvents = vi.fn(), offStatus = vi.fn()
  const lifecycle = { subscribeStatus: vi.fn<(listener: unknown) => () => void>().mockReturnValue(offStatus), subscribeEvents: vi.fn<(listener: unknown, refresh: unknown, options: unknown) => () => void>().mockReturnValue(offEvents) }
  const api = { devices: vi.fn(async () => ({ devices: [], generated_at: iso(T) })) }
  const store = new DevicesStore(api, 'connected')
  const cleanup = bindDevicesLifecycle(store, lifecycle)
  await Promise.resolve()
  expect(lifecycle.subscribeEvents.mock.calls[0][2]).toEqual({ refreshOnEvent: false })
  cleanup()
  expect(offEvents).toHaveBeenCalledOnce()
  expect(offStatus).toHaveBeenCalledOnce()
  expect(store.getSnapshot().trusted).toBe(false)
})

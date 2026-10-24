import { describe, expect, it } from 'vitest'
import { panelDeviceDetail, panelDevices, panelDeviceTone, panelScenes } from './panel-state'
import type { DeviceDetail } from './types'

const device = (id: string, connection: DeviceDetail['connection'] = 'online', health: DeviceDetail['health'] = 'healthy', freshness: DeviceDetail['telemetry_freshness'] = 'fresh') => ({
  id, name: id, type: id === 'workstation' ? 'windows' : 'linux', platform: 'test', connection, health, telemetry_freshness: freshness,
  last_seen_at: null, last_telemetry_at: null, status_reason: null, capabilities: {}, sources: [], metadata: {}, sensors: [],
} as DeviceDetail)

describe('Phone 2 panel presentation model', () => {
  it('keeps the health strip small and in stable room-device order', () => {
    const selected = panelDevices([device('phone_2'), device('phone'), device('workstation'), device('secondary'), device('primary')])
    expect(selected.map((item) => item.id)).toEqual(['primary', 'secondary', 'workstation', 'phone'])
  })

  it('does not call unknown, stale, degraded or offline evidence healthy', () => {
    expect(panelDeviceTone(device('primary'))).toBe('healthy')
    expect(panelDeviceTone(device('primary', 'unknown'))).toBe('unknown')
    expect(panelDeviceTone(device('primary', 'online', 'healthy', 'stale'))).toBe('stale')
    expect(panelDeviceTone(device('primary', 'online', 'degraded'))).toBe('degraded')
    expect(panelDeviceTone(device('primary', 'offline'))).toBe('offline')
  })

  it('expands split scenes into seven large, explicit targets', () => {
    const scenes = panelScenes()
    expect(scenes.map((scene) => scene.id)).toEqual(['normal', 'work_1', 'work_2', 'gaming_1', 'gaming_2', 'movie', 'night'])
    expect(new Set(scenes.map((scene) => scene.id)).size).toBe(scenes.length)
  })

  it('describes each device in one honest line (phones: Unknown, never Offline when silent)', () => {
    const now = Date.parse('2026-10-15T12:00:00Z')
    const src = { kind: 'roomos_native', configured: true, connection: 'unknown', last_seen_at: null, stale_after_seconds: 90 } as DeviceDetail['sources'][number]
    const sensor = (id: string, value: number) => ({ id, kind: 'number', support: 'supported', source: 'roomos_native', value, unit: '%', availability: 'available', freshness: 'fresh', observed_at: null, measured_at: null, reason: null }) as DeviceDetail['sensors'][number]
    const handset = { ...device('phone'), type: 'android', sources: [src], sensors: [sensor('battery_percent', 45.4)] } as DeviceDetail
    expect(panelDeviceDetail(handset, now)).toBe('Online · battery 45%')
    expect(panelDeviceDetail({ ...handset, connection: 'unknown', last_seen_at: '2026-10-15T11:48:00Z' }, now)).toBe('Unknown · last report 12 min ago')
    expect(panelDeviceDetail({ ...handset, connection: 'unknown' }, now)).toBe('Unknown · no report yet')
    expect(panelDeviceDetail({ ...handset, sources: [{ ...src, configured: false }] }, now)).toBe('Not connected')
    const primary = { ...device('primary'), sources: [{ ...src, kind: 'system_monitor' }], sensors: [{ ...sensor('cpu_usage', 21.6), source: 'system_monitor' }] } as DeviceDetail
    expect(panelDeviceDetail(primary, now)).toBe('Online · CPU 22%')
    expect(panelDeviceDetail({ ...primary, connection: 'offline', last_seen_at: '2026-10-15T10:00:00Z' }, now)).toBe('Offline · last report 2 h ago')
  })
})

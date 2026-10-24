import { describe, expect, it } from 'vitest'
import { DEVICE_TABS, sensorsForTab } from './device-tabs'
import { phone, sensor, server, workstation } from './devices-fixtures.test-support'

describe('device detail tabs', () => {
  it.each([['workstation', workstation()], ['phone', phone()], ['server', server()]])('keep every %s sensor reachable from exactly one detail tab', (_name, device) => {
    const withUnsupported = { ...device, sensors: [...device.sensors, sensor('gpu_temperature', { support: 'unsupported' }), sensor('storage_free', { support: 'unsupported' })].filter((s, i, all) => all.findIndex((o) => o.id === s.id) === i) }
    const detail = DEVICE_TABS.filter((tab) => tab !== 'Overview').flatMap((tab) => sensorsForTab(withUnsupported, tab).map((s) => s.id))
    expect([...detail].sort()).toEqual(withUnsupported.sensors.map((s) => s.id).sort())
    expect(new Set(detail).size).toBe(detail.length)
  })
  it('keeps the overview to the summary sensors', () => {
    expect(sensorsForTab(workstation(), 'Overview').every((s) => ['cpu_usage', 'ram_usage', 'disk_usage', 'battery_percent', 'ac_connected'].includes(s.id))).toBe(true)
  })
})

import { describe, expect, it } from 'vitest'
import { T, iso, phone, reportingPhone, sensor, server, workstation } from './devices-fixtures.test-support'
import { summarizeMainLights } from './main-lights'
import { lights as homeLights } from './home-fixtures.test-support'
import { roomLightsLabel, alertSummary, dashboardDevices, deviceRow, dnsView, overviewView, serviceRows, validTemperature } from './dashboard-state'
import type { AlertCatalog, AlertRecord, DnsClusterStatus, DnsResolverStatus, SystemData } from './types'

const resolver = (role: 'primary' | 'secondary', status: DnsResolverStatus['status'], ms: number | null = 4.2): DnsResolverStatus =>
  ({ id: role, role, status, last_checked: iso(T - 30_000), response_time_ms: ms })
const cluster = (status: DnsClusterStatus['status'], primary = resolver('primary', 'healthy'), secondary = resolver('secondary', 'healthy')): DnsClusterStatus =>
  ({ status, primary, secondary, last_checked: iso(T - 30_000) })
const node = (id: 'primary' | 'secondary') => ({ id, name: id, online: true, metrics: {} })
const system = (dns: DnsClusterStatus): SystemData => ({ primary: node('primary'), secondary: node('secondary'), dns_cluster: dns,
  services: [{ id: 'frigate', status: 'online', health: 'healthy', source: 'x', stale: false, freshness: 'fresh' }] })
const alert = (id: string, severity: AlertRecord['severity'], updated = '2026-10-15T08:00:00Z'): AlertRecord =>
  ({ id, type: 't', severity, title: id, message: 'm', source: 'workstation', started_at: '2026-10-15T07:00:00Z', updated_at: updated, status: 'active' })

describe('device rows', () => {
  it('shows CPU/RAM/Disk and a real temperature for a live server', () => {
    const row = deviceRow(server(), true, T)
    expect(row.tone).toBe('healthy')
    expect(row.metrics.map((metric) => `${metric.label} ${metric.text}`)).toEqual(['CPU 12%', 'RAM 40%', 'Disk 30%', 'Temp 51°C'])
  })

  it('never shows a 0 °C sentinel or an unverified temperature as real', () => {
    expect(validTemperature(sensor('gpu_temperature', { value: 0, unit: '°C' }))).toBeNull()
    expect(validTemperature(sensor('cpu_temperature', { value: 64, unit: '°C', support: 'unverified' }))).toBeNull()
    const hot = workstation({ overrides: {} })
    hot.sensors = [...hot.sensors.filter((item) => item.id !== 'cpu_temperature'), sensor('cpu_temperature', { value: 0, unit: '°C' })]
    expect(deviceRow(hot, true, T).metrics.map((metric) => metric.key)).not.toContain('temp')
  })

  it('omits unverified GPU usage and flags high disk', () => {
    const row = deviceRow(workstation({ disk: 97 }), true, T)
    expect(row.metrics.map((metric) => metric.key)).toEqual(['cpu', 'ram', 'disk'])
    expect(row.metrics.find((metric) => metric.key === 'disk')?.high).toBe(true)
  })

  it('a switched-off Workstation is "off or asleep", with no live values', () => {
    const row = deviceRow(workstation({ connection: 'offline' }), true, T)
    expect(row).toMatchObject({ connection: 'offline', tone: 'offline', status: 'Off or asleep', metrics: [] })
  })

  it('untrusted live state is never current or online', () => {
    const row = deviceRow(server(), false, T)
    expect(row.connection).toBe('unknown')
    expect(row.current).toBe(false)
    expect(row.metrics.every((metric) => !metric.current)).toBe(true)
  })

  it('stale sensors are marked, phones that are silent read "Not reporting"', () => {
    expect(deviceRow(server(), true, T).tone).toBe('healthy')
    const stale = deviceRow(reportingPhone({ freshness: 'stale' }), true, T)
    expect(stale.metrics.every((metric) => !metric.current)).toBe(true)
    const silent = deviceRow(reportingPhone({ connection: 'unknown' }), true, T)
    expect(silent.status).toBe('Not reporting')
  })

  it('lists only devices with a configured telemetry source, servers first', () => {
    const ids = dashboardDevices([phone(), workstation(), server('secondary', 'Secondary'), server()]).map((device) => device.id)
    expect(ids).toEqual(['primary', 'secondary', 'workstation'])
  })
})

describe('services and DNS', () => {
  it('names the resolver that stopped answering and says redundancy is lost', () => {
    const view = dnsView(system(cluster('degraded', resolver('primary', 'healthy'), resolver('secondary', 'offline', null))), true, T)
    expect(view.tone).toBe('degraded')
    expect(view.headline).toBe('DNS degraded · 1/2')
    expect(view.problem).toContain('Secondary DNS is not answering')
    expect(view.problem).toContain('Redundancy lost')
  })

  it('does not present last-known DNS as current', () => {
    expect(dnsView(system(cluster('healthy')), false, T)).toMatchObject({ tone: 'stale', problem: null })
    expect(dnsView(null, true, T).tone).toBe('unknown')
  })

  it('reports MQTT as not monitored instead of inventing a state', () => {
    const rows = serviceRows({ system: system(cluster('healthy')), control: null, connection: 'connected', live: true, now: T })
    expect(rows.find((row) => row.id === 'mqtt')).toMatchObject({ tone: 'unknown', status: 'Not monitored' })
    expect(rows.map((row) => row.id)).toEqual(['roomos', 'home_assistant', 'mqtt', 'dns_primary', 'dns_secondary', 'frigate', 'nextcloud'])
  })

  it('every service is last known while the connection is not live', () => {
    const rows = serviceRows({ system: system(cluster('healthy')), control: null, connection: 'reconnecting', live: false, now: T })
    expect(rows.filter((row) => row.id !== 'roomos' && row.id !== 'mqtt').every((row) => row.tone === 'stale')).toBe(true)
  })
})

describe('alerts and overview', () => {
  it('counts by severity and orders critical first', () => {
    const catalog: AlertCatalog = { active: [alert('w', 'warning', '2026-10-15T08:30:00Z'), alert('c', 'critical')], active_count: 2, highest_severity: 'critical', storage_status: 'ok' }
    const summary = alertSummary(catalog)
    expect(summary).toMatchObject({ critical: 1, warning: 1, total: 2, tone: 'offline', latest: '2026-10-15T08:30:00Z' })
    expect(summary.top.map((item) => item.id)).toEqual(['c', 'w'])
  })

  it('is never "everything normal" while not live', () => {
    const view = overviewView({ connection: 'reconnecting', healthReport: { overall: 'healthy', checked_at: iso(T), components: [] }, healthStatus: 'ready',
      devices: [server()], devicesTrusted: false, devicesAllowed: true, alerts: { active: [], active_count: 0, highest_severity: null, storage_status: 'ok' }, alertsAllowed: true,
      system: system(cluster('healthy')), systemAllowed: true, systemLive: false, now: T })
    expect(view.live).toBe(false)
    expect(view.tone).not.toBe('healthy')
    expect(view.headline).toContain('last known')
    expect(view.tiles.find((tile) => tile.id === 'alerts')).toMatchObject({ tone: 'stale', detail: 'All clear · last known' })
  })

  it('a degraded DNS cluster makes the overview ask for attention', () => {
    const view = overviewView({ connection: 'connected', healthReport: { overall: 'healthy', checked_at: iso(T), components: [] }, healthStatus: 'ready',
      devices: [server(), server('secondary', 'Secondary')], devicesTrusted: true, devicesAllowed: true, alerts: { active: [], active_count: 0, highest_severity: null, storage_status: 'ok' }, alertsAllowed: true,
      system: system(cluster('degraded', resolver('primary', 'healthy'), resolver('secondary', 'offline'))), systemAllowed: true, systemLive: true, now: T })
    expect(view.tone).toBe('degraded')
    expect(view.tiles.find((tile) => tile.id === 'dns')).toMatchObject({ value: '1/2', detail: 'Redundancy lost' })
    expect(view.tiles.find((tile) => tile.id === 'devices')).toMatchObject({ value: '2/2' })
  })

  it('omits tiles this device may not read', () => {
    const view = overviewView({ connection: 'connected', healthReport: null, healthStatus: 'forbidden', devices: [], devicesTrusted: true, devicesAllowed: false,
      alerts: null, alertsAllowed: false, system: null, systemAllowed: false, systemLive: false, now: T })
    expect(view.tiles).toEqual([])
  })
})

describe('room lights label', () => {
  it('is English on the dashboard and counts unavailable lights', () => {
    const lights = homeLights.map((light) => light.id === 'desk_ambient' ? light : { ...light, available: true, on: light.id !== 'room_4' })
    expect(roomLightsLabel(summarizeMainLights(lights)!)).toBe('Partly on · 3/4')
    expect(roomLightsLabel(summarizeMainLights(lights.map((light) => light.id === 'room_2' ? { ...light, available: false, on: null } : light))!)).toContain('1 unavailable')
  })
})

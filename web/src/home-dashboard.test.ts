import { describe, expect, it } from 'vitest'
import { dashboardStatusLabel, homeServices, nodeDashboardStatus, overallDashboardStatus, overallStatusLabel, serviceDashboardStatus } from './home-dashboard'
import type { Health, Light, Metric, ServiceStatus, SystemData, SystemNode } from './types'

const metric = (id: string, value: number | boolean | string | null, options: Partial<Metric> = {}): Metric => ({
  id,
  value,
  unit: null,
  available: value !== null,
  stale: false,
  freshness: 'fresh',
  last_updated: '2026-10-03T07:00:00Z',
  ...options,
})

const node = (id: 'primary' | 'secondary', options: { online?: boolean | null; stale?: boolean; missing?: boolean } = {}): SystemNode => ({
  id,
  name: id === 'primary' ? 'Primary' : 'Secondary',
  online: options.online === undefined ? true : options.online,
  metrics: {
    [`${id}_online`]: metric(`${id}_online`, options.online === undefined ? true : options.online, { stale: options.stale ?? false, freshness: options.stale ? 'stale' : 'fresh' }),
    [`${id}_cpu`]: metric(`${id}_cpu`, options.missing ? null : 20, { unit: '%' }),
    [`${id}_ram`]: metric(`${id}_ram`, 30, { unit: '%' }),
    [`${id}_temperature`]: metric(`${id}_temperature`, 42, { unit: '°C' }),
  },
})

const service = (id: string, status: ServiceStatus['status'], stale = false, health: ServiceStatus['health'] = status === 'online' ? 'healthy' : status): ServiceStatus => ({ id, status, health, stale, freshness: stale ? 'stale' : status === 'unknown' ? 'unknown' : 'fresh', source: 'fixture' })

const health: Health = {
  status: 'ok', home_assistant: 'connected', live_connection_enabled: true,
  control_enabled: true, control_policy: 'production', writable_device_ids: [], writable_actions: [], writable_permissions: {},
  alerts: { status: 'unknown', active_count: 0, highest_severity: null },
  notifications: { status: 'disabled', configured_providers: 0, configured_destinations: 0, pending_count: 0, failed_count: 0, external_attempts: 0, external_deliveries: 0, last_delivery: null },
}

const light = (id: string, available = true): Light => ({
  id, name: id, type: 'light', available, on: false, brightness: null, color_mode: null,
  rgb: null, hs: null, color_temperature_kelvin: null, effect: null, effects: [],
  capabilities: { power: true, brightness: true, color: true, temperature: true, effects: false, min_kelvin: 2500, max_kelvin: 6500 },
  last_updated: null,
})

const system = (primary = node('primary'), secondary = node('secondary')): SystemData => ({
  primary,
  secondary,
  services: [
    service('home_assistant', 'online'),
    service('frigate', 'online'),
    service('dns', 'unknown'),
    service('nextcloud', 'online'),
  ],
  dns_cluster: {
    status: 'unknown',
    primary: { id: 'primary', role: 'primary', status: 'unknown', last_checked: null, response_time_ms: null },
    secondary: { id: 'secondary', role: 'secondary', status: 'unknown', last_checked: null, response_time_ms: null },
    last_checked: null,
  },
})

describe('home dashboard truth model', () => {
  it('keeps Healthy, Degraded, Offline, Unknown and Stale semantically distinct', () => {
    expect(dashboardStatusLabel('healthy')).toBe('Healthy')
    expect(dashboardStatusLabel('degraded')).toBe('Degraded')
    expect(dashboardStatusLabel('offline')).toBe('Offline')
    expect(dashboardStatusLabel('unknown')).toBe('Unknown')
    expect(dashboardStatusLabel('stale')).toBe('Stale')
    expect(serviceDashboardStatus(service('dns', 'unknown'))).toBe('unknown')
    expect(serviceDashboardStatus(service('nextcloud', 'online', true))).toBe('stale')
    expect(serviceDashboardStatus(service('frigate', 'offline'))).toBe('offline')
    expect(serviceDashboardStatus(service('dns', 'online', false, 'degraded'))).toBe('degraded')
    expect(serviceDashboardStatus({ ...service('frigate', 'offline'), freshness: 'stale', stale: true })).toBe('offline')
  })

  it('never reports a stale or incomplete node as healthy', () => {
    expect(nodeDashboardStatus(node('primary'))).toBe('healthy')
    expect(nodeDashboardStatus(node('primary', { stale: true }))).toBe('stale')
    expect(nodeDashboardStatus(node('primary', { missing: true }))).toBe('degraded')
    expect(nodeDashboardStatus(node('primary', { online: false }))).toBe('offline')
    expect(nodeDashboardStatus(node('primary', { online: null }))).toBe('unknown')
  })

  it('derives exactly the four requested service summaries without inventing DNS health', () => {
    const summaries = homeServices(system(), health)
    expect(summaries.map((item) => item.id)).toEqual(['home_assistant', 'frigate', 'dns', 'nextcloud'])
    expect(summaries.find((item) => item.id === 'dns')?.status).toBe('unknown')
    expect(summaries.find((item) => item.id === 'home_assistant')?.status).toBe('healthy')
  })

  it('shows attention for unavailable lights, stale telemetry and unknown services without calling them offline', () => {
    const lights = ['one', 'two', 'three', 'four', 'five'].map((id) => light(id))
    expect(overallDashboardStatus(health, lights, { ...system(), services: system().services.map((item) => item.id === 'dns' ? service('dns', 'online') : item) })).toBe('healthy')
    expect(overallDashboardStatus(health, lights.map((item, index) => index === 0 ? { ...item, available: false } : item), system())).toBe('degraded')
    expect(overallDashboardStatus(health, lights, system(node('primary', { stale: true })))).toBe('stale')
    expect(overallDashboardStatus(health, lights, system())).toBe('unknown')
    expect(overallStatusLabel('unknown')).toBe('Attention needed')
  })

  it('uses the canonical alert summary for dashboard health without exposing details', () => {
    const lights = ['one', 'two', 'three', 'four', 'five'].map((id) => light(id))
    expect(overallDashboardStatus({ ...health, alerts: { status: 'operational', active_count: 0, highest_severity: null } }, lights, system())).toBe('healthy')
    expect(overallDashboardStatus({ ...health, alerts: { status: 'attention', active_count: 1, highest_severity: 'warning' } }, lights, system())).toBe('degraded')
    expect(overallDashboardStatus({ ...health, alerts: { status: 'critical', active_count: 1, highest_severity: 'critical' } }, lights, system())).toBe('offline')
    expect(overallStatusLabel('offline')).toBe('Critical issue')
  })
})

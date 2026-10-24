// @vitest-environment jsdom

import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { catalog, health, lights } from '../home-fixtures.test-support'
import { automationCatalog, smartAutomation } from '../routines-fixtures.test-support'
import { server, workstation } from '../devices-fixtures.test-support'
import type { AuthStatus } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const harness = vi.hoisted(() => {
  const statusListeners = new Set<(status: string) => void>()
  const eventListeners = new Set<(event: unknown) => void>()
  const refreshListeners = new Set<() => Promise<void>>()
  const lifecycle = {
    snapshot: 'reconnecting' as string,
    subscribeStatus(listener: (status: string) => void) { statusListeners.add(listener); listener(lifecycle.snapshot); return () => { statusListeners.delete(listener) } },
    subscribeEvents(listener: (event: unknown) => void, refresh?: () => Promise<void>) {
      eventListeners.add(listener)
      if (refresh) refreshListeners.add(refresh)
      return () => { eventListeners.delete(listener); if (refresh) refreshListeners.delete(refresh) }
    },
  }
  const setStatus = (status: string) => {
    if (status === lifecycle.snapshot) return
    lifecycle.snapshot = status
    statusListeners.forEach((listener) => listener(status))
    if (status === 'connected') refreshListeners.forEach((refresh) => void refresh())
  }
  const emit = (event: unknown) => eventListeners.forEach((listener) => listener(event))
  const names = ['health', 'lights', 'scenes', 'systemHealth', 'alerts', 'routines', 'automations', 'devices', 'system', 'cameras', 'metricHistory', 'runRoutine',
    'activateScene', 'allPower', 'power', 'brightness', 'color', 'temperature', 'effect', 'mainPower', 'mainBrightness', 'mainTemperature']
  const api = Object.fromEntries(names.map((name) => [name, vi.fn()])) as Record<string, ReturnType<typeof vi.fn>>
  return { lifecycle, setStatus, emit, api, eventListeners }
})

vi.mock('../lifecycle', () => ({ roomosLifecycle: harness.lifecycle }))
vi.mock('../api', async (original) => ({ ...(await original<typeof import('../api')>()), api: harness.api }))

import { AuthContext } from '../auth-context'
import { DashboardPage } from './Dashboard'
import { WidgetBoundary } from '../components/DashboardWidgets'
import { storageKey } from '../dashboard-layout'

const ALL = ['lights:read', 'lights:write', 'system:read', 'routines:read', 'activity:read', 'devices:read']
const authFor = (scopes: string[], id = 'dev_a') => ({ authenticated: true, device: { id, scopes } }) as unknown as AuthStatus
const dns = { status: 'degraded', last_checked: '2026-10-18T10:00:00Z',
  primary: { id: 'primary', role: 'primary', status: 'healthy', last_checked: '2026-10-18T10:00:00Z', response_time_ms: 3 },
  secondary: { id: 'secondary', role: 'secondary', status: 'offline', last_checked: '2026-10-18T10:00:00Z', response_time_ms: null } }
const metric = (id: string, value: unknown) => ({ id, value, unit: '%', available: true, stale: false, freshness: 'fresh', last_updated: '2026-10-18T10:00:00Z' })
const system = { primary: { id: 'primary', name: 'Primary', online: true, metrics: { primary_online: metric('primary_online', true) } },
  secondary: { id: 'secondary', name: 'Secondary', online: true, metrics: { secondary_online: metric('secondary_online', true) } }, services: [], dns_cluster: dns }
const history = { device_id: 'primary', metric: 'cpu_usage', range: '1h', status: 'ok', unit: '%', step_seconds: 60, generated_at: '2026-10-18T10:00:00Z',
  points: [10, 20, null, 30].map((avg, index) => ({ ts: `2026-10-18T09:5${index}:00Z`, avg, min: avg, max: avg, samples: avg === null ? 0 : 1, source: 'system_monitor', provenance: 'measured' })) }

let root: Root
let container: HTMLDivElement
const calls = (name: string) => harness.api[name].mock.calls.length

beforeEach(() => {
  const responses: Record<string, unknown> = {
    health, lights, scenes: catalog, systemHealth: { overall: 'healthy', checked_at: '2026-10-18T10:00:00Z', components: [] },
    alerts: { active: [], active_count: 0, highest_severity: null, storage_status: 'ok' },
    routines: { routines: [], timezone: 'Europe/Istanbul', storage_status: 'ok', can_write: false, can_run: false, ha_trigger_ids: [], scenes: [] }, automations: automationCatalog(),
    devices: { devices: [server(), workstation()], generated_at: '2026-10-18T10:00:00Z' }, system, metricHistory: history,
  }
  for (const [name, mock] of Object.entries(harness.api)) { mock.mockReset(); mock.mockImplementation(async () => responses[name]) }
  harness.lifecycle.snapshot = 'reconnecting'
  localStorage.clear()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
})

async function mount(auth = authFor(ALL)) {
  await act(async () => { root.render(<AuthContext.Provider value={auth}><MemoryRouter><DashboardPage /></MemoryRouter></AuthContext.Provider>) })
}
const settle = () => act(async () => { for (let i = 0; i < 8; i++) await Promise.resolve() })
async function connect() { await act(async () => harness.setStatus('connected')); await settle() }
const widgets = () => [...container.querySelectorAll<HTMLElement>('[data-widget]')].map((element) => element.dataset.widget)
const button = (label: string | RegExp) => [...container.querySelectorAll('button')].find((element) => (typeof label === 'string' ? element.textContent?.trim() === label || element.getAttribute('aria-label') === label : label.test(element.textContent ?? '') || label.test(element.getAttribute('aria-label') ?? '')))!
const click = async (element: Element) => { await act(async () => { element.dispatchEvent(new MouseEvent('click', { bubbles: true })) }); await settle() }

describe('Dashboard 2.0 mount', () => {
  it('bootstraps each resource once over the shared connection and never reads the camera', async () => {
    await mount()
    expect(calls('lights')).toBe(0) // nothing before the socket is live
    await connect()
    expect({ lights: calls('lights'), health: calls('health'), system: calls('system'), devices: calls('devices'), alerts: calls('alerts'), routines: calls('routines'), automations: calls('automations') })
      .toEqual({ lights: 1, health: 1, system: 1, devices: 1, alerts: 1, routines: 1, automations: 1 })
    expect(calls('cameras')).toBe(0)
    expect(calls('metricHistory')).toBe(1)
    expect(harness.api.metricHistory.mock.calls[0]).toEqual(['primary', 'cpu_usage', '1h'])
    expect(widgets()).toEqual(['system_overview', 'lights', 'alerts', 'devices', 'services', 'performance', 'routines'])
    expect(container.textContent).toContain('Secondary DNS is not answering')
    expect(container.textContent).toContain('Not monitored')
  })

  it('ordinary events update in place without any REST request', async () => {
    await mount(); await connect()
    const before = Object.keys(harness.api).reduce((sum, name) => sum + calls(name), 0)
    await act(async () => {
      harness.emit({ type: 'system.snapshot_updated', data: { system: { ...system, dns_cluster: { ...dns, status: 'healthy', secondary: { ...dns.secondary, status: 'healthy', response_time_ms: 5 } } } } })
      harness.emit({ type: 'light.state_changed', device_id: 'room_main', data: { light: { ...lights[0], on: true } } })
      harness.emit({ type: 'system.service_updated', device_id: 'dns', data: { service: 'dns', status: 'healthy' } })
    })
    await settle()
    expect(Object.keys(harness.api).reduce((sum, name) => sum + calls(name), 0)).toBe(before)
    expect(container.textContent).toContain('DNS redundant · 2/2')
    await act(async () => harness.emit({ type: 'automation.changed', data: { automation_id: smartAutomation.id, automation: { ...smartAutomation, enabled: true, updated_at: '2026-10-19T11:00:00Z' } } }))
    await settle()
    expect(Object.keys(harness.api).reduce((sum, name) => sum + calls(name), 0)).toBe(before)
    expect(container.textContent).toContain('1 smart active')
  })

  it('does not render or read widgets this device may not use', async () => {
    await mount(authFor(['lights:read']))
    await connect()
    expect(widgets()).toEqual(['system_overview', 'lights', 'routines'])
    expect(calls('system')).toBe(0)
    expect(calls('devices')).toBe(0)
    expect(calls('metricHistory')).toBe(0)
  })

  it('edits, saves per device, cancels and resets without touching widget controls', async () => {
    await mount(); await connect()
    await click(button('Edit dashboard'))
    expect(container.querySelector('.dash-editbar')).not.toBeNull()
    const bodies = [...container.querySelectorAll('.dash-widget-body')]
    expect(bodies.every((body) => body.hasAttribute('inert'))).toBe(true)
    expect(button('Hide System overview')).toBeUndefined() // pinned
    await click(button('Move Lights down'))
    await click(button('Hide Performance'))
    expect(widgets()).not.toContain('performance')
    expect(container.textContent).toContain('Show Performance')
    await click(button('Save'))
    const saved = JSON.parse(localStorage.getItem(storageKey('dev_a'))!)
    expect(saved.widgets.map((item: { id: string }) => item.id).slice(0, 3)).toEqual(['system_overview', 'alerts', 'lights'])
    expect(saved.widgets.find((item: { id: string }) => item.id === 'performance').visible).toBe(false)
    expect(localStorage.getItem(storageKey('dev_b'))).toBeNull()
    expect(writesSent()).toBe(0)

    await click(button('Edit dashboard'))
    await click(button('Move Alerts down'))
    await click(button('Cancel'))
    expect(widgets().slice(0, 3)).toEqual(['system_overview', 'alerts', 'lights'])

    await click(button('Edit dashboard'))
    await click(button('Reset to default'))
    await click(button('Save'))
    expect(widgets()).toEqual(['system_overview', 'lights', 'alerts', 'devices', 'services', 'performance', 'routines'])
  })

  it('survives a corrupt stored layout', async () => {
    localStorage.setItem(storageKey('dev_a'), '{"version":1,"widgets":[{"id":"evil","size":"huge"},42]}')
    await mount(); await connect()
    expect(widgets()).toHaveLength(7)
  })

  it('keeps one widget failure local', async () => {
    const Boom = () => { throw new Error('boom') }
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    await act(async () => { root.render(<div><WidgetBoundary title="Devices"><Boom /></WidgetBoundary><p>still here</p></div>) })
    expect(container.textContent).toContain('Devices could not be shown')
    expect(container.textContent).toContain('still here')
    spy.mockRestore()
  })
})

function writesSent() {
  return ['runRoutine', 'activateScene', 'allPower', 'power', 'brightness', 'color', 'temperature', 'effect', 'mainPower', 'mainBrightness', 'mainTemperature'].reduce((sum, name) => sum + calls(name), 0)
}

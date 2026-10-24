import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { HealthPanel, HealthSection, HealthUnavailable, HealthView } from './Health'
import type { HealthSnapshot } from '../health-store'
import { HealthContractError, orderComponents, overallHeadline, parseHealth } from '../health'
import { MemoryRouter } from 'react-router-dom'
import { SystemPage } from '../pages/System'
import type { HealthComponent, HealthReport } from '../types'

const at = '2026-10-07T09:00:00Z'
const component = (id: HealthComponent['id'], name: string, status: HealthComponent['status'] = 'healthy', summary = 'OK', details: string[] = []): HealthComponent => ({
  id, name, status, critical: ['home_assistant', 'control', 'persistent_state', 'automations'].includes(id), summary, details, checked_at: at,
})

function report(overrides: Partial<Record<HealthComponent['id'], Partial<HealthComponent>>> = {}, overall: HealthReport['overall'] = 'healthy'): HealthReport {
  const base = [
    component('home_assistant', 'Home Assistant', 'healthy', 'Connected · live updates active'),
    component('control', 'Control', 'healthy', 'Ready · 8 scenes'),
    component('persistent_state', 'Persistent state', 'healthy', 'All stores healthy'),
    component('automations', 'Automations', 'healthy', 'Scheduler running · 2 of 2 enabled'),
    component('execution_history', 'Execution history', 'healthy', 'Writable · no executions yet'),
    component('notifications', 'Notifications', 'healthy', 'Dispatcher ready · 1 destination'),
    component('telemetry_history', 'Telemetry history', 'healthy', 'Recorder history available'),
    component('backups', 'Backups', 'unknown', 'Not monitored · encrypted backups are manual'),
  ]
  return { overall, checked_at: at, components: base.map((item) => ({ ...item, ...overrides[item.id] })) }
}

describe('health contract', () => {
  it('accepts the bounded server response', () => {
    expect(parseHealth(report()).components).toHaveLength(8)
  })

  it('accepts the background services component from Core 7E.1', () => {
    const payload = report()
    payload.components.push(component('background_services', 'Background services', 'degraded', '1 of 8 background services stopped', ['Device registry stopped unexpectedly']))
    const parsed = parseHealth(payload)
    expect(parsed.components).toHaveLength(9)
    expect(parsed.components[8]).toMatchObject({ id: 'background_services', critical: false, status: 'degraded' })
    const html = renderToStaticMarkup(<HealthPanel report={{ ...parsed, overall: 'degraded' }} />)
    expect(html).toContain('data-health="background_services"')
    expect(html).toContain('Device registry stopped unexpectedly')
  })

  it.each([
    ['extra top-level field', { ...report(), entity_id: 'light.x' }],
    ['unknown component id', { ...report(), components: [{ ...component('control', 'Control'), id: 'sql' }] }],
    ['unknown status', { ...report(), components: [{ ...component('control', 'Control'), status: 'fine' }] }],
    ['unbounded summary', { ...report(), components: [component('control', 'Control', 'healthy', 'x'.repeat(121))] }],
    ['bad overall', { ...report(), overall: 'unknown' }],
  ])('rejects %s', (_, payload) => {
    expect(() => parseHealth(payload)).toThrow(HealthContractError)
  })
})

describe('RoomOS Health UI', () => {
  it('renders all-healthy compactly with text status and no interactive noise', () => {
    const html = renderToStaticMarkup(<HealthPanel report={report()} />)
    expect(html).toContain('RoomOS Health')
    expect(html).toContain('RoomOS is operating normally')
    expect((html.match(/data-health="/g) ?? []).length).toBe(8)
    expect((html.match(/>Healthy</g) ?? []).length).toBe(8) // overall + 7 components
    expect(html).toContain('Not monitored')
    expect(html).not.toContain('<details')
    expect(html).not.toMatch(/sensor\.|192\.168|\/var\/lib|token/i)
  })

  it('surfaces one degraded component first with its sentence', () => {
    const data = report({ execution_history: { status: 'degraded', summary: 'Writes are currently failing; control continues' } }, 'degraded')
    const html = renderToStaticMarkup(<HealthPanel report={data} />)
    expect(html).toContain('data-overall="degraded"')
    expect(html).toContain('1 subsystem needs attention')
    expect(html.indexOf('data-health="execution_history"')).toBeLessThan(html.indexOf('data-health="home_assistant"'))
    expect(html).toContain('Writes are currently failing; control continues')
    expect(html).toContain('>Degraded<')
  })

  it('shows unhealthy distinctly from degraded, in text', () => {
    const data = report({ automations: { status: 'unhealthy', summary: 'Scheduler is not running' } }, 'unhealthy')
    const html = renderToStaticMarkup(<HealthPanel report={data} />)
    expect(html).toContain('>Unhealthy<')
    expect(html).toContain('RoomOS impaired')
    expect(html).toContain('is-unhealthy')
  })

  it('orders multiple problems unhealthy → degraded → healthy → not monitored', () => {
    const data = report({ telemetry_history: { status: 'degraded' }, notifications: { status: 'unhealthy' }, persistent_state: { status: 'degraded' } }, 'degraded')
    expect(orderComponents(data.components).map((item) => item.id)).toEqual([
      'notifications', 'persistent_state', 'telemetry_history', 'home_assistant', 'control', 'automations', 'execution_history', 'backups',
    ])
    expect(overallHeadline(data)).toBe('3 subsystems need attention')
  })

  it('makes only components with details expandable, collapsed by default', () => {
    const data = report({ persistent_state: { status: 'degraded', summary: '1 store needs attention', details: ['Activity & alerts: writes failing'] } }, 'degraded')
    const html = renderToStaticMarkup(<HealthPanel report={data} />)
    expect((html.match(/<details/g) ?? []).length).toBe(1)
    expect(html).not.toContain('<details open')
    expect(html).toContain('<summary class="health-row-head">')
    expect(html).toContain('Activity &amp; alerts: writes failing')
    expect(html).toContain('aria-label="Persistent state details"')
  })

  it('renders loading and API-failure states with retry', () => {
    expect(renderToStaticMarkup(<HealthSection />)).toContain('Reading RoomOS health')
    const failed = renderToStaticMarkup(<HealthUnavailable message="RoomOS Core is temporarily unavailable." onRetry={() => undefined} />)
    expect(failed).toContain('role="alert"')
    expect(failed).toContain('Retry')
    expect(failed).toContain('type="button"')
  })

  it('appears on the System page independently of system data', () => {
    const html = renderToStaticMarkup(<MemoryRouter><SystemPage /></MemoryRouter>)
    expect(html).toContain('RoomOS Health')
  })
})

describe('Backups health rendering (Phase 6D.4)', () => {
  const backup = (status: HealthComponent['status'], summary: string, details: string[] = []) =>
    report({ backups: { status, summary, details } }, status === 'healthy' || status === 'unknown' ? 'healthy' : 'degraded')

  it.each([
    ['unknown', 'Waiting for the first backup status', 'Not monitored'],
    ['healthy', 'Restore-tested · 3 h ago', 'Healthy'],
    ['degraded', 'Last backup failed · protected copy 3 h ago', 'Degraded'],
    ['unhealthy', 'No restore-tested backup for 8 days', 'Unhealthy'],
    ['degraded', 'Backup in progress', 'Degraded'],
  ] as const)('renders %s backup state with text label', (status, summary, label) => {
    const html = renderToStaticMarkup(<HealthPanel report={backup(status, summary)} />)
    expect(html).toContain(summary)
    const row = html.slice(html.indexOf('data-health="backups"'))
    expect(row).toContain(`>${label}<`)
  })

  it('shows safe backup details in a collapsed expandable row', () => {
    const details = ['Last restore-tested backup: 3 h ago', 'Schedule: Daily 04:30 Europe/Istanbul']
    const html = renderToStaticMarkup(<HealthPanel report={backup('healthy', 'Restore-tested · 3 h ago', details)} />)
    expect(html).toContain('aria-label="Backups details"')
    expect(html).not.toContain('<details open')
    for (const detail of details) expect(html).toContain(detail)
    expect(html).not.toMatch(/\/home|\/var|\.roomos-backup|backup\.key|\/root/i)
  })
})

describe('Health freshness presentation', () => {
  const state = (change: Partial<HealthSnapshot> = {}): HealthSnapshot => ({
    report: report(), freshness: 'fresh', refreshing: false,
    lastSuccessAt: Date.parse(at), lastAttemptAt: Date.parse(at), lastAttemptResult: 'success', error: null, ageMs: 1_000, ...change,
  })
  it('shows current data, separate attempt/success metadata and retry', () => {
    const html = renderToStaticMarkup(<HealthView state={state()} onRetry={() => undefined} />)
    for (const text of ['Current data', 'Last successful check', 'Last attempt', 'Succeeded', 'Report age', '1s', 'Retry']) expect(html).toContain(text)
    expect(html).toContain('data-overall="healthy"')
  })
  it('never presents a retained healthy report as current after an error', () => {
    const html = renderToStaticMarkup(<HealthView state={state({ freshness: 'stale', error: 'Health API or connection error.', lastAttemptResult: 'error' })} onRetry={() => undefined} />)
    for (const text of ['Last known data', 'current system health is unknown', 'Last known: Healthy', 'Failed', 'role="alert"']) expect(html).toContain(text)
    expect(html).toContain('data-overall="unknown"')
    expect(html).not.toContain('RoomOS is operating normally')
    expect(html).not.toContain('data-overall="healthy"')
    expect(html).not.toContain('>Healthy<')
  })
  it('shows stale data without falsely inventing a network error', () => {
    const html = renderToStaticMarkup(<HealthView state={state({ freshness: 'stale', ageMs: 120_000 })} onRetry={() => undefined} />)
    expect(html).toContain('Stale data'); expect(html).toContain('2m 0s')
    expect(html).not.toContain('role="alert"')
  })
  it('distinguishes unavailable/no-data from historical data', () => {
    const html = renderToStaticMarkup(<HealthView state={state({ report: null, freshness: 'unavailable', lastSuccessAt: null, error: 'Health request timed out.', lastAttemptResult: 'error', ageMs: null })} onRetry={() => undefined} />)
    for (const text of ['Health unavailable', 'No health data received yet', 'Never', 'timed out']) expect(html).toContain(text)
    expect(html).not.toContain('data-overall')
  })
  it('disables retry while checking without hiding stale warnings', () => {
    const html = renderToStaticMarkup(<HealthView state={state({ freshness: 'stale', refreshing: true, lastAttemptResult: 'pending', error: 'Connection error' })} onRetry={() => undefined} />)
    expect(html).toContain('disabled=""'); expect(html).toContain('Checking…')
    expect(html).toContain('Last known data'); expect(html).toContain('Connection error')
  })
})

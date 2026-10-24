import type { ExecutionRecord } from '../types'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { ActivityDashboard, ActivityLocked, ActivityRow } from './Activity'
import type { ActivityCatalog, AlertCatalog } from '../types'

const alerts: AlertCatalog = { active: [{ id: 'service.dns.degraded', type: 'service.degraded', severity: 'warning', title: 'DNS redundancy degraded', message: 'Secondary resolver unavailable.', source: 'dns', started_at: '2026-10-03T10:00:00Z', updated_at: '2026-10-03T10:00:00Z', status: 'active' }], active_count: 1, highest_severity: 'warning', storage_status: 'ok' }
const history: ActivityCatalog = { activity: [{ id: 'act_00000000000000000000000000000000', type: 'scene_activated', title: 'Night activated', message: 'Scene completed successfully.', source: 'scene', occurred_at: '2026-10-03T10:00:00Z', severity: null, reference_id: 'night' }], total: 1, limit: 50, offset: 0, storage_status: 'ok' }

describe('Activity UI', () => {
  it('renders an explicit authorization gate', () => {
    const html = renderToStaticMarkup(<ActivityLocked />)
    expect(html).toContain('Activity access required')
    expect(html).toContain('explicitly authorized')
  })
  it('renders active alerts, bounded history, and accessible filters', () => {
    const html = renderToStaticMarkup(<ActivityDashboard alerts={alerts} history={history} filter="all" onFilter={vi.fn()} />)
    expect(html).toContain('DNS redundancy degraded')
    expect(html).toContain('Secondary resolver unavailable')
    expect(html).toContain('Night activated')
    expect(html).toContain('aria-pressed="true"')
    expect(html).not.toContain('192.0.2.')
    expect(html).not.toContain('entity_id')
  })
  it('renders a quiet empty state without fake alerts', () => {
    const html = renderToStaticMarkup(<ActivityDashboard alerts={{ ...alerts, active: [], active_count: 0, highest_severity: null }} history={{ ...history, activity: [], total: 0 }} filter="all" onFilter={vi.fn()} />)
    expect(html).toContain('All systems operational')
    expect(html).toContain('No recent activity')
  })
})

describe('persistent execution details', () => {
  const execution = (result: ExecutionRecord['result'] = 'partial'): ExecutionRecord => ({ id: 'ex_'+'1'.repeat(32), source: 'routine', trigger: 'ha_state', routine_id: 'rt_'+'1'.repeat(32), routine_name: 'Evening Home Lighting', scene_id: 'normal', scene_name: 'Normal', result, reason: result === 'interrupted' ? 'interrupted' : null, started_at: '2026-10-07T12:00:00Z', finished_at: '2026-10-07T12:00:00.412Z', duration_ms: 412, targets: [
    ...['room_main','room_2','room_3','room_4'].map(device_id => ({ device_id, actions: ['power'] as const as ['power'], result: 'skipped' as const, reason: 'unavailable', stage: 'read' as const, duration_ms: 12 })),
    { device_id: 'desk_ambient', actions: ['power'], result: 'applied', reason: null, stage: 'apply', duration_ms: 20 },
  ] })
  const render = (record?: ExecutionRecord) => renderToStaticMarkup(<ActivityRow item={{ id: 'legacy', type: 'scene_activated', title: 'Normal activated', message: 'Scene applied.', source: 'scenes', occurred_at: '2026-10-07T12:00:00Z', severity: null, reference_id: 'scene.normal', execution: record }} />)
  it('preserves legacy records without inventing details', () => {
    expect(render()).toContain('Detail unavailable')
    expect(render()).not.toContain('<details')
  })
  it('keeps successful executions collapsed', () => {
    const record = execution('success'); record.targets = []
    const html = render(record)
    expect(html).toContain('execution-success')
    expect(html).toContain('All targets applied')
    expect(html).not.toContain(' open=')
  })
  it('shows partial summary, source, scene, duration and all logical targets', () => {
    const html = render(execution())
    for (const text of ['Evening Home Lighting', 'Normal', '4 devices unavailable', '0.4 s', 'ha_state', 'room_main', 'desk_ambient', 'skipped', 'applied']) expect(html).toContain(text)
    expect(html).toContain('execution-partial')
  })
  it.each(['failed','rejected','interrupted'] as const)('makes %s explicit', result => {
    expect(render(execution(result))).toContain(`execution-${result}`)
    expect(render(execution(result))).toContain(result)
  })
  it('provides native accessible disclosure and target list', () => {
    const html = render(execution())
    expect(html).toContain('<summary>')
    expect(html).toContain('aria-label="Target execution details"')
    expect(html).toContain('<time dateTime=')
  })
  it('shows failed target reason and stage without raw errors', () => {
    const record=execution('failed'); record.targets=[{device_id:'room_main',actions:['power','brightness'],result:'failed',reason:'ha_error',stage:'apply',duration_ms:200}]
    const html=render(record)
    expect(html).toContain('Home Assistant error')
    expect(html).toContain('apply')
    expect(html).toContain('0.2 s')
  })
})

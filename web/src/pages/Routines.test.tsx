import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import type { ReactElement } from 'react'
import { describe, expect, it } from 'vitest'
import { RoutineCard, RoutineEmptyState, RoutineForm, RoutinesView, SchedulerStrip, type CardActions } from './Routines'
import { validateRoutineDraft } from '../routines-ui'
import { schedulerView, type ExecutionIndex } from '../routines-state'
import { READ_SCOPES, allOff, catalogOf, evening, manualAllOff, names, report, routinesFixture, scheduled, userRoutine } from '../routines-fixtures.test-support'
import type { RoutineDraft } from '../types'

const html = (node: ReactElement) => renderToStaticMarkup(<MemoryRouter>{node}</MemoryRouter>)
const index: ExecutionIndex = new Map([...scheduled, ...manualAllOff].map((record) => [record.id, record]))
const NOW = Date.parse('2026-10-10T13:20:00Z')
const catalog = catalogOf([evening, allOff])
const card = (routine = allOff, extra: { actions?: CardActions; executionsStatus?: 'ready' | 'forbidden' } = {}) => html(<RoutineCard routine={routine} catalog={catalog} executions={extra.executionsStatus === 'forbidden' ? new Map() : index} deviceNames={names} executionsStatus={extra.executionsStatus ?? 'ready'} now={NOW} actions={extra.actions} />)
const guard = (visible: boolean, ok: boolean, reason: string | null = null) => ({ visible, ok, reason })
const noop = () => undefined
const actions = (visible: boolean, ok: boolean, reason: string | null = null): CardActions => ({ run: guard(visible, ok, reason), write: guard(visible, ok, reason), busy: false, onRun: noop, onEdit: noop, onToggle: noop, onDelete: noop })

describe('managed routine cards', () => {
  it('are informational: Managed by RoomOS, no buttons, expandable details and Activity link', () => {
    for (const routine of [evening, allOff]) {
      const markup = card(routine, { actions: actions(false, false) })
      expect(markup).toContain('Managed by RoomOS')
      expect(markup).not.toContain('<button')
      expect(markup).not.toMatch(/>(Run|Run now|Edit|Disable|Delete|Turn off)</)
      expect(markup).toContain('<details class="auto-details">')
      expect(markup).toContain('href="/activity"')
      expect(markup).toContain('Active')
      expect(markup).not.toMatch(/rt_6b2|person\.|sun\.sun|light\./)
    }
  })
  it('describes the evening routine conditionally and without inventing a next time or reason', () => {
    const markup = card(evening)
    expect(markup).toContain('At sunset, if you&#x27;re home')
    expect(markup).toContain('After arriving home, until 02:00')
    expect(markup).toContain('Depends on sunset and arrival · no fixed time')
    expect(markup).toContain('Partly applied')
    expect(markup).toContain('Target details were not recorded for this run.')
    expect(markup).toContain('No execution recorded since 6 Oct, 18:38')
    expect(markup).toContain('No runs of this automation in recent history.')
    expect(markup).not.toMatch(/because you were away|presence unavailable|duplicate suppressed/i)
  })
  it('shows the 02:00 schedule, last scheduled run details and only routine executions', () => {
    const markup = card(allOff)
    expect(markup).toContain('Every day at 02:00')
    expect(markup).toContain('Scheduled Tomorrow 02:00')
    expect(markup).toContain('Scheduled time, not a confirmed run')
    expect(markup).toContain('Succeeded')
    expect(markup).toContain('5 of 5 lights applied · 295 ms')
    expect(markup).toContain('ex_7586b28e')
    expect(markup).toContain('Scheduled time')
    expect(markup).toContain('4 of 5 lights applied · 1 failed')
    expect(markup).toContain('Desk ambient')
    expect(markup).not.toContain('8 Oct, 23:47') // manual All Off never appears as a routine run
    expect(markup).not.toContain('10 Oct, 00:25')
  })
  it('hides run details when the device lacks activity access', () => {
    const markup = card(allOff, { executionsStatus: 'forbidden' })
    expect(markup).toContain('Run details are not shared with this device.')
    expect(markup).not.toContain('Recent runs')
  })
})

describe('user routine cards', () => {
  it('show supported actions only when permitted, disabled with a visible reason when blocked', () => {
    const ok = card(userRoutine, { actions: actions(true, true) })
    expect(ok).toContain('Your routine'); expect(ok).toContain('Run now'); expect(ok).toContain('Turn off'); expect(ok).toContain('Delete')
    expect(ok).toContain('No recorded execution')
    const blocked = card(userRoutine, { actions: actions(true, false, 'Controls are turned off on the server.') })
    expect((blocked.match(/<button[^>]*disabled/g) ?? []).length).toBe(4)
    expect(blocked).toContain('Controls are turned off on the server.')
    expect(card(userRoutine, { actions: actions(false, false) })).not.toContain('<button')
  })
})

describe('scheduler strip', () => {
  it('shows authoritative scheduler state and the next scheduled time', () => {
    const next = { name: '02:00 All Off', text: 'Scheduled Tomorrow 02:00', at: new Date('2026-10-10T23:00:00Z') }
    const healthy = html(<SchedulerStrip view={schedulerView(report().components[1], 'ready', false)} timezone="Europe/Istanbul" active={2} total={2} next={next} stale={false} />)
    expect(healthy).toContain('Running'); expect(healthy).toContain('Scheduler running · 2 of 2 enabled'); expect(healthy).toContain('Tomorrow 02:00'); expect(healthy).toContain('2 of 2')
    const unknown = html(<SchedulerStrip view={schedulerView(null, 'forbidden', false)} timezone="Europe/Istanbul" active={2} total={2} next={null} stale={false} />)
    expect(unknown).toContain('Unknown'); expect(unknown).not.toContain('Running')
    const stopped = html(<SchedulerStrip view={schedulerView(report('unhealthy', 'Scheduler is not running').components[1], 'ready', false)} timezone="Europe/Istanbul" active={2} total={2} next={null} stale />)
    expect(stopped).toContain('Not running'); expect(stopped).toContain('(last known)')
  })
})

describe('page states', () => {
  it('renders both managed routines with healthy scheduler and no page-level create for read-only devices', async () => {
    const { store } = routinesFixture({ scopes: READ_SCOPES }); await store.load()
    const markup = html(<RoutinesView store={store} snapshot={store.getSnapshot()} now={NOW} />)
    expect(markup).toContain('Evening Home Lighting'); expect(markup).toContain('02:00 All Off')
    expect(markup).not.toContain('New routine'); expect(markup).not.toContain('New automation')
    expect(markup).not.toContain('Run now'); expect(markup).not.toContain('Edit rule')
  })
  it('warns when the scheduler is not running or Home Assistant is disconnected', async () => {
    const { api, store } = routinesFixture()
    api.systemHealth.mockResolvedValueOnce(report('unhealthy', 'Scheduler is not running'))
    await store.load()
    store.handleEvent({ type: 'roomos.connection', data: { home_assistant: 'disconnected' }, device_id: null, timestamp: '' })
    const markup = html(<RoutinesView store={store} snapshot={store.getSnapshot()} now={NOW} />)
    expect(markup).toContain('Automations are not running')
    expect(markup).toContain('Home Assistant is disconnected')
  })
  it('marks stale data while reconnecting and shows a locked panel without access', async () => {
    const { store } = routinesFixture(); await store.load(); store.setConnection('reconnecting')
    expect(html(<RoutinesView store={store} snapshot={store.getSnapshot()} now={NOW} />)).toContain('Reconnecting · showing last known data')
    const locked = routinesFixture({ scopes: ['lights:read'] }); await locked.store.load()
    expect(html(<RoutinesView store={locked.store} snapshot={locked.store.getSnapshot()} now={NOW} />)).toContain('Automations aren&#x27;t shared with this device')
  })
  it('shows storage repair at the top', async () => {
    const { store } = routinesFixture({ catalog: { storage_status: 'corrupt' } }); await store.load()
    const markup = html(<RoutinesView store={store} snapshot={store.getSnapshot()} now={NOW} />)
    expect(markup.indexOf('Automation storage needs repair')).toBeLessThan(markup.indexOf('Evening Home Lighting'))
  })
})

describe('existing user-routine editor', () => {
  const draft: RoutineDraft = { name: 'Weeknight', enabled: true, trigger: { type: 'time', time: '22:30', days: ['mon'] }, action: { type: 'activate_scene', scene_id: 'night' } }
  it('keeps the bounded Manual/Time form with HA trigger unavailable', () => {
    const create = renderToStaticMarkup(<RoutineForm catalog={catalog} onSave={async () => undefined} onCancel={noop} />)
    expect(create).toContain('New routine'); expect(create).toContain('HA device state · unavailable')
    expect(renderToStaticMarkup(<RoutineForm catalog={catalog} initial={userRoutine} onSave={async () => undefined} onCancel={noop} />)).toContain('Edit routine')
    expect(renderToStaticMarkup(<RoutineEmptyState canWrite onCreate={noop} />)).toContain('Create your first routine')
  })
  it('validates drafts', () => {
    expect(validateRoutineDraft(draft)).toBeNull()
    expect(validateRoutineDraft({ ...draft, name: ' ' })).toContain('name')
    expect(validateRoutineDraft({ ...draft, trigger: { type: 'time', time: '25:00', days: ['mon'] } })).toContain('valid time')
    expect(validateRoutineDraft({ ...draft, trigger: { type: 'time', time: '22:00', days: [] } })).toContain('weekday')
  })
})

it('renders only proved decisions and keeps GPS/reporting uncertainty separate', async () => {
  const { diagnostics } = await import('../routines-fixtures.test-support')
  const c = { ...catalog, diagnostics }
  const markup = html(<RoutineCard routine={evening} catalog={c} executions={index} deviceNames={names} executionsStatus="ready" now={NOW} />)
  expect(markup).toContain('Skipped: presence was away')
  expect(markup).toContain('Last evaluated at sunset')
  expect(markup).toContain('Presence freshness unknown')
  expect(markup).toContain('No tracker update observed for 6 hours')
  expect(markup).not.toContain('<button')
  expect(markup).not.toContain('device_tracker.')
  const disconnected = html(<RoutineCard routine={evening} catalog={c} executions={index} deviceNames={names} executionsStatus="ready" now={NOW} diagnosticsLive={false} />)
  expect(disconnected).toContain('Tracker reporting recency unknown')
  const incomplete = { ...c, diagnostics: { ...diagnostics, evaluations: [{ ...diagnostics.evaluations[0], completion_state: 'incomplete' as const, outcome: null, reason_code: null }] } }
  const missing = html(<RoutineCard routine={evening} catalog={incomplete} executions={index} deviceNames={names} executionsStatus="ready" now={NOW} />)
  expect(missing).toContain('Outcome not recorded'); expect(missing).not.toContain('Skipped:')
})

it('shows reporting recency captured at evaluation, separate from current GPS uncertainty', async () => {
  const { diagnostics } = await import('../routines-fixtures.test-support')
  const record = { ...diagnostics.evaluations[0], trigger_origin: 'arrival' as const, outcome: 'executed' as const, reason_code: null, execution_id: 'ex_' + 'b'.repeat(32), execution_result: 'success' as const, presence_reporting: 'fresh' as const }
  const markup = html(<RoutineCard routine={evening} catalog={{ ...catalog, diagnostics: { ...diagnostics, evaluations: [record] } }} executions={index} deviceNames={names} executionsStatus="ready" now={NOW} />)
  expect(markup).toContain('Tracker reporting was recent at evaluation')
  expect(markup).toContain('Presence freshness unknown')
  expect(markup).toContain('GPS observation time is not available')
  const legacy = html(<RoutineCard routine={evening} catalog={{ ...catalog, diagnostics }} executions={index} deviceNames={names} executionsStatus="ready" now={NOW} />)
  expect(legacy).not.toContain('at evaluation')
})

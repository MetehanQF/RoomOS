import { describe, expect, it } from 'vitest'
import { ALL_SCOPES, READ_SCOPES, allOff, catalogOf, control, evening, manualAllOff, report, scheduled, userRoutine } from './routines-fixtures.test-support'
import {
  actionGuard, countsText, formatDateTime, isNewerRoutine, lastRun, nextLine, nextScheduled, quietNote, relativeSchedule, resultLabel,
  routineExecutions, schedulerView, triggerLines, triggerRules, zonedInstant, type ExecutionIndex,
} from './routines-state'
import type { ConnectionStatus, Health, Routine } from './types'

const index = (records = [...scheduled, ...manualAllOff]): ExecutionIndex => new Map(records.map((record) => [record.id, record]))
const at = (iso: string) => Date.parse(iso)
const TZ = 'Europe/Istanbul'

describe('trigger wording', () => {
  it('describes the evening routine conditionally without promising one run per evening', () => {
    expect(triggerLines(evening.trigger)).toEqual(["At sunset, if you're home", 'After arriving home, until 02:00'])
    const rules = triggerRules(evening.trigger, TZ).join(' ')
    expect(rules).toContain('Each arrival before 02:00 can run it again')
    expect(rules).toContain('not made up later')
    expect(rules).not.toMatch(/once per evening(?! limit)/)
  })
  it('describes daily, weekday and custom time triggers', () => {
    expect(triggerLines(allOff.trigger)).toEqual(['Every day at 02:00'])
    expect(triggerLines(userRoutine.trigger)).toEqual(['Weekdays at 22:30'])
    expect(triggerLines({ type: 'time', time: '09:00', days: ['sat', 'sun'] })).toEqual(['Weekends at 09:00'])
    expect(triggerLines({ type: 'time', time: '07:15', days: ['wed', 'mon'] })).toEqual(['Mon, Wed at 07:15'])
    expect(triggerRules(allOff.trigger, TZ)[0]).toBe('Every day at 02:00 (Istanbul time).')
  })
  it('never exposes raw HA or trigger identifiers', () => {
    const text = triggerLines({ type: 'ha_state', trigger_id: 'person_metehan', state: 'home' }).join(' ') + triggerRules({ type: 'ha_state', trigger_id: 'person_metehan', state: 'home' }, TZ).join(' ')
    expect(text).not.toContain('person_metehan')
    expect(text).not.toMatch(/person\.|sun\.sun|light\./)
  })
})

describe('next scheduled 02:00 in Europe/Istanbul', () => {
  it('is tonight 02:00 when viewed in the afternoon (UTC+3, not device time)', () => {
    const next = nextScheduled(allOff, TZ, at('2026-10-10T13:20:00Z'))!
    expect(next.toISOString()).toBe('2026-10-10T23:00:00.000Z')
    expect(relativeSchedule(next, TZ, at('2026-10-10T13:20:00Z'))).toBe('Tomorrow 02:00')
  })
  it('handles the minutes around 02:00 local and the local date boundary', () => {
    expect(nextScheduled(allOff, TZ, at('2026-10-10T22:59:59Z'))!.toISOString()).toBe('2026-10-10T23:00:00.000Z')
    expect(nextScheduled(allOff, TZ, at('2026-10-10T23:00:00Z'))!.toISOString()).toBe('2026-10-11T23:00:00.000Z')
    expect(nextScheduled(allOff, TZ, at('2026-10-10T23:00:30Z'))!.toISOString()).toBe('2026-10-11T23:00:00.000Z')
    // 21:30Z is already 00:30 local on the 11th: 02:00 is "today" locally.
    const late = at('2026-10-10T21:30:00Z')
    expect(relativeSchedule(nextScheduled(allOff, TZ, late)!, TZ, late)).toBe('Today 02:00')
  })
  it('respects weekdays in local time and returns null when not applicable', () => {
    // Friday 2026-10-09 23:00 local → Weeknight next is Monday 22:30 local.
    expect(nextScheduled(userRoutine, TZ, at('2026-10-09T20:00:00Z'))!.toISOString()).toBe('2026-10-12T19:30:00.000Z')
    expect(nextScheduled(evening, TZ, Date.now())).toBeNull()
    expect(nextScheduled({ ...allOff, enabled: false }, TZ, Date.now())).toBeNull()
  })
  it('converts wall time correctly across a DST change in another zone', () => {
    expect(new Date(zonedInstant(2026, 3, 8, 3, 30, 'America/New_York')).toISOString()).toBe('2026-03-08T07:30:00.000Z')
    expect(new Date(zonedInstant(2026, 3, 7, 3, 30, 'America/New_York')).toISOString()).toBe('2026-03-07T08:30:00.000Z')
  })
  it('labels the next line as scheduled, conditional, manual or off', () => {
    expect(nextLine(allOff, TZ, at('2026-10-10T13:20:00Z'))).toMatchObject({ kind: 'scheduled', text: 'Scheduled Tomorrow 02:00' })
    expect(nextLine(evening, TZ, Date.now())).toEqual({ kind: 'conditional', text: 'Depends on sunset and arrival · no fixed time' })
    expect(nextLine({ ...userRoutine, trigger: { type: 'manual' } }, TZ, Date.now()).kind).toBe('manual')
    expect(nextLine({ ...allOff, enabled: false }, TZ, Date.now()).text).toBe("Won't run while off")
  })
  it('formats 24-hour Istanbul time', () => {
    expect(formatDateTime('2026-10-09T23:00:09Z', TZ)).toBe('10 Oct, 02:00')
  })
})

describe('execution correlation', () => {
  it('uses last_result.execution_id and never manual All Off of the same scene', () => {
    const last = lastRun(allOff, index(), 'ready')
    expect(last.execution?.id).toBe('ex_7586b28e504c484980d93570a3b67f02')
    const history = routineExecutions(allOff, index())
    expect(history.map((record) => record.id)).toEqual(['ex_7586b28e504c484980d93570a3b67f02', 'ex_04a70c1ee4fc4207bc93d6cb407fe74a', 'ex_dd06b4d9920047f9aa4c6c041091f87e'])
    expect(history.every((record) => record.source === 'routine' && record.routine_id === allOff.id)).toBe(true)
  })
  it('does not adopt a manual execution even when the routine points at a missing id', () => {
    const pointing: Routine = { ...allOff, last_result: { ...allOff.last_result!, execution_id: 'ex_0250fb71f4214a11beebe5a002fc7478' } }
    const last = lastRun(pointing, index(), 'ready')
    expect(last.execution).toBeNull()
    expect(last.detailNote).toBe('Run details are no longer retained.')
  })
  it('shows the routine record without targets when no execution was recorded', () => {
    const last = lastRun(evening, index(), 'ready')
    expect(last).toMatchObject({ tone: 'partial', label: 'Partly applied', at: evening.last_run, execution: null, detailNote: 'Target details were not recorded for this run.' })
    expect(routineExecutions(evening, index())).toEqual([])
  })
  it('reports "No recorded execution" when nothing ran and never fabricates a time', () => {
    expect(lastRun(userRoutine, index(), 'ready')).toMatchObject({ tone: 'none', label: 'No recorded execution', at: null })
  })
  it('explains details unavailable by permission or retention', () => {
    expect(lastRun(allOff, new Map(), 'forbidden').detailNote).toBe('Run details are not shared with this device.')
    expect(lastRun(allOff, new Map(), 'ready').detailNote).toBe('Run details are no longer retained.')
  })
  it('distinguishes partial, failed and refused runs', () => {
    expect(countsText(scheduled[1])).toBe('4 of 5 lights applied · 1 failed')
    expect(resultLabel('rejected', 'control_disabled')).toEqual({ tone: 'rejected', label: 'Not run · control was turned off' })
    expect(resultLabel('failed', null)).toEqual({ tone: 'failed', label: 'Failed' })
    const refused: Routine = { ...allOff, last_result: { execution_id: null, status: 'failed', scene_status: null, source: 'schedule', detail: 'control_disabled' } }
    expect(lastRun(refused, new Map(), 'ready').tone).toBe('rejected')
  })
  it('uses neutral wording for an evening without runs and never invents a reason', () => {
    const note = quietNote(evening, TZ)!
    expect(note).toBe('No execution recorded since 6 Oct, 18:38. RoomOS does not record evaluations that end without a run, so a reason is not available.')
    expect(note).not.toMatch(/away|presence|duplicate|missed|skipped/i)
    expect(quietNote(allOff, TZ)).toBeNull()
  })
})

describe('scheduler status', () => {
  it('maps the authoritative automations component, not routine counts', () => {
    const component = (status: 'healthy' | 'degraded' | 'unhealthy' | 'unknown') => report(status, 'x').components[1]
    expect(schedulerView(component('healthy'), 'ready', false).state).toBe('healthy')
    expect(schedulerView(component('degraded'), 'ready', false).state).toBe('degraded')
    expect(schedulerView(component('unhealthy'), 'ready', false)).toMatchObject({ state: 'unavailable', label: 'Not running' })
    expect(schedulerView(component('unknown'), 'ready', false).state).toBe('unknown')
    expect(schedulerView(null, 'forbidden', false)).toMatchObject({ state: 'unknown', summary: 'Scheduler status is not shared with this device.' })
    expect(schedulerView(null, 'error', false).state).toBe('unknown')
    expect(schedulerView(null, 'ready', false).state).toBe('unknown')
    expect(schedulerView(component('healthy'), 'ready', true).state).toBe('unavailable')
  })
})

describe('action guards', () => {
  const base: { catalog: ReturnType<typeof catalogOf>; scopes: string[]; authResolved: boolean; control: Health | null; connection: ConnectionStatus; trusted: boolean; pending: boolean } = { catalog: catalogOf([userRoutine, allOff]), scopes: ALL_SCOPES, authResolved: true, control, connection: 'connected', trusted: true, pending: false }
  it('hides every control for managed routines', () => {
    expect(actionGuard({ ...base, routine: allOff, kind: 'run' }).visible).toBe(false)
    expect(actionGuard({ ...base, routine: evening, kind: 'write' }).visible).toBe(false)
  })
  it('hides controls on read-only devices and when auth is unresolved', () => {
    expect(actionGuard({ ...base, routine: userRoutine, kind: 'write', scopes: READ_SCOPES, catalog: catalogOf([userRoutine], { can_write: false, can_run: false }) }).visible).toBe(false)
    expect(actionGuard({ ...base, routine: userRoutine, kind: 'run', scopes: ALL_SCOPES.filter((s) => s !== 'lights:write') }).visible).toBe(false)
    expect(actionGuard({ ...base, routine: userRoutine, kind: 'run', authResolved: false }).visible).toBe(false)
  })
  it('fails closed on kill switch, connection, refresh, HA and pending state', () => {
    const run = (change: Partial<typeof base>) => actionGuard({ ...base, ...change, routine: userRoutine, kind: 'run' })
    expect(run({}).ok).toBe(true)
    expect(run({ control: { ...control, control_enabled: false } })).toMatchObject({ visible: true, ok: false, reason: 'Controls are turned off on the server.' })
    expect(run({ control: null }).ok).toBe(false)
    expect(run({ connection: 'offline' }).ok).toBe(false)
    expect(run({ connection: 'reconnecting' }).ok).toBe(false)
    expect(run({ trusted: false }).ok).toBe(false)
    expect(run({ pending: true }).ok).toBe(false)
    expect(run({ control: { ...control, home_assistant: 'disconnected' } }).ok).toBe(false)
    expect(run({ catalog: catalogOf([userRoutine], { storage_status: 'corrupt' }) }).ok).toBe(false)
  })
})

describe('event ordering', () => {
  it('accepts newer-or-equal routine state and rejects older', () => {
    expect(isNewerRoutine({ ...allOff, updated_at: '2026-10-10T23:00:09.000001Z' }, allOff)).toBe(true)
    expect(isNewerRoutine({ ...allOff, updated_at: '2026-10-09T23:00:09.166332Z' }, allOff)).toBe(false)
    expect(isNewerRoutine({ ...allOff, updated_at: 'garbage' }, allOff)).toBe(false)
  })
})

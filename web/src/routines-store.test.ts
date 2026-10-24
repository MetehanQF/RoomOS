import { describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { ALL_OFF_ID, ALL_SCOPES, READ_SCOPES, activityOf, allOff, evening, exec, manualAllOff, routinesFixture, scheduled, userRoutine, writeCalls } from './routines-fixtures.test-support'
import type { Routine, RoomOSEvent } from './types'

const event = (type: RoomOSEvent['type'], data: RoomOSEvent['data']): RoomOSEvent => ({ type, data, device_id: null, timestamp: '2026-10-10T23:00:10Z' })
const ran: Routine = { ...allOff, updated_at: '2026-10-10T23:00:08.500000Z', last_run: '2026-10-10T23:00:08.4Z', last_result: { execution_id: 'ex_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', status: 'success', scene_status: 'success', source: 'schedule', detail: null } }
const newExec = exec('ex_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '2026-10-10T23:00:08.1Z')
const flush = () => new Promise((resolve) => setTimeout(resolve, 0))

describe('Routines store bootstrap', () => {
  it('reads routines, executions, scheduler health and control once, single-flight', async () => {
    const { api, store } = routinesFixture()
    const a = store.load(), b = store.load()
    expect(a).toBe(b); await a
    expect(api.routines).toHaveBeenCalledTimes(1); expect(api.executions).toHaveBeenCalledTimes(1)
    expect(api.systemHealth).toHaveBeenCalledTimes(1); expect(api.health).toHaveBeenCalledTimes(1)
    const s = store.getSnapshot()
    expect(s.catalogStatus).toBe('ready'); expect(s.trusted).toBe(true)
    expect(s.scheduler?.status).toBe('healthy'); expect(s.executions.size).toBe(5)
    expect(writeCalls(api)).toBe(0)
  })
  it('only reads what a read-only device is allowed to read', async () => {
    const { api, store } = routinesFixture({ scopes: READ_SCOPES })
    await store.load()
    expect(api.health).not.toHaveBeenCalled()
    expect(store.guard(userRoutine, 'run').visible).toBe(false)
    expect(store.guard(null, 'write').visible).toBe(false)
  })
  it('marks optional data forbidden without the scope and does not fetch it', async () => {
    const { api, store } = routinesFixture({ scopes: ['routines:read'] })
    await store.load()
    expect(api.executions).not.toHaveBeenCalled(); expect(api.systemHealth).not.toHaveBeenCalled()
    expect(store.getSnapshot()).toMatchObject({ executionsStatus: 'forbidden', schedulerStatus: 'forbidden', homeAssistant: 'unknown' })
  })
  it('reports a locked page without routines:read and makes no request', async () => {
    const { api, store } = routinesFixture({ scopes: ['lights:read'] })
    await store.load()
    expect(api.routines).not.toHaveBeenCalled(); expect(store.getSnapshot().catalogStatus).toBe('forbidden')
  })
  it('keeps scheduler unknown when health cannot be read', async () => {
    const { api, store } = routinesFixture()
    api.systemHealth.mockRejectedValueOnce(new ApiError(500, 'x'))
    await store.load()
    expect(store.getSnapshot().schedulerStatus).toBe('error')
  })
})

describe('Routines store events', () => {
  it('ignores unrelated system, light and camera events with zero REST', async () => {
    const { api, store } = routinesFixture(); await store.load()
    for (const type of ['system.metric_updated', 'system.snapshot_updated', 'system.service_updated', 'light.state_changed', 'camera.updated', 'scene.active_changed', 'alert.started'] as const) store.handleEvent(event(type, {}))
    await flush()
    expect(api.routines).toHaveBeenCalledTimes(1); expect(api.executions).toHaveBeenCalledTimes(1); expect(api.systemHealth).toHaveBeenCalledTimes(1)
  })
  it('applies routine.run_completed locally and fetches the new execution once, coalesced with activity.recorded', async () => {
    const { api, store, state } = routinesFixture(); await store.load()
    state.activity = activityOf([...scheduled, ...manualAllOff, newExec])
    store.handleEvent(event('activity.recorded', { activity: { type: 'scene_activated', source: 'routine', execution_id: newExec.id } }))
    store.handleEvent(event('routine.run_completed', { routine_id: ALL_OFF_ID, routine: ran }))
    await flush(); await flush()
    expect(api.routines).toHaveBeenCalledTimes(1)
    expect(api.executions).toHaveBeenCalledTimes(2) // bootstrap + one targeted read
    const s = store.getSnapshot()
    expect(s.catalog!.routines.find((r) => r.id === ALL_OFF_ID)!.last_run).toBe(ran.last_run)
    expect(s.executions.get(newExec.id)?.result).toBe('success')
  })
  it('does not loop when an execution stays unavailable', async () => {
    const { api, store } = routinesFixture(); await store.load()
    store.handleEvent(event('routine.run_completed', { routine_id: ALL_OFF_ID, routine: ran }))
    await flush(); await flush()
    expect(api.executions).toHaveBeenCalledTimes(2)
  })
  it('does not fetch for manual (non-routine) activity or already-known executions', async () => {
    const { api, store } = routinesFixture(); await store.load()
    store.handleEvent(event('activity.recorded', { activity: { type: 'scene_activated', source: 'scenes', execution_id: 'ex_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' } }))
    store.handleEvent(event('activity.recorded', { activity: { type: 'scene_activated', source: 'routine', execution_id: scheduled[2].id } }))
    await flush()
    expect(api.executions).toHaveBeenCalledTimes(1)
  })
  it('never overwrites newer routine state with a delayed older event', async () => {
    const { store } = routinesFixture(); await store.load()
    store.handleEvent(event('routine.run_completed', { routine_id: ALL_OFF_ID, routine: ran }))
    store.handleEvent(event('routine.changed', { routine_id: ALL_OFF_ID, routine: allOff }))
    expect(store.getSnapshot().catalog!.routines.find((r) => r.id === ALL_OFF_ID)!.updated_at).toBe(ran.updated_at)
  })
  it('handles routine.deleted and new user routines', async () => {
    const { store } = routinesFixture({ routines: [evening, allOff, userRoutine] }); await store.load()
    store.handleEvent(event('routine.deleted', { routine_id: userRoutine.id }))
    expect(store.getSnapshot().catalog!.routines.map((r) => r.id)).toEqual([evening.id, allOff.id])
    store.handleEvent(event('routine.changed', { routine_id: userRoutine.id, routine: userRoutine }))
    expect(store.getSnapshot().catalog!.routines).toHaveLength(3)
  })
  it('queues events received during a refresh and replays them over the older snapshot', async () => {
    const { store } = routinesFixture()
    const load = store.load()
    store.handleEvent(event('routine.run_completed', { routine_id: ALL_OFF_ID, routine: ran }))
    await load
    expect(store.getSnapshot().catalog!.routines.find((r) => r.id === ALL_OFF_ID)!.updated_at).toBe(ran.updated_at)
  })
  it('tracks Home Assistant connection from roomos.connection events', async () => {
    const { store } = routinesFixture(); await store.load()
    store.handleEvent(event('roomos.connection', { home_assistant: 'disconnected' }))
    expect(store.getSnapshot().homeAssistant).toBe('disconnected')
    expect(store.guard(userRoutine, 'run').ok).toBe(false)
  })
})

describe('Routines store reconnect and stale data', () => {
  it('distrusts data on disconnect and reloads once on reconnect', async () => {
    const { api, store } = routinesFixture({ routines: [evening, allOff, userRoutine] }); await store.load()
    store.setConnection('reconnecting')
    expect(store.getSnapshot().trusted).toBe(false)
    expect(store.guard(userRoutine, 'write')).toMatchObject({ visible: true, ok: false })
    store.setConnection('connected'); await store.load()
    expect(api.routines).toHaveBeenCalledTimes(2); expect(store.getSnapshot().trusted).toBe(true)
  })
  it('discards a load that started before a disconnect', async () => {
    const { api, store } = routinesFixture()
    let release!: (value: unknown) => void
    api.routines.mockImplementationOnce(() => new Promise((resolve) => { release = resolve }) as never)
    const load = store.load()
    store.setConnection('offline')
    release({ routines: [], timezone: 'Europe/Istanbul', storage_status: 'ok', can_write: true, can_run: true, ha_trigger_ids: [], scenes: [] })
    await load
    expect(store.getSnapshot().catalog).toBeNull(); expect(store.getSnapshot().trusted).toBe(false)
  })
})

describe('Routines store writes (fixtures only)', () => {
  it('never sends a write for managed routines or from read-only devices', async () => {
    const { api, store } = routinesFixture(); await store.load()
    expect(await store.run(allOff)).toBe(false); expect(await store.toggle(evening)).toBe(false); expect(await store.remove(allOff)).toBe(false)
    const ro = routinesFixture({ scopes: READ_SCOPES, routines: [userRoutine], catalog: { can_write: false, can_run: false } }); await ro.store.load()
    expect(await ro.store.run(userRoutine)).toBe(false); expect(await ro.store.toggle(userRoutine)).toBe(false)
    expect(writeCalls(api) + writeCalls(ro.api)).toBe(0)
  })
  it('blocks writes when control is disabled or disconnected', async () => {
    const { api, store } = routinesFixture({ routines: [userRoutine] })
    api.health.mockResolvedValueOnce({ ...(await api.health()), control_enabled: false })
    await store.load()
    expect(await store.run(userRoutine)).toBe(false)
    const other = routinesFixture({ routines: [userRoutine] }); await other.store.load(); other.store.setConnection('offline')
    expect(await other.store.toggle(userRoutine)).toBe(false)
    expect(writeCalls(api) + writeCalls(other.api)).toBe(0)
  })
  it('runs a user routine once, blocks concurrent actions and reports the result', async () => {
    const { api, store } = routinesFixture({ routines: [userRoutine], scopes: ALL_SCOPES }); await store.load()
    let finish!: (value: unknown) => void
    api.run.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const first = store.run(userRoutine)
    expect(await store.toggle(userRoutine)).toBe(false)
    finish({ routine: { ...userRoutine, updated_at: '2026-10-10T10:00:00Z', last_run: '2026-10-10T10:00:00Z', last_result: { execution_id: null, status: 'partial', scene_status: 'partial', source: 'manual', detail: null } }, scene: { scene: 'night', name: 'Gece', status: 'partial', applied: [], skipped: [], failed: [], applied_at: '2026-10-10T10:00:00Z' } })
    await first
    expect(api.run).toHaveBeenCalledTimes(1)
    expect(store.getSnapshot().feedback?.value.tone).toBe('partial')
    expect(store.getSnapshot().pending).toBeNull()
  })
  it('reports API refusals without retrying', async () => {
    const { api, store } = routinesFixture({ routines: [userRoutine] }); await store.load()
    api.update.mockRejectedValueOnce(new ApiError(409, 'x'))
    await store.toggle(userRoutine)
    expect(api.update).toHaveBeenCalledTimes(1)
    expect(store.getSnapshot().feedback?.value).toMatchObject({ tone: 'error', detail: 'Managed by RoomOS or in conflict with current state.' })
  })
  it('removes a deleted routine and saves edits from the API response', async () => {
    const { api, store } = routinesFixture({ routines: [userRoutine] }); await store.load()
    api.update.mockResolvedValueOnce({ ...userRoutine, name: 'Late night', updated_at: '2026-10-10T11:00:00Z' })
    await store.save({ name: 'Late night', enabled: true, trigger: userRoutine.trigger, action: userRoutine.action }, userRoutine)
    expect(store.getSnapshot().catalog!.routines[0].name).toBe('Late night')
    api.remove.mockResolvedValueOnce(undefined)
    await store.remove(store.getSnapshot().catalog!.routines[0])
    expect(store.getSnapshot().catalog!.routines).toHaveLength(0)
  })
})
vi.useRealTimers()

it('applies scoped diagnostics without REST and reloads authoritatively on reconnect', async () => {
  const { diagnostics } = await import('./routines-fixtures.test-support')
  const { api, store } = routinesFixture(); await store.load()
  store.handleEvent(event('routine.diagnostics', { diagnostics }))
  expect(store.getSnapshot().catalog?.diagnostics).toEqual(diagnostics)
  expect(api.routines).toHaveBeenCalledTimes(1)
  expect(api.executions).toHaveBeenCalledTimes(1)
  store.setConnection('offline'); expect(store.getSnapshot().trusted).toBe(false)
  store.setConnection('connected'); await store.load()
  expect(api.routines).toHaveBeenCalledTimes(2)
})

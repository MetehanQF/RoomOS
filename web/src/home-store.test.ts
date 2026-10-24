import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { fixture, ids, lights, alert, health, scopes, result } from './home-fixtures.test-support'
import { roomState, healthSummary, sceneFeedback } from './home-state'
import type { RoomOSEvent } from './types'

const event = (type: RoomOSEvent['type'], data: RoomOSEvent['data'], device_id: string | null = null): RoomOSEvent => ({ type, data, device_id, timestamp: '2026-10-07T12:00:01Z' })
afterEach(() => vi.useRealTimers())

describe('event-driven Home', () => {
  it('bootstraps one request per resource, with concurrent calls single-flight and no /system loader', async () => {
    const { api, store } = fixture()
    const a = store.load(); const b = store.load()
    expect(a).toBe(b)
    await a
    for (const key of ['health', 'lights', 'scenes', 'systemHealth', 'alerts', 'routines', 'camera'] as const) expect(api[key]).toHaveBeenCalledTimes(1)
    expect(Object.keys(api)).not.toContain('system')
    expect(store.control().group).toBe(true)
  })
  it('updates ordinary light/scene/alert events locally without REST or announcements', async () => {
    const { api, store } = fixture(); await store.load()
    store.handleEvent(event('light.state_changed', { light: { ...lights[4], on: true } }, 'desk_ambient'))
    expect(store.getSnapshot().resources.lights.data?.[4].on).toBe(true)
    store.handleEvent(event('scene.active_changed', { active_scene: 'work_2' }))
    expect(store.getSnapshot().resources.scenes.data?.active_scene).toBe('work_2')
    store.handleEvent(event('alert.resolved', { alert: { ...alert, status: 'resolved' } }))
    expect(store.getSnapshot().resources.alerts.data?.active).toHaveLength(0)
    store.handleEvent(event('alert.started', { alert }))
    expect(store.getSnapshot().resources.alerts.data?.active[0].title).toBe('Secondary offline')
    expect(store.getSnapshot().announcement).toBeNull()
    for (const key of ['health', 'lights', 'scenes', 'systemHealth', 'alerts', 'routines', 'camera'] as const) expect(api[key]).toHaveBeenCalledTimes(1)
  })
  it('replays events received during a bootstrap after the older REST snapshot', async () => {
    const { store } = fixture(); const load = store.load()
    store.handleEvent(event('light.state_changed', { light: { ...lights[4], on: true } }, 'desk_ambient'))
    await load
    expect(store.getSnapshot().resources.lights.data?.[4].on).toBe(true)
  })
  it.each(['camera', 'systemHealth', 'alerts', 'routines'] as const)('isolates %s failure from room controls', async (key) => {
    const { store } = fixture({ [key]: vi.fn(async () => { throw new Error('fixture') }) })
    await store.load(); expect(store.control().group).toBe(true)
    expect(store.getSnapshot().resources[key].status).toBe('error')
  })
  it('disables writes until reconnect refresh completes and after a failed safety refresh', async () => {
    const { api, store } = fixture(); await store.load()
    store.setConnection('reconnecting'); expect(store.control().group).toBe(false)
    store.setConnection('connected'); expect(store.control().group).toBe(false)
    const load = store.load(); expect(store.control().group).toBe(false)
    await load; expect(store.control().group).toBe(true)
    vi.mocked(api.health).mockRejectedValueOnce(new Error('offline'))
    await store.load(); expect(store.control().group).toBe(false)
    expect(await store.allOn()).toBe(false); expect(api.allPower).not.toHaveBeenCalled()
  })
  it('does not trust a bootstrap started before a disconnect', async () => {
    const { store } = fixture(); const load = store.load()
    store.setConnection('offline'); store.setConnection('connected')
    await load; expect(store.control().group).toBe(false)
    await store.load(); expect(store.control().group).toBe(true)
  })
  it('updates HA connecting to unsafe immediately without letting Secondary block controls', async () => {
    const { store } = fixture(); await store.load()
    expect(store.control().group).toBe(true)
    store.handleEvent(event('roomos.connection', { home_assistant: 'connecting' }))
    expect(store.control().group).toBe(false)
  })
  it.each(ids)('activates explicit %s only through scene abstraction, without optimistic active state', async (id) => {
    const { api, store } = fixture(); await store.load()
    await store.activateScene(id)
    expect(api.activateScene).toHaveBeenCalledExactlyOnceWith(id)
    expect(api.allPower).not.toHaveBeenCalled()
    expect(store.getSnapshot().resources.scenes.data?.active_scene).toBeNull()
  })
  it('All On uses only group power true', async () => {
    const { api, store } = fixture(); await store.load(); await store.allOn()
    expect(api.allPower).toHaveBeenCalledExactlyOnceWith(true)
    expect(api.activateScene).not.toHaveBeenCalled()
  })
  it.each(['read-only', 'missing-write', 'unresolved', 'disabled', 'offline', 'untrusted'])('sends no writes for %s', async (state) => {
    const { api, store } = fixture(state === 'disabled' ? { health: async () => ({ ...health, control_enabled: false }) } : {}, state === 'read-only' || state === 'missing-write' ? scopes.filter((x) => x !== 'lights:write') : scopes, state !== 'unresolved')
    await store.load()
    if (state === 'offline' || state === 'untrusted') store.setConnection('offline')
    if (state === 'untrusted') store.setConnection('connected')
    for (const id of ids) expect(await store.activateScene(id)).toBe(false)
    expect(await store.allOn()).toBe(false)
    expect(api.activateScene).not.toHaveBeenCalled(); expect(api.allPower).not.toHaveBeenCalled()
  })
  it('guards duplicate commands, shows only selected pending, and bounds unknown results without retries', async () => {
    vi.useFakeTimers()
    const { api, store } = fixture({ activateScene: vi.fn(() => new Promise<import("./types").SceneActivationResult>(() => {})) }); await store.load()
    const run = store.activateScene('normal')
    expect(store.getSnapshot().pending).toEqual({ kind: 'scene', id: 'normal' })
    expect(await store.activateScene('movie')).toBe(false)
    await vi.advanceTimersByTimeAsync(15_000); await run
    expect(store.getSnapshot().feedback?.detail).toContain('Result unknown')
    expect(api.activateScene).toHaveBeenCalledTimes(1)
  })
  it.each(['success', 'partial', 'failed'] as const)('announces %s result once; details require activity access', async (status) => {
    const response = { ...result, status, skipped: [{ device: 'secret.id', reason: 'unavailable' }] }
    const { store } = fixture({ activateScene: async () => response }); await store.load(); await store.activateScene('normal')
    expect(store.getSnapshot().announcement?.id).toBe(1)
    expect(JSON.stringify(store.getSnapshot().feedback)).not.toContain('secret.id')
    expect(store.getSnapshot().feedback?.details).toBe(status !== 'success')
    expect(sceneFeedback(response, false).feedback.details).toBe(false)
    if (status === 'partial') expect(store.getSnapshot().feedback?.detail).toBe('1 unavailable')
  })
  it('sanitizes rejection errors even if the exception contains private transport details', async () => {
    const { store } = fixture({ activateScene: async () => { throw new ApiError(403, 'secret.host') } }); await store.load(); await store.activateScene('normal')
    expect(store.getSnapshot().feedback?.detail).toBe('Controls are currently locked.')
    expect(store.getSnapshot().announcement?.text).not.toContain('secret.host')
  })
  it('does not infer all_off active from an off room or count unavailable lights as on', async () => {
    const { store } = fixture(); await store.load()
    const catalog = store.getSnapshot().resources.scenes.data
    expect(roomState(lights.map((x) => ({ ...x, on: !x.available })), catalog).mode.kind).toBe('off')
    expect(roomState(lights.map((x) => ({ ...x, on: x.available })), catalog).mode.kind).toBe('custom')
    expect(roomState(lights, { ...catalog!, active_scene: 'all_off' }).mode.kind).toBe('scene')
    expect(healthSummary(store.getSnapshot().resources.systemHealth.data, false).label).toBe('RoomOS ok')
  })
})

import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { fixture, health, lights, scopes, writeCalls } from './lights-fixtures.test-support'
import { CONFIRM_TIMEOUT_MS, type LightsApi } from './lights-store'
import { colorMode, effectLabel, isNewer, kelvinValue, stateLine } from './lights-state'
import type { Light, RoomOSEvent } from './types'

const changed = (light: Light): RoomOSEvent => ({ type: 'light.state_changed', data: { light }, device_id: light.id, timestamp: '2026-10-08T10:00:05Z' })
const later = (light: Light, change: Partial<Light>, at = '2026-10-08T10:00:05Z'): Light => ({ ...light, ...change, last_updated: at })
const get = (store: ReturnType<typeof fixture>['store'], id: string) => store.getSnapshot().lights!.find((light) => light.id === id)!
afterEach(() => vi.useRealTimers())

describe('Lights store bootstrap and events', () => {
  it('bootstraps exactly /lights + /health once, single-flight, and trusts only when connected', async () => {
    const { api, store } = fixture()
    const a = store.load(); const b = store.load()
    expect(a).toBe(b); await a
    expect(api.lights).toHaveBeenCalledTimes(1); expect(api.health).toHaveBeenCalledTimes(1)
    expect(store.page().ok).toBe(true)
    expect(writeCalls(api)).toBe(0)
  })
  it('applies light.state_changed locally with zero REST refetch and no announcement', async () => {
    const { api, store } = fixture(); await store.load()
    for (let index = 0; index < 20; index += 1) store.handleEvent(changed(later(lights[0], { brightness: 10 + index }, `2026-10-08T10:01:${String(index).padStart(2, '0')}Z`)))
    expect(get(store, 'room_main').brightness).toBe(29)
    expect(api.lights).toHaveBeenCalledTimes(1); expect(api.health).toHaveBeenCalledTimes(1)
    expect(store.getSnapshot().announcement).toBeNull()
  })
  it('rejects stale observations and ignores unknown devices / unrelated events', async () => {
    const { api, store } = fixture(); await store.load()
    store.handleEvent(changed(later(lights[0], { brightness: 200 }, '2026-10-08T09:59:00Z')))
    expect(get(store, 'room_main').brightness).toBe(128)
    store.handleEvent(changed({ ...lights[0], id: 'light.all_lights', last_updated: '2026-10-08T11:00:00Z' }))
    expect(store.getSnapshot().lights!.map((light) => light.id)).not.toContain('light.all_lights')
    store.handleEvent({ type: 'system.metric_updated', data: {}, device_id: null, timestamp: '2026-10-08T10:00:00Z' } as unknown as RoomOSEvent)
    expect(api.lights).toHaveBeenCalledTimes(1)
  })
  it('replays events received during bootstrap over the older REST snapshot', async () => {
    const { store } = fixture(); const load = store.load()
    store.handleEvent(changed(later(lights[1], { on: true, brightness: 255 })))
    await load
    expect(get(store, 'room_2').on).toBe(true)
  })
  it('pauses writes on disconnect and only re-trusts after a refresh that started after reconnect', async () => {
    const { api, store } = fixture(); await store.load()
    store.setConnection('reconnecting'); expect(store.page().reason).toBe('Reconnecting · controls paused.')
    expect(await store.power(lights[0], false)).toBe(false)
    store.setConnection('connected'); expect(store.page().ok).toBe(false)
    await store.load(); expect(store.page().ok).toBe(true)
    expect(api.lights).toHaveBeenCalledTimes(2); expect(writeCalls(api)).toBe(0)
  })
  it('does not trust a bootstrap started before a disconnect', async () => {
    const { store } = fixture(); const load = store.load()
    store.setConnection('offline'); store.setConnection('connected')
    await load; expect(store.page().ok).toBe(false)
    await store.load(); expect(store.page().ok).toBe(true)
  })
  it('HA disconnect blocks writes immediately; HA reconnect triggers one authoritative refresh', async () => {
    const { api, store } = fixture(); await store.load()
    store.handleEvent({ type: 'roomos.connection', data: { home_assistant: 'disconnected' }, device_id: null, timestamp: 'x' } as RoomOSEvent)
    expect(store.page().reason).toBe('Home Assistant is unavailable.')
    store.handleEvent({ type: 'roomos.connection', data: { home_assistant: 'connected' }, device_id: null, timestamp: 'x' } as RoomOSEvent)
    await vi.waitFor(() => expect(api.lights).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(store.page().ok).toBe(true))
  })
  it('isolates failures: lights failing keeps writes closed; health failing keeps state visible but writes closed', async () => {
    const a = fixture({ lights: vi.fn(async () => { throw new Error('boom') }) }); await a.store.load()
    expect(a.store.getSnapshot().lightsStatus).toBe('error'); expect(a.store.page().ok).toBe(false)
    const b = fixture({ health: vi.fn(async () => { throw new Error('boom') }) }); await b.store.load()
    expect(b.store.getSnapshot().lights).toHaveLength(5); expect(b.store.page().ok).toBe(false)
    expect(b.store.canAllOn()).toBe(false); expect(b.store.canAllOff()).toBe(false)
  })
})

describe('Lights fail-closed permissions (zero writes)', () => {
  const attempt = async (store: ReturnType<typeof fixture>['store']) => {
    const main = get(store, 'room_main')
    await store.power(main, false); await store.brightness(main, 50); await store.color(main, [1, 2, 3])
    await store.temperature(main, 3000); await store.effect(main, 'Party'); await store.allOn(); await store.allOff()
  }
  it.each([
    ['read-only device', () => fixture({}, ['lights:read']), 'This device can view the lights but not control them.'],
    ['auth unresolved', () => fixture({}, scopes, false), 'Checking access…'],
    ['control disabled', () => fixture({ health: vi.fn(async () => ({ ...structuredClone(health), control_enabled: false })) }), 'Controls are turned off on the server.'],
    ['HA disconnected', () => fixture({ health: vi.fn(async () => ({ ...structuredClone(health), home_assistant: 'disconnected' as const })) }), 'Home Assistant is unavailable.'],
    ['WS offline', () => fixture({}, scopes, true, 'offline'), 'Offline · controls unavailable.'],
  ])('%s', async (_name, make, reason) => {
    const { api, store } = make(); await store.load()
    expect(store.page()).toEqual({ ok: false, reason })
    await attempt(store)
    expect(writeCalls(api)).toBe(0)
  })
  it('missing per-light permission, unavailable target, unsupported capability', async () => {
    const perms = { ...health.writable_permissions, room_main: ['power' as const] }
    const { api, store } = fixture({ health: vi.fn(async () => ({ ...structuredClone(health), writable_permissions: perms })) }); await store.load()
    expect(await store.brightness(get(store, 'room_main'), 10)).toBe(false)
    expect(await store.power(get(store, 'room_4'), true)).toBe(false) // unavailable
    const noFx = { ...get(store, 'desk_ambient'), capabilities: { ...get(store, 'desk_ambient').capabilities, effects: false } }
    expect(store.can(noFx, 'effect')).toBe(false)
    expect(await store.effect(get(store, 'room_main'), 'Not-a-real-effect')).toBe(false)
    expect(await store.temperature(get(store, 'room_main'), 9000)).toBe(false) // L530 max 6500
    expect(await store.color(get(store, 'room_main'), [300, 0, 0])).toBe(false)
    expect(writeCalls(api)).toBe(0)
  })
  it('never powers on implicitly: attribute writes are refused while the light is off', async () => {
    const { api, store } = fixture(); await store.load()
    const off = get(store, 'room_2')
    for (const action of ['brightness', 'color', 'temperature', 'effect'] as const) expect(store.can(off, action)).toBe(false)
    await store.brightness(off, 100); await store.color(off, [255, 0, 0]); await store.temperature(off, 3000); await store.effect(off, 'Party')
    expect(writeCalls(api)).toBe(0)
    expect(store.can(off, 'power')).toBe(true)
  })
  it('blocks a second command while one is pending (no duplicates)', async () => {
    let release!: () => void
    const { api, store } = fixture({ power: vi.fn(() => new Promise((resolve) => { release = () => resolve({}) })) }); await store.load()
    const first = store.power(get(store, 'room_main'), false)
    expect(await store.power(get(store, 'room_main'), false)).toBe(false)
    expect(await store.brightness(get(store, 'desk_ambient'), 50)).toBe(false)
    expect(await store.allOn()).toBe(false)
    release(); await first
    expect(api.power).toHaveBeenCalledTimes(1); expect(writeCalls(api)).toBe(1)
  })
})

describe('Lights command mapping and confirmation', () => {
  it('maps every action to its existing endpoint exactly once with bounded payloads', async () => {
    const cases: Array<[keyof LightsApi, (store: ReturnType<typeof fixture>['store'], light: Light) => Promise<boolean>, unknown]> = [
      ['power', (s, l) => s.power(l, false), false],
      ['brightness', (s, l) => s.brightness(l, 999), 255],
      ['color', (s, l) => s.color(l, [32, 102, 255]), [32, 102, 255]],
      ['temperature', (s, l) => s.temperature(l, 3000), 3000],
      ['effect', (s, l) => s.effect(l, 'Party'), 'Party'],
    ]
    for (const [endpoint, send, payload] of cases) {
      const { api, store } = fixture(); await store.load()
      await send(store, get(store, 'room_main'))
      expect(api[endpoint]).toHaveBeenCalledWith('room_main', payload)
      expect(writeCalls(api)).toBe(1)
    }
  })
  it('All on uses the group power endpoint; All off uses the all_off scene and never group power-off', async () => {
    const { api, store } = fixture(); await store.load()
    await store.allOn(); expect(api.allPower).toHaveBeenCalledWith(true)
    await store.allOff(); expect(api.activateScene).toHaveBeenCalledWith('all_off')
    expect(api.allPower).not.toHaveBeenCalledWith(false)
    expect(api.allPower).toHaveBeenCalledTimes(1); expect(api.activateScene).toHaveBeenCalledTimes(1)
  })
  it('is never optimistic: waits for the WS state, then confirms and announces once', async () => {
    const { store } = fixture(); await store.load()
    await store.power(get(store, 'room_main'), false)
    expect(get(store, 'room_main').on).toBe(true)
    expect(store.getSnapshot().feedback?.value.title).toBe('Sent · waiting for the light…')
    store.handleEvent(changed(later(get(store, 'room_main'), { on: false }, '2026-10-08T10:05:00Z')))
    expect(get(store, 'room_main').on).toBe(false)
    expect(store.getSnapshot().pending).toBeNull()
    expect(store.getSnapshot().announcement?.text).toBe('Main light updated.')
  })
  it('reports partial when the WS confirmation never arrives', async () => {
    vi.useFakeTimers()
    const { store } = fixture(); await store.load()
    await store.power(get(store, 'room_main'), false)
    vi.advanceTimersByTime(CONFIRM_TIMEOUT_MS + 1)
    expect(store.getSnapshot().feedback?.value.tone).toBe('partial')
    expect(store.getSnapshot().pending).toBeNull()
  })
  it('rejection shows the safe API reason; unknown errors are generic; nothing is retried', async () => {
    const a = fixture({ power: vi.fn(async () => { throw new ApiError(403, 'This device cannot control lights') }) }); await a.store.load()
    await a.store.power(get(a.store, 'room_main'), false)
    expect(a.store.getSnapshot().feedback?.value).toMatchObject({ tone: 'error', detail: 'This device cannot control lights' })
    const b = fixture({ power: vi.fn(async () => { throw new TypeError('secret internal 192.0.2.20 trace') }) }); await b.store.load()
    await b.store.power(get(b.store, 'room_main'), false)
    expect(b.store.getSnapshot().feedback?.value.detail).toBe('Please try again.')
    expect(b.api.power).toHaveBeenCalledTimes(1)
  })
  it('times out a hung request without replaying it', async () => {
    vi.useFakeTimers()
    const { api, store } = fixture({ power: vi.fn(() => new Promise(() => {})) }); await store.load()
    const run = store.power(get(store, 'room_main'), false)
    await vi.advanceTimersByTimeAsync(15_001); await run
    expect(store.getSnapshot().feedback?.value).toMatchObject({ tone: 'error', detail: 'Request timed out' })
    expect(api.power).toHaveBeenCalledTimes(1)
  })
  it('reports partial group results honestly', async () => {
    const { store } = fixture({
      allPower: vi.fn<LightsApi['allPower']>(async () => ({ accepted: true, device_id: 'all_room_lights', succeeded: ['room_main'], failed: [{ device_id: 'room_4', reason: 'unavailable' as const }] })),
      activateScene: vi.fn<LightsApi['activateScene']>(async () => ({ scene: 'all_off', name: 'All off', status: 'partial' as const, applied: ['room_main'], skipped: [{ device: 'room_4', reason: 'unavailable' }], failed: [], applied_at: 'x' })),
    }); await store.load()
    await store.allOn(); expect(store.getSnapshot().feedback?.value.tone).toBe('partial')
    await store.allOff(); expect(store.getSnapshot().feedback?.value).toMatchObject({ tone: 'partial', detail: '1 unavailable' })
  })
  it('does not resend the current effect', async () => {
    const { api, store } = fixture(); await store.load()
    expect(await store.effect(get(store, 'room_main'), 'Off')).toBe(false)
    expect(api.effect).not.toHaveBeenCalled()
  })
})

describe('Lights state helpers', () => {
  it('never fabricates Kelvin outside white mode', () => {
    expect(kelvinValue(lights[0])).toBe(2700)
    expect(kelvinValue({ ...lights[0], color_mode: 'hs', color_temperature_kelvin: 4500 })).toBeNull()
    expect(kelvinValue({ ...lights[0], on: false })).toBeNull()
    expect(colorMode(lights[2])).toBe('colour')
    expect(stateLine(lights[2])).toBe('On · 50% · Colour')
    expect(stateLine(lights[3])).toBe('Unavailable'); expect(stateLine(lights[1])).toBe('Off')
  })
  it('labels the Off effect as No effect and compares observation times', () => {
    expect(effectLabel('Off')).toBe('No effect'); expect(effectLabel('Aurora')).toBe('Aurora')
    expect(isNewer(later(lights[0], {}, '2026-10-08T09:00:00Z'), lights[0])).toBe(false)
    expect(isNewer(later(lights[0], {}, '2026-10-08T11:00:00Z'), lights[0])).toBe(true)
  })
})


describe('Lights ordering and command races', () => {
  it('retains HA microsecond ordering, rejects duplicate frames and undated restoration', () => {
    const current = later(lights[0], {}, '2026-10-08T10:00:00.123500Z')
    expect(isNewer(later(current, {}, '2026-10-08T10:00:00.123499Z'), current)).toBe(false)
    expect(isNewer(later(current, {}, '2026-10-08T13:00:00.123501+03:00'), current)).toBe(true)
    expect(isNewer(current, current)).toBe(false)
    expect(isNewer({ ...current, last_updated: null }, current)).toBe(false)
    expect(isNewer({ ...current, available: false, last_updated: null }, current)).toBe(true)
  })
  it('checks the latest authoritative target, not a stale drag-start object', async () => {
    const { api, store } = fixture(); await store.load()
    const old = get(store, 'room_main')
    store.handleEvent(changed(later(old, { on: false })))
    expect(await store.brightness(old, 100)).toBe(false)
    expect(await store.color(old, [1, 2, 3])).toBe(false)
    store.handleEvent(changed(later(old, { available: false }, '2026-10-08T10:00:06Z')))
    expect(await store.power(old, true)).toBe(false)
    expect(writeCalls(api)).toBe(0)
  })
  it('confirms a matching WS observation that precedes the HTTP response', async () => {
    let ack!: () => void
    const { store } = fixture({ power: vi.fn(() => new Promise((resolve) => { ack = () => resolve({}) })) }); await store.load()
    const run = store.power(get(store, 'room_main'), false)
    store.handleEvent(changed(later(lights[0], { on: false })))
    expect(store.getSnapshot().pending).not.toBeNull()
    ack(); await run
    expect(store.getSnapshot().pending).toBeNull()
    expect(store.getSnapshot().feedback?.value.tone).toBe('success')
    expect(get(store, 'room_main').on).toBe(false)
  })
  it('another device changing a different attribute is not confirmation of this command', async () => {
    const { store } = fixture(); await store.load()
    await store.brightness(get(store, 'room_main'), 200)
    store.handleEvent(changed(later(lights[0], { effect: 'Party' })))
    expect(store.getSnapshot().pending).not.toBeNull()
    store.handleEvent(changed(later(lights[0], { brightness: 200 }, '2026-10-08T10:00:06Z')))
    expect(store.getSnapshot().pending).toBeNull()
  })
  it('delayed acknowledgement cannot overwrite a newer conflicting state', async () => {
    let ack!: () => void
    const { store } = fixture({ brightness: vi.fn(() => new Promise((resolve) => { ack = () => resolve({}) })) }); await store.load()
    const run = store.brightness(get(store, 'room_main'), 200)
    store.handleEvent(changed(later(lights[0], { brightness: 200 })))
    store.handleEvent(changed(later(lights[0], { brightness: 50 }, '2026-10-08T10:00:06Z')))
    ack(); await run
    expect(get(store, 'room_main').brightness).toBe(50)
    expect(store.getSnapshot().pending).not.toBeNull()
  })
  it('idle creates zero resource reads; explicit foreground refresh is single-flight', async () => {
    vi.useFakeTimers()
    const { api, store } = fixture(); await store.load()
    await vi.advanceTimersByTimeAsync(180_000)
    expect(api.lights).toHaveBeenCalledTimes(1); expect(api.health).toHaveBeenCalledTimes(1)
    await Promise.all([store.load(), store.load()])
    expect(api.lights).toHaveBeenCalledTimes(2); expect(api.health).toHaveBeenCalledTimes(2)
  })
  it('does not invent brightness or on/off state', () => {
    expect(stateLine({ ...lights[0], brightness: null })).not.toContain('%')
    expect(stateLine({ ...lights[0], on: null })).toBe('State unavailable')
  })
})


describe('Lights final safety gates', () => {
  it('does not offer a group command when no target is reachable', async () => {
    const { api, store } = fixture({ lights: vi.fn(async () => lights.map((l) => ({ ...l, available: false, on: null }))) }); await store.load()
    expect(store.canAllOn()).toBe(false); expect(store.canAllOff()).toBe(false)
    await store.allOn(); await store.allOff(); expect(writeCalls(api)).toBe(0)
  })
  it('all_off requires per-target power permission independently of group power permission', async () => {
    const { store, api } = fixture({ health: vi.fn(async () => ({ ...structuredClone(health), writable_permissions: { ...health.writable_permissions, room_3: [] } })) }); await store.load()
    expect(store.canAllOn()).toBe(true); expect(store.canAllOff()).toBe(false)
    await store.allOff(); expect(api.activateScene).not.toHaveBeenCalled()
  })
  it('a missing confirmation closes writes until an explicit state refresh', async () => {
    vi.useFakeTimers()
    const { api, store } = fixture(); await store.load()
    await store.power(get(store, 'room_main'), false)
    await vi.advanceTimersByTimeAsync(CONFIRM_TIMEOUT_MS + 1)
    expect(store.page().ok).toBe(false)
    expect(await store.power(get(store, 'room_main'), false)).toBe(false)
    expect(api.power).toHaveBeenCalledTimes(1)
    await store.load(); expect(store.page().ok).toBe(true)
  })
  it('unrelated camera/system events cause zero reads or state loss', async () => {
    const { store, api } = fixture(); await store.load()
    const before = store.getSnapshot().lights
    store.handleEvent({ type: 'camera.updated', data: {}, timestamp: 'x', device_id: null } as RoomOSEvent)
    expect(store.getSnapshot().lights).toBe(before); expect(api.lights).toHaveBeenCalledTimes(1)
  })
})

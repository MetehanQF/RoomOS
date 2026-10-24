import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { fixture, health, lights } from './lights-fixtures.test-support'
import { CONFIRM_TIMEOUT_MS } from './lights-store'
import type { GroupCommandResult, Light, RoomOSEvent } from './types'

const MAIN = ['room_main', 'room_2', 'room_3', 'room_4']
const white = (light: Light, change: Partial<Light> = {}): Light => ({ ...light, available: true, on: true, brightness: 128, color_mode: 'color_temp', color_temperature_kelvin: 3000, last_updated: '2026-10-15T10:00:00Z', ...change })
const roomAll = (change: Partial<Light> = {}) => lights.map((light) => MAIN.includes(light.id) ? white(light, change) : light)
const permitted = { ...health, writable_permissions: { ...health.writable_permissions, main_room_lights: ['power', 'brightness', 'temperature'] as Array<'power' | 'brightness' | 'temperature'> } }
const ok = (succeeded = MAIN): GroupCommandResult => ({ accepted: true, device_id: 'main_room_lights', succeeded, failed: [] })
const event = (light: Light): RoomOSEvent => ({ type: 'light.state_changed', data: { light }, device_id: light.id, timestamp: '2026-10-15T10:00:05Z' })
const observe = (store: ReturnType<typeof fixture>['store'], change: Partial<Light>, ids = MAIN, at = '2026-10-15T10:00:05Z') => {
  for (const id of ids) store.handleEvent(event({ ...store.getSnapshot().lights!.find((light) => light.id === id)!, ...change, last_updated: at }))
}
afterEach(() => vi.useRealTimers())

function setup(state = roomAll(), overrides = {}) {
  const made = fixture({ lights: vi.fn(async () => structuredClone(state)), health: vi.fn(async () => structuredClone(permitted)),
    mainPower: vi.fn(async () => ok()), mainBrightness: vi.fn(async () => ok()), mainTemperature: vi.fn(async () => ok()), ...overrides })
  return made
}

describe('main room lights group commands', () => {
  it('is fail-closed without the group permission, while pending, or before trusted state', async () => {
    const { store } = fixture({ health: vi.fn(async () => structuredClone(health)) })
    expect(store.canMain('power')).toBe(false)
    await store.load()
    expect(store.canMain('power')).toBe(false) // health lacks main_room_lights
    const allowed = setup()
    expect(allowed.store.canMain('power')).toBe(false) // not loaded yet
    await allowed.store.load()
    expect(allowed.store.canMain('power')).toBe(true)
  })

  it('brightness and temperature only when every available member is on (turn_on never powers lights on silently)', async () => {
    const { store } = setup(roomAll().map((light) => light.id === 'room_2' ? { ...light, on: false, brightness: null } : light))
    await store.load()
    expect(store.canMain('power')).toBe(true)
    expect(store.canMain('brightness')).toBe(false)
    expect(store.canMain('temperature')).toBe(false)
    expect(await store.mainBrightness(40)).toBe(false)
  })

  it('one group request; success only after every member reports the new state', async () => {
    const { api, store } = setup(roomAll({ on: false, brightness: null }))
    await store.load()
    await store.mainPower(true)
    expect(api.mainPower).toHaveBeenCalledTimes(1)
    expect(api.mainPower).toHaveBeenCalledWith(true)
    expect(api.power).not.toHaveBeenCalled()
    expect(store.getSnapshot().pending).toMatchObject({ lightId: null, action: 'main_power', acknowledged: true })
    observe(store, { on: true, brightness: 128 }, MAIN.slice(0, 3))
    expect(store.getSnapshot().pending).not.toBeNull()
    observe(store, { on: true, brightness: 128 }, ['room_4'])
    expect(store.getSnapshot().pending).toBeNull()
    expect(store.getSnapshot().feedback).toMatchObject({ scope: 'main', value: { tone: 'success' } })
    expect(api.lights).toHaveBeenCalledTimes(1) // confirmed by events, no refetch
  })

  it('brightness and temperature send bounded values to the group endpoints', async () => {
    const { api, store } = setup()
    await store.load()
    await store.mainBrightness(80)
    expect(api.mainBrightness).toHaveBeenCalledWith(204)
    observe(store, { brightness: 204 })
    await store.mainTemperature(4012)
    expect(api.mainTemperature).toHaveBeenCalledWith(4012)
    observe(store, { color_temperature_kelvin: 4000 }, MAIN, '2026-10-15T10:00:09Z')
    expect(store.getSnapshot().pending).toBeNull()
    expect(await store.mainTemperature(9000)).toBe(false) // outside the members' common range
    expect(api.mainTemperature).toHaveBeenCalledTimes(1)
  })

  it('a group command where every light failed is an error, never "partially applied"', async () => {
    const none: GroupCommandResult = { accepted: false, device_id: 'main_room_lights', succeeded: [], failed: ['room_main', 'room_2', 'room_3', 'room_4'].map((device_id) => ({ device_id, reason: 'service_failed' as const })) }
    const { store } = setup(undefined, { mainPower: vi.fn(async () => none) })
    await store.load()
    await store.mainPower(true)
    expect(store.getSnapshot().feedback).toMatchObject({ scope: 'main', value: { tone: 'error', title: 'Ana Oda Işıkları: uygulanamadı' } })
  })

  it('partial result is reported and the real state is re-read once', async () => {
    const partial: GroupCommandResult = { accepted: false, device_id: 'main_room_lights', succeeded: ['room_main', 'room_2', 'room_4'], failed: [{ device_id: 'room_3', reason: 'service_failed' }] }
    const { api, store } = setup(undefined, { mainPower: vi.fn(async () => partial) })
    await store.load()
    await store.mainPower(false)
    await Promise.resolve(); await Promise.resolve()
    expect(store.getSnapshot().feedback).toMatchObject({ scope: 'main', value: { tone: 'partial' } })
    expect(store.getSnapshot().feedback!.value.detail).toBe('1 ışık yanıt vermedi: Ambient light')
    expect(api.lights).toHaveBeenCalledTimes(2)
    expect(api.health).toHaveBeenCalledTimes(2)
    expect(api.mainPower).toHaveBeenCalledTimes(1) // never retried
  })

  it('a rejected command shows the safe reason and re-reads state, never retried', async () => {
    const { api, store } = setup(undefined, { mainPower: vi.fn(async () => { throw new ApiError(503, 'Home Assistant is unavailable') }) })
    await store.load()
    await store.mainPower(false)
    expect(store.getSnapshot().feedback).toMatchObject({ scope: 'main', value: { tone: 'error', detail: 'Home Assistant is unavailable' } })
    expect(api.lights).toHaveBeenCalledTimes(2)
    expect(api.mainPower).toHaveBeenCalledTimes(1)
  })

  it('missing confirmation ends as partial and resynchronizes', async () => {
    vi.useFakeTimers()
    const { api, store } = setup()
    await store.load()
    await store.mainPower(false)
    observe(store, { on: false, brightness: null }, ['room_main'])
    await vi.advanceTimersByTimeAsync(CONFIRM_TIMEOUT_MS + 1)
    expect(store.getSnapshot().feedback).toMatchObject({ scope: 'main', value: { tone: 'partial' } })
    expect(api.lights).toHaveBeenCalledTimes(2)
  })

  it('blocks per-light commands while a group command is pending and vice versa', async () => {
    const { api, store } = setup(undefined, { mainPower: vi.fn(() => new Promise<GroupCommandResult>(() => undefined)) })
    await store.load()
    void store.mainPower(false)
    expect(store.can(store.getSnapshot().lights![0], 'power')).toBe(false)
    expect(await store.mainPower(true)).toBe(false)
    expect(api.mainPower).toHaveBeenCalledTimes(1)
  })
})

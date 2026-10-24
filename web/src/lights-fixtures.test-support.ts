import { vi } from 'vitest'
import { LightsStore, type LightsApi } from './lights-store'
import type { ConnectionStatus, Health, Light } from './types'
import type { LightAction } from './ui'

export const scopes = ['lights:read', 'lights:write', 'activity:read']
const ALL: Light['capabilities'] = { power: true, brightness: true, color: true, temperature: true, effects: true, min_kelvin: 2500, max_kelvin: 6500 }
const l530 = (id: string, name: string, extra: Partial<Light> = {}): Light => ({
  id, name, type: 'light', available: true, on: true, brightness: 128, color_mode: 'color_temp', rgb: [255, 200, 150], hs: [30, 40],
  color_temperature_kelvin: 2700, effect: 'Off', effects: ['Off', 'Party', 'Relax'], capabilities: { ...ALL }, last_updated: '2026-10-08T10:00:00Z', ...extra,
})
export const lights: Light[] = [
  l530('room_main', 'Main Lamp'),
  l530('room_2', 'Lamp 2', { on: false, brightness: null, color_mode: null, color_temperature_kelvin: null, effect: null }),
  l530('room_3', 'Lamp 3', { color_mode: 'hs', rgb: [255, 55, 70], color_temperature_kelvin: null }),
  l530('room_4', 'Lamp 4', { available: false, on: null, brightness: null, color_mode: null, color_temperature_kelvin: null, effect: null, last_updated: null }),
  l530('desk_ambient', 'Desk strip', {
    effects: ['Off', 'Aurora', 'Bubbling Cauldron', 'Candy Cane', 'Christmas', 'Flicker', 'Ocean', 'Rainbow'],
    capabilities: { ...ALL, max_kelvin: 9000 },
  }),
]
const everything: LightAction[] = ['power', 'brightness', 'color', 'temperature', 'effect']
export const health: Health = {
  status: 'ok', home_assistant: 'connected', live_connection_enabled: true, control_enabled: true, control_policy: 'production',
  writable_device_ids: [], writable_actions: [],
  writable_permissions: { all_room_lights: ['power'], ...Object.fromEntries(lights.map((light) => [light.id, everything])) },
  alerts: { status: 'operational', active_count: 0, highest_severity: null },
  notifications: { status: 'healthy', configured_providers: 1, configured_destinations: 1, pending_count: 0, failed_count: 0, external_attempts: 0, external_deliveries: 0, last_delivery: null },
}

export const writeCalls = (api: LightsApi) => (['power', 'brightness', 'color', 'temperature', 'effect', 'allPower', 'activateScene'] as const).reduce((sum, key) => sum + vi.mocked(api[key]).mock.calls.length, 0)

export function makeApi(overrides: Partial<LightsApi> = {}) {
  return {
    lights: vi.fn<LightsApi['lights']>(async () => structuredClone(lights)),
    health: vi.fn<LightsApi['health']>(async () => structuredClone(health)),
    power: vi.fn<LightsApi['power']>(async (id) => ({ device_id: id })),
    brightness: vi.fn<LightsApi['brightness']>(async (id) => ({ device_id: id })),
    color: vi.fn<LightsApi['color']>(async (id) => ({ device_id: id })),
    temperature: vi.fn<LightsApi['temperature']>(async (id) => ({ device_id: id })),
    effect: vi.fn<LightsApi['effect']>(async (id) => ({ device_id: id })),
    mainPower: vi.fn<LightsApi['mainPower']>(async () => ({ accepted: true, device_id: 'main_room_lights', succeeded: ['room_main', 'room_2', 'room_3', 'room_4'], failed: [] })),
    mainBrightness: vi.fn<LightsApi['mainBrightness']>(async () => ({ accepted: true, device_id: 'main_room_lights', succeeded: ['room_main', 'room_2', 'room_3', 'room_4'], failed: [] })),
    mainTemperature: vi.fn<LightsApi['mainTemperature']>(async () => ({ accepted: true, device_id: 'main_room_lights', succeeded: ['room_main', 'room_2', 'room_3', 'room_4'], failed: [] })),
    allPower: vi.fn<LightsApi['allPower']>(async () => ({ accepted: true, device_id: 'all_room_lights', succeeded: ['room_main', 'room_2', 'room_3', 'desk_ambient'], failed: [] })),
    activateScene: vi.fn<LightsApi['activateScene']>(async () => ({ scene: 'all_off', name: 'All off', status: 'success', applied: ['room_main'], skipped: [], failed: [], applied_at: '2026-10-08T10:00:00Z' })),
    ...overrides,
  }
}

export function fixture(overrides: Partial<LightsApi> = {}, access: readonly string[] = scopes, auth = true, connection: ConnectionStatus = 'connected') {
  const api = makeApi(overrides)
  return { api, store: new LightsStore(api, access, auth, connection) }
}

import { vi } from 'vitest'
import { HomeStore, type HomeApi } from './home-store'
import type { AlertRecord, Health, Light, SceneCatalog, SceneActivationResult } from './types'
import { automationCatalog } from './routines-fixtures.test-support'

export const scopes = ['lights:read', 'lights:write', 'activity:read', 'camera:read', 'system:read', 'routines:read']
export const ids = ['normal', 'work_1', 'work_2', 'gaming_1', 'gaming_2', 'movie', 'night', 'all_off'] as const
export const lights: Light[] = ['room_main', 'room_2', 'room_3', 'room_4', 'desk_ambient'].map((id, index) => ({
  id, name: `Light ${index + 1}`, type: 'light', available: index === 4, on: false, brightness: null,
  color_mode: null, rgb: null, hs: null, color_temperature_kelvin: null, effect: null, effects: [], last_updated: null,
  capabilities: { power: true, brightness: true, color: true, temperature: true, effects: false, min_kelvin: 2500, max_kelvin: 6500 },
}))
export const catalog: SceneCatalog = { scenes: ids.map((id) => ({ id, name: id.replace('_', ' '), configured: true, activatable: true, target_count: 5 })), active_scene: null, last_applied: null }
export const health: Health = {
  status: 'ok', home_assistant: 'connected', live_connection_enabled: true, control_enabled: true, control_policy: 'production',
  writable_device_ids: [], writable_actions: [], writable_permissions: { all_room_lights: ['power'] },
  alerts: { status: 'critical', active_count: 1, highest_severity: 'critical' },
  notifications: { status: 'healthy', configured_providers: 1, configured_destinations: 1, pending_count: 0, failed_count: 0, external_attempts: 0, external_deliveries: 0, last_delivery: null },
}
export const alert: AlertRecord = { id: 'secondary_offline', type: 'node_offline', severity: 'critical', title: 'Secondary offline', message: 'Secondary is offline', source: 'system', started_at: '2026-10-07T12:00:00Z', updated_at: '2026-10-07T12:00:00Z', status: 'active' }
export const result: SceneActivationResult = { scene: 'normal', name: 'Normal', status: 'success', applied: ['desk_ambient'], skipped: [], failed: [], applied_at: '2026-10-07T12:00:00Z' }
export function fixture(overrides: Partial<HomeApi> = {}, access: readonly string[] = scopes, auth = true) {
  const api = {
    health: vi.fn(async () => structuredClone(health)), lights: vi.fn(async () => structuredClone(lights)),
    scenes: vi.fn(async () => structuredClone(catalog)),
    systemHealth: vi.fn<HomeApi['systemHealth']>(async () => ({ overall: 'healthy', checked_at: '2026-10-07T12:00:00Z', components: [] })),
    alerts: vi.fn<HomeApi['alerts']>(async () => ({ active: [alert], active_count: 1, highest_severity: 'critical', storage_status: 'ok' })),
    routines: vi.fn<HomeApi['routines']>(async () => ({ routines: [], timezone: 'Europe/Istanbul', storage_status: 'ok', can_write: false, can_run: false, ha_trigger_ids: [], scenes: [] })),
    automations: vi.fn<HomeApi['automations']>(async () => automationCatalog([])),
    camera: vi.fn<HomeApi['camera']>(async () => ({ authorized: true, camera: null })),
    activateScene: vi.fn<HomeApi['activateScene']>(async (id) => ({ ...result, scene: id })),
    allPower: vi.fn<HomeApi['allPower']>(async () => ({ accepted: true, device_id: 'all_room_lights', succeeded: ['desk_ambient'], failed: [] })),
    ...overrides,
  }
  return { api, store: new HomeStore(api, access, auth, 'connected') }
}

import { vi } from 'vitest'
import { RoutinesStore, type RoutinesApi } from './routines-store'
import type { ActivityCatalog, Automation, AutomationCatalog, AutomationExecutionResult, AutomationHistoryPage, ExecutionRecord, Health, HealthReport, Routine, RoutineCatalog } from './types'

export const EVENING_ID = 'rt_6b200000000000000000000000000001'
export const ALL_OFF_ID = 'rt_6b200000000000000000000000000002'
export const ALL_SCOPES = ['lights:read', 'lights:write', 'system:read', 'routines:read', 'routines:write', 'camera:read', 'activity:read']
export const READ_SCOPES = ['lights:read', 'system:read', 'camera:read', 'activity:read', 'routines:read']

export const evening: Routine = {
  id: EVENING_ID, name: 'Evening Home Lighting', enabled: true, managed: true, trigger: { type: 'evening_home' },
  action: { type: 'activate_scene', scene_id: 'normal' }, created_at: '2026-10-06T05:51:50.966713Z', updated_at: '2026-10-06T15:38:53.305549Z',
  last_run: '2026-10-06T15:38:53.305546Z', last_result: { execution_id: null, status: 'partial', scene_status: 'partial', source: 'ha_state', detail: null },
}
export const allOff: Routine = {
  id: ALL_OFF_ID, name: '02:00 All Off', enabled: true, managed: true, trigger: { type: 'time', time: '02:00', days: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] },
  action: { type: 'activate_scene', scene_id: 'all_off' }, created_at: '2026-10-06T05:51:50.966713Z', updated_at: '2026-10-09T23:00:09.166333Z',
  last_run: '2026-10-09T23:00:09.166330Z', last_result: { execution_id: 'ex_7586b28e504c484980d93570a3b67f02', status: 'success', scene_status: 'success', source: 'schedule', detail: null },
}
export const userRoutine: Routine = {
  id: 'rt_11111111111111111111111111111111', name: 'Weeknight', enabled: true, managed: false,
  trigger: { type: 'time', time: '22:30', days: ['mon', 'tue', 'wed', 'thu', 'fri'] }, action: { type: 'activate_scene', scene_id: 'night' },
  created_at: '2026-09-30T00:00:00Z', updated_at: '2026-09-30T00:00:00Z', last_run: null, last_result: null,
}

const targets = (failed: string[] = []) => ['room_main', 'room_2', 'room_3', 'room_4', 'desk_ambient'].map((device_id) => ({
  device_id, actions: ['power' as const], result: failed.includes(device_id) ? 'failed' as const : 'applied' as const, reason: failed.includes(device_id) ? 'ha_error' : null, stage: 'apply' as const, duration_ms: 120,
}))
export const exec = (id: string, started_at: string, change: Partial<ExecutionRecord> = {}): ExecutionRecord => ({
  id, source: 'routine', trigger: 'schedule', routine_id: ALL_OFF_ID, routine_name: '02:00 All Off', scene_id: 'all_off', scene_name: 'Tümünü Kapat',
  result: 'success', reason: null, started_at, finished_at: started_at, duration_ms: 313, targets: targets(), ...change,
})
export const scheduled = [
  exec('ex_dd06b4d9920047f9aa4c6c041091f87e', '2026-10-07T23:00:05.467515Z'),
  exec('ex_04a70c1ee4fc4207bc93d6cb407fe74a', '2026-10-08T23:00:09.222815Z', { result: 'partial', duration_ms: 5035, targets: targets(['room_2']) }),
  exec('ex_7586b28e504c484980d93570a3b67f02', '2026-10-09T23:00:08.866355Z', { duration_ms: 295 }),
]
export const manualAllOff = [
  exec('ex_0250fb71f4214a11beebe5a002fc7478', '2026-10-07T20:52:12.915916Z', { source: 'manual', trigger: 'manual', routine_id: null, routine_name: null }),
  exec('ex_e43f2197d1d54fc7b4158b56579d4b7c', '2026-10-09T21:25:39.450422Z', { source: 'manual', trigger: 'manual', routine_id: null, routine_name: null }),
]
export const names = { room_main: 'Main light', room_2: 'Light 2', room_3: 'Light 3', room_4: 'Light 4', desk_ambient: 'Desk ambient' }
export const activityOf = (records: ExecutionRecord[]): ActivityCatalog => ({
  activity: records.map((execution, index) => ({ id: `act_${index}`, type: 'scene_activated', title: 'x', message: 'x', source: execution.source === 'routine' ? 'routine' : 'scenes', occurred_at: execution.started_at, severity: null, reference_id: `scene.${execution.scene_id}`, execution_id: execution.id, execution, device_names: names })),
  execution_storage_status: 'ok', total: records.length, limit: 100, offset: 0, storage_status: 'ok',
})
export const catalogOf = (routines: Routine[], change: Partial<RoutineCatalog> = {}): RoutineCatalog => ({
  routines, timezone: 'Europe/Istanbul', storage_status: 'ok', can_write: true, can_run: true, ha_trigger_ids: [],
  scenes: [{ id: 'normal', name: 'Normal' }, { id: 'night', name: 'Gece' }, { id: 'all_off', name: 'Tümünü Kapat' }], ...change,
})
export const control: Health = {
  status: 'ok', home_assistant: 'connected', live_connection_enabled: true, control_enabled: true, control_policy: 'production', writable_device_ids: [], writable_actions: [], writable_permissions: {},
  alerts: { status: 'operational', active_count: 0, highest_severity: null },
  notifications: { status: 'healthy', configured_providers: 1, configured_destinations: 1, pending_count: 0, failed_count: 0, external_attempts: 3, external_deliveries: 3, last_delivery: null },
}

export const smartAutomation: Automation = {
  schema_version: 1, id: `au_${'1'.repeat(32)}`, name: 'Workstation heat guard', description: 'Dim the room when Workstation stays hot.', enabled: false,
  trigger: { type: 'device_health', device_id: 'workstation', metric: 'temperature', operator: 'above', value: 78, for_seconds: 300 },
  conditions: null, actions: [{ type: 'scene', scene_id: 'night' }], cooldown_seconds: 900, failure_policy: 'stop_on_failure',
  created_at: '2026-10-19T08:00:00Z', updated_at: '2026-10-19T08:00:00Z', last_triggered: null, last_outcome: null,
}
export const automationCatalog = (automations = [smartAutomation]): AutomationCatalog => ({
  automations, timezone: 'Europe/Istanbul', storage_status: 'ok', can_write: true, can_enable: true,
  limits: { condition_depth: 3, conditions: 20, actions: 8, execution_seconds: 15 },
  targets: {
    lights: [{ id: 'desk_ambient', name: 'Desk ambient' }, { id: 'main_room_lights', name: 'Room lights' }],
    devices: [{ id: 'workstation', name: 'Workstation', metrics: ['online', 'cpu', 'ram', 'disk', 'temperature'] }],
  },
})
export const automationHistory = (cursor: string | null = '1'): AutomationHistoryPage => ({
  items: [{ timestamp: '2026-10-19T09:00:00Z', automation_id: smartAutomation.id, automation_name: smartAutomation.name, outcome: 'blocked', trigger_source: 'device_health', trigger_reason: 'temperature_above', reason: 'condition_unknown', correlation_id: `ac_${'2'.repeat(32)}`, duration_ms: 0, error: null }],
  next_cursor: cursor,
})
export const simulation: AutomationExecutionResult = {
  mode: 'SIMULATION', correlation_id: `ac_${'3'.repeat(32)}`, automation_id: smartAutomation.id, outcome: 'success', reason: null, condition: null,
  actions: [{ index: 0, status: 'skipped', detail: 'would_run' }], duration_ms: 0,
}
export const report = (status: 'healthy' | 'degraded' | 'unhealthy' | 'unknown' = 'healthy', summary = 'Scheduler running · 2 of 2 enabled'): HealthReport => ({
  overall: status === 'unhealthy' ? 'unhealthy' : 'healthy', checked_at: '2026-10-10T13:15:52.760137Z',
  components: [
    { id: 'home_assistant', name: 'Home Assistant', status: 'healthy', critical: true, summary: 'Connected · live updates active', details: [], checked_at: '2026-10-10T13:15:52Z' },
    { id: 'automations', name: 'Automations', status, critical: true, summary, details: [], checked_at: '2026-10-10T13:15:52Z' },
  ],
})

export function routinesFixture({ scopes = ALL_SCOPES, routines = [evening, allOff], executions = [...scheduled, ...manualAllOff], connection = 'connected' as const, catalog = {} as Partial<RoutineCatalog> } = {}) {
  const state = { activity: activityOf(executions) }
  const api = {
    routines: vi.fn(async () => catalogOf(structuredClone(routines), catalog)),
    health: vi.fn(async () => structuredClone(control)),
    systemHealth: vi.fn(async () => report()),
    executions: vi.fn(async () => structuredClone(state.activity)),
    run: vi.fn(), update: vi.fn(), create: vi.fn(), remove: vi.fn(),
    automations: vi.fn(async () => automationCatalog()), automationHistory: vi.fn(async () => automationHistory()),
    createAutomation: vi.fn(), updateAutomation: vi.fn(), deleteAutomation: vi.fn(), enableAutomation: vi.fn(),
    simulateAutomation: vi.fn(async () => simulation), runAutomation: vi.fn(),
  } satisfies Record<keyof RoutinesApi, unknown>
  const store = new RoutinesStore(api as unknown as RoutinesApi, scopes, true, connection)
  return { api, store, state }
}

export const writeCalls = (api: ReturnType<typeof routinesFixture>['api']) => api.run.mock.calls.length + api.update.mock.calls.length + api.create.mock.calls.length + api.remove.mock.calls.length

export const diagnostics: import('./types').RoutineDiagnostics = {
  presence: { presence_freshness: 'unknown', reporting: 'stale', basis: 'live_tracker_event', observed_at: '2026-10-10T01:00:00Z', threshold_seconds: 21600, gps_observation_available: false },
  storage_status: 'ok', retention_days: 30, record_limit: 250,
  evaluations: [{ evaluation_id: 'ev_' + 'a'.repeat(32), routine_id: EVENING_ID, evaluated_at: '2026-10-10T15:30:00Z', trigger_origin: 'sunset', completion_state: 'complete', outcome: 'skipped', reason_code: 'presence_away', execution_id: null, execution_result: null }],
}

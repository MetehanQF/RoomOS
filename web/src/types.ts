export type Capabilities = {
  power: boolean
  brightness: boolean
  color: boolean
  temperature: boolean
  effects: boolean
  min_kelvin: number
  max_kelvin: number
}

export type Light = {
  id: string
  name: string
  type: 'light'
  available: boolean
  on: boolean | null
  brightness: number | null
  color_mode: string | null
  rgb: [number, number, number] | null
  hs: [number, number] | null
  color_temperature_kelvin: number | null
  effect: string | null
  effects: string[]
  capabilities: Capabilities
  last_updated: string | null
}

export type TelemetryFreshness = 'fresh' | 'stale' | 'unknown' | 'unavailable'

export type Metric = {
  id: string
  value: string | number | boolean | null
  unit: string | null
  available: boolean
  stale: boolean
  freshness: TelemetryFreshness
  last_updated: string | null
}

export type SystemNode = {
  id: 'primary' | 'secondary'
  name: string
  online: boolean | null
  metrics: Record<string, Metric>
}

export type ServiceStatus = {
  id: string
  status: 'online' | 'offline' | 'unknown'
  health: 'healthy' | 'degraded' | 'offline' | 'unknown'
  source: string
  stale: boolean
  freshness: TelemetryFreshness
}

export type DnsResolverStatus = {
  id: 'primary' | 'secondary'
  role: 'primary' | 'secondary'
  status: 'healthy' | 'degraded' | 'offline' | 'unknown'
  last_checked: string | null
  response_time_ms: number | null
}

export type DnsClusterStatus = {
  status: 'healthy' | 'degraded' | 'offline' | 'unknown'
  primary: DnsResolverStatus
  secondary: DnsResolverStatus
  last_checked: string | null
}

export type SystemData = {
  primary: SystemNode
  secondary: SystemNode
  services: ServiceStatus[]
  dns_cluster: DnsClusterStatus
}

export type ReleaseInfo = {
  release: string
  revision: string
  component: 'core'
}

export type CameraStatus = {
  id: 'room_camera'
  display_name: string
  status: 'online' | 'offline' | 'unknown'
  detection_status: 'active' | 'paused' | 'offline' | 'unknown'
  frigate_health: 'healthy' | 'degraded' | 'offline' | 'unknown'
  last_person_event: string | null
  last_person_event_freshness: 'recent' | 'stale' | 'none'
  snapshot_available: boolean
  live_view_available: boolean
  last_checked: string | null
}

export type CameraCatalog = { cameras: CameraStatus[] }

export type CameraAccess = {
  authorized: boolean
  camera: CameraStatus | null
}

export type Health = {
  status: string
  home_assistant: 'connected' | 'disconnected' | 'not_configured'
  live_connection_enabled: boolean
  control_enabled: boolean
  control_policy: 'read_only' | 'production' | 'scoped' | string
  writable_device_ids: string[]
  writable_actions: string[]
  writable_permissions: Record<string, Array<'power' | 'brightness' | 'color' | 'temperature' | 'effect'>>
  alerts: AlertSummary
  notifications: NotificationSummary
}

export type AlertSeverity = 'info' | 'warning' | 'critical'
export type AlertSummary = { status: 'operational' | 'attention' | 'critical' | 'unknown'; active_count: number; highest_severity: AlertSeverity | null }
export type NotificationSummary = { status: 'disabled' | 'healthy' | 'degraded' | 'unavailable' | 'corrupt'; configured_providers: number; configured_destinations: number; pending_count: number; failed_count: number; external_attempts: number; external_deliveries: number; last_delivery: string | null }
export type AlertRecord = {
  id: string; type: string; severity: AlertSeverity; title: string; message: string; source: string
  started_at: string; updated_at: string; status: 'active' | 'resolved'
  occurrence_count?: number; last_observed_at?: string | null; last_occurrence_at?: string | null
  read_at?: string | null; acknowledged_at?: string | null; acknowledged_by?: string | null
  muted_until?: string | null; muted_by?: string | null
}
export type AlertCatalog = { active: AlertRecord[]; active_count: number; highest_severity: AlertSeverity | null; storage_status: 'ok' | 'corrupt' }
export type AlertInsight = {
  value: number | null; unit: string; profile: AlertProfile; severity_basis: AlertSeverity | null
  threshold: number | null; minutes: number | null; coverage: number | null; since: string | null
  free_gb: number | null; reason: string
}
export type AlertSnooze = { until: string; severity: AlertSeverity }
export type AlertProfile = 'normal' | 'gaming' | 'development'
export type AlertProfiles = { devices: Record<'workstation' | 'primary' | 'secondary', { profile: AlertProfile; until: string | null }> }
export type NotificationCatalog = {
  notifications: AlertRecord[]; total: number; unread_count: number; active_count: number
  highest_severity: AlertSeverity | null; limit: number; offset: number; storage_status: 'ok' | 'corrupt'
  snoozes?: Record<string, AlertSnooze>; insights?: Record<string, AlertInsight>
}
export type QuietHours = { enabled: boolean; start: string; end: string; allow_critical: boolean }
export type PushPreferences = {
  enabled: boolean; critical: boolean; warning: boolean; info: boolean
  recovery: 'off' | 'critical' | 'all'; quiet_hours: QuietHours; lock_screen_details: boolean
  snooze_until?: string | null; updated_at?: string | null
}
export type PushPreferencesUpdate = Partial<Omit<PushPreferences, 'snooze_until' | 'updated_at'>> & { snooze_minutes?: number }
export type PushChannel = { kind: 'fcm' | 'webpush'; status: 'active' | 'invalid' | 'suspended'; since: string; last_accepted_at?: string | null; endpoint_host?: string | null }
export type PushDelivery = {
  id: string; transition: 'started' | 'escalated' | 'resolved' | 'summary'; severity: AlertSeverity
  status: 'pending' | 'deferred' | 'accepted' | 'displayed' | 'opened' | 'failed' | 'expired' | 'superseded' | 'skipped' | 'aggregated' | 'invalid' | 'cancelled'
  reason?: string | null; created_at: string; accepted_at?: string | null; displayed_at?: string | null; opened_at?: string | null
}
export type PushSettings = {
  device_id: string; display_name: string; session_kind: string | null; can_manage: boolean; can_receive: boolean
  preferences: PushPreferences; channels: PushChannel[]; recent: PushDelivery[]
  capabilities: { mode: 'legacy' | 'smart' | 'off'; delivery_active: boolean; webpush_available: boolean; webpush_public_key?: string | null; fcm_available: boolean; timezone: string }
}
export type WebPushSubscriptionJSON = { endpoint: string; expirationTime?: number | null; keys: { p256dh: string; auth: string } }
export type ActivityRecord = { id: string; type: 'alert_started' | 'alert_resolved' | 'scene_activated' | 'scene_failed'; title: string; message: string; source: string; occurred_at: string; severity: AlertSeverity | null; reference_id: string; execution_id?: string | null; execution?: ExecutionRecord | null; device_names?: Record<string, string> }
export type ActivityCatalog = { activity: ActivityRecord[]; execution_storage_status?: 'ok' | 'corrupt' | 'write_failed'; total: number; limit: number; offset: number; storage_status: 'ok' | 'corrupt' }

export type GroupCommandResult = {
  accepted: boolean
  device_id: 'all_room_lights' | 'main_room_lights'
  succeeded: string[]
  failed: { device_id: string; reason: 'unavailable' | 'service_failed' }[]
}

export type SceneSummary = {
  id: 'normal' | 'work_1' | 'work_2' | 'gaming_1' | 'gaming_2' | 'movie' | 'night' | 'all_off'
  name: string
  configured: boolean
  activatable: boolean
  target_count: number
}

export type SceneLastApplied = {
  scene: SceneSummary['id']
  name: string
  status: 'success' | 'partial'
  applied_at: string
}

export type SceneCatalog = {
  scenes: SceneSummary[]
  last_applied: SceneLastApplied | null
  active_scene: SceneSummary['id'] | null
}

export type SceneActivationResult = {
  execution_id?: string | null
  scene: SceneSummary['id']
  name: string
  status: 'success' | 'partial' | 'failed'
  applied: string[]
  skipped: { device: string; reason: string }[]
  failed: { device: string; reason: string }[]
  applied_at: string
}

/** Mirrors Core EventType (core/app/models.py); parity with shared/api/event.schema.json is tested. */
export const ROOMOS_EVENT_TYPES = [
  'roomos.connection',
  'light.state_changed',
  'scene.active_changed',
  'system.metric_updated',
  'system.snapshot_updated',
  'system.service_updated',
  'camera.updated',
  'device.updated',
  'alert.started',
  'alert.resolved',
  'alert.updated',
  'activity.recorded',
  'routine.changed',
  'routine.deleted',
  'routine.run_completed',
  'routine.diagnostics',
  'automation.changed',
  'automation.deleted',
  'automation.executed',
] as const
export type RoomOSEventType = typeof ROOMOS_EVENT_TYPES[number]

export type RoomOSEvent = {
  type: RoomOSEventType
  timestamp: string
  device_id: string | null
  data: {
    light?: Light
    active_scene?: SceneSummary['id'] | null
    routine_id?: string
    diagnostics?: RoutineDiagnostics
    device?: DeviceDetail
    routine?: Routine
    automation_id?: string
    automation?: Automation
    result?: AutomationExecutionResult
    camera?: CameraStatus
    home_assistant?: 'connecting' | 'connected' | 'disconnected'
    [key: string]: unknown
  }
}

export type RoutineTrigger =
  | { type: 'manual' }
  | { type: 'time'; time: string; days: Array<'mon' | 'tue' | 'wed' | 'thu' | 'fri' | 'sat' | 'sun'> }
  | { type: 'ha_state'; trigger_id: string; state: string }
  | { type: 'evening_home' }

export type Routine = {
  id: string
  name: string
  enabled: boolean
  managed: boolean
  trigger: RoutineTrigger
  action: { type: 'activate_scene'; scene_id: SceneSummary['id'] }
  created_at: string
  updated_at: string
  last_run: string | null
  last_result: {
    execution_id?: string | null
    status: 'success' | 'partial' | 'failed'
    scene_status: 'success' | 'partial' | 'failed' | null
    source: 'manual' | 'schedule' | 'ha_state'
    detail: string | null
  } | null
}

export type RoutineDraft = {
  name: string
  enabled: boolean
  trigger: RoutineTrigger
  action: { type: 'activate_scene'; scene_id: SceneSummary['id'] }
}

export type RoutineEvaluation = {
  evaluation_id: string; routine_id: string; evaluated_at: string
  trigger_origin: 'sunset' | 'arrival' | 'schedule' | 'manual' | null
  completion_state: 'pending' | 'complete' | 'incomplete'
  outcome: 'executed' | 'skipped' | 'failed' | null
  reason_code: string | null; execution_id: string | null
  execution_result: 'success' | 'partial' | 'failed' | 'rejected' | 'interrupted' | null
  presence_reporting?: 'fresh' | 'stale' | 'unknown' | null
}
export type RoutineDiagnostics = {
  presence: { presence_freshness: 'fresh' | 'stale' | 'unknown'; reporting: 'fresh' | 'stale' | 'unknown'; basis: 'live_tracker_event' | null; observed_at: string | null; threshold_seconds: number; gps_observation_available: boolean }
  storage_status: 'ok' | 'corrupt' | 'write_failed'
  retention_days: number; record_limit: number; evaluations: RoutineEvaluation[]
}

export type RoutineCatalog = {
  diagnostics?: RoutineDiagnostics | null
  routines: Routine[]
  timezone: string
  storage_status: 'ok' | 'corrupt'
  can_write: boolean
  can_run: boolean
  ha_trigger_ids: string[]
  scenes: Array<{ id: SceneSummary['id']; name: string }>
}

export type RoutineRunResponse = {
  routine: Routine
  scene: SceneActivationResult
}

export type AutomationWeekday = 'mon' | 'tue' | 'wed' | 'thu' | 'fri' | 'sat' | 'sun'
export type AutomationTrigger =
  | { type: 'time'; time: string; days: AutomationWeekday[] }
  | { type: 'ha_state'; target_id: 'room_main' | 'room_2' | 'room_3' | 'room_4' | 'desk_ambient' | 'main_room_lights'; from_state?: 'on' | 'off' | null; to_state?: 'on' | 'off' | null; attribute?: 'brightness' | 'temperature' | null; operator?: 'above' | 'below' | null; threshold?: number | null; for_seconds: number }
  | { type: 'device_health'; device_id: string; metric: 'online' | 'cpu' | 'ram' | 'disk' | 'temperature'; operator: 'equals' | 'above' | 'below'; value: boolean | number; for_seconds: number }

export type AutomationPredicate = {
  kind: 'predicate'
  source: 'light' | 'device' | 'time'
  target_id: string | null
  field: 'state' | 'available' | 'online' | 'cpu' | 'ram' | 'disk' | 'temperature' | 'local_time'
  operator: 'equals' | 'not_equals' | 'greater_than' | 'less_than' | 'between'
  value: string | number | boolean | null
  upper: number | null
}
export type AutomationCondition = AutomationPredicate | { kind: 'group'; operator: 'and' | 'or' | 'not'; children: AutomationCondition[] }

export type AutomationAction =
  | { type: 'scene'; scene_id: SceneSummary['id'] }
  | { type: 'routine'; routine_id: string }
  | { type: 'light'; target_id: 'room_main' | 'room_2' | 'room_3' | 'room_4' | 'desk_ambient' | 'main_room_lights'; operation: 'power' | 'brightness' | 'color' | 'temperature'; on: boolean | null; brightness: number | null; rgb: [number, number, number] | null; kelvin: number | null }

export type AutomationDraft = {
  name: string
  description: string
  enabled: boolean
  trigger: AutomationTrigger
  conditions: AutomationCondition | null
  actions: AutomationAction[]
  cooldown_seconds: number
  failure_policy: 'stop_on_failure' | 'continue'
}

export type Automation = AutomationDraft & {
  schema_version: 1
  id: string
  created_at: string
  updated_at: string
  last_triggered: string | null
  last_outcome: 'success' | 'partial' | 'failed' | 'skipped' | 'blocked' | null
}

export type AutomationCatalog = {
  automations: Automation[]
  timezone: string
  storage_status: 'ok' | 'corrupt'
  can_write: boolean
  can_enable: boolean
  limits: { condition_depth: number; conditions: number; actions: number; execution_seconds: number }
  targets: {
    lights: Array<{ id: Extract<AutomationTrigger, { type: 'ha_state' }>['target_id']; name: string }>
    devices: Array<{ id: string; name: string; metrics: Array<Extract<AutomationTrigger, { type: 'device_health' }>['metric']> }>
  }
}

export type AutomationConditionTrace = { result: 'true' | 'false' | 'unknown'; summary: string; children: AutomationConditionTrace[] }
export type AutomationExecutionResult = {
  mode: 'LIVE' | 'SIMULATION'
  correlation_id: string
  automation_id: string
  outcome: 'success' | 'partial' | 'failed' | 'skipped' | 'blocked'
  reason: string | null
  condition: AutomationConditionTrace | null
  actions: Array<{ index: number; status: 'success' | 'failed' | 'skipped'; detail: string | null }>
  duration_ms: number
}

export type AutomationHistoryRecord = {
  timestamp: string
  automation_id: string
  automation_name: string
  outcome: AutomationExecutionResult['outcome']
  trigger_source: string
  trigger_reason: string
  reason: string | null
  correlation_id: string
  duration_ms: number
  error: string | null
}
export type AutomationHistoryPage = { items: AutomationHistoryRecord[]; next_cursor: string | null }

export type ConnectionStatus = 'connected' | 'reconnecting' | 'offline'
export type RealtimeStatus = ConnectionStatus

export type CommandFeedback = {
  tone: 'pending' | 'success' | 'partial' | 'error'
  title: string
  detail?: string
}

export type AuthDevice = {
  id: string
  friendly_name: string
  scopes: string[]
}

export type AuthStatus = {
  authenticated: boolean
  device: AuthDevice | null
}

export type HistoryNode = 'primary' | 'secondary'
export type HistoryMetric = 'cpu' | 'ram' | 'temperature'
export type HistoryRange = '1h' | '6h' | '24h'

export type HistoryPoint = {
  ts: string
  avg: number | null
  min: number | null
  max: number | null
  samples: number
}

export type HistoryGap = { start: string; end: string; reason: 'no_data' | 'unavailable' | 'offline' }

export type HistorySeries = {
  node: HistoryNode
  metric: HistoryMetric
  unit: string
  range: HistoryRange
  bucket_seconds: number
  points: HistoryPoint[]
  gaps: HistoryGap[]
  source: 'home_assistant'
  freshness: TelemetryFreshness
  generated_at: string
  cached: boolean
}

export type ExecutionTarget = { device_id: string; actions: ('power' | 'brightness' | 'temperature' | 'color' | 'effect')[]; result: 'applied' | 'skipped' | 'failed'; reason: string | null; stage: 'read' | 'wake' | 'apply'; duration_ms: number }
export type ExecutionRecord = { id: string; source: 'manual' | 'routine'; trigger: 'manual' | 'schedule' | 'ha_state'; routine_id: string | null; routine_name: string | null; scene_id: string; scene_name: string; result: 'running' | 'success' | 'partial' | 'failed' | 'rejected' | 'interrupted'; reason: string | null; started_at: string; finished_at: string | null; duration_ms: number | null; targets: ExecutionTarget[] }

export type HealthStatus = 'healthy' | 'degraded' | 'unhealthy' | 'unknown'
export type HealthComponentId = 'home_assistant' | 'control' | 'persistent_state' | 'automations' | 'execution_history' | 'notifications' | 'telemetry_history' | 'backups' | 'background_services'
export type HealthComponent = { id: HealthComponentId; name: string; status: HealthStatus; critical: boolean; summary: string; details: string[]; checked_at: string }
export type HealthReport = { overall: Exclude<HealthStatus, 'unknown'>; checked_at: string; components: HealthComponent[] }

/* Device Registry (Phase 7D.1). Values are latest-only and never zero-filled. */
export type DeviceType = 'windows' | 'android' | 'linux' | 'roomos_panel' | 'other'
export type DeviceSourceKind = 'system_monitor' | 'home_assistant' | 'mqtt' | 'roomos_native' | 'roomos_web'
export type SensorSupport = 'supported' | 'unverified' | 'unsupported'
export type SensorAvailability = 'available' | 'unavailable' | 'unsupported' | 'unknown'
export type DeviceFreshness = 'fresh' | 'stale' | 'unknown'
export type DeviceConnection = 'online' | 'offline' | 'unknown'
export type DeviceHealth = 'healthy' | 'degraded' | 'offline' | 'unknown'
export type SensorReason = 'source_not_configured' | 'awaiting_first_report' | 'source_unavailable' | 'invalid_report'
  | 'source_reported_unknown' | 'invalid_timestamp' | 'clock_discontinuity' | 'not_supported_by_device'
export type DeviceSource = { kind: DeviceSourceKind; configured: boolean; connection: DeviceConnection; last_seen_at: string | null; stale_after_seconds: number }
export type DeviceSensor = {
  id: string; kind: 'number' | 'boolean' | 'text'; support: SensorSupport; source: DeviceSourceKind | null
  value: string | number | boolean | null; unit: string | null; availability: SensorAvailability; freshness: DeviceFreshness
  /** When RoomOS received the value. */ observed_at: string | null
  /** Only set when the source states a measurement time. */ measured_at: string | null
  reason: SensorReason | null
}
export type DeviceSummary = {
  id: string; name: string; type: DeviceType; platform: string
  connection: DeviceConnection; health: DeviceHealth; telemetry_freshness: DeviceFreshness
  last_seen_at: string | null; last_telemetry_at: string | null; status_reason: 'source_not_configured' | 'awaiting_first_report' | null
  capabilities: Record<string, SensorSupport>; sources: DeviceSource[]; metadata: Record<string, string>
}
export type DeviceDetail = DeviceSummary & { sensors: DeviceSensor[] }
/** 7D.4: the list carries sensors (same shape as a device.updated event). */
export type DeviceList = { devices: DeviceDetail[]; generated_at: string }
/** A paired RoomOS app (auth device). Coarse by design: no scopes, tokens or session data. */
export type PairedClient = { id: string; name: string; app: 'web' | 'android' | 'unknown'; paired_at: string; last_seen_at: string | null; active_session: boolean; current: boolean }
export type PairedClientList = { clients: PairedClient[]; last_seen_resolution_seconds: number; generated_at: string }
export type DeviceTelemetry = { device_id: string; sensors: DeviceSensor[]; generated_at: string }

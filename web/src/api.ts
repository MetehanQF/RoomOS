import type { ActivityCatalog, AlertCatalog, AlertProfile, AlertProfiles, AlertRecord, AlertSeverity, AuthStatus, Automation, AutomationCatalog, AutomationDraft, AutomationExecutionResult, AutomationHistoryPage, CameraCatalog, GroupCommandResult, Health, Light, NotificationCatalog, PushPreferencesUpdate, PushSettings, ReleaseInfo, Routine, RoutineCatalog, RoutineDraft, RoutineRunResponse, SceneActivationResult, SceneCatalog, SystemData, WebPushSubscriptionJSON } from './types'
import { connectionStatus } from './connection-state'
import { historyQuery, parseHistory } from './trends'
import { parseHealth } from './health'
import { DEVICE_ID, parseDeviceDetail, parseDeviceList, parseDeviceTelemetry, parsePairedClients } from './devices-state'
import type { HistoryMetric, HistoryNode, HistoryRange } from './types'
import type { PanelLayout, PanelLayoutResponse } from './panel-layout'
import type { MetricHistoryData, MetricRange } from './components/MetricHistory'
import type { DiskForecastData } from './disk-forecast'

const BASE = '/api/v1'

export class ApiError extends Error {
  constructor(public status: number, message: string, public retryAfter: number | null = null) {
    super(message)
    this.name = 'ApiError'
  }
}

export function friendlyApiMessage(status: number) {
  if (status === 401) return 'This device needs to be paired with RoomOS.'
  if (status === 403) return 'Controls are currently locked.'
  if (status === 404) return 'That RoomOS device is not available.'
  if (status === 409) return 'This scene is not configured yet.'
  if (status === 429) return 'That scene is already being applied.'
  if (status === 503) return 'RoomOS cannot reach Home Assistant right now.'
  if (status === 422) return 'That setting is not supported by this light.'
  if (status >= 500) return 'RoomOS Core is temporarily unavailable.'
  return 'The command could not be completed.'
}

export function csrfToken(cookie = typeof document === 'undefined' ? '' : document.cookie) {
  const value = cookie.split(';').map((item) => item.trim()).find((item) => item.startsWith('roomos_csrf='))
  return value ? decodeURIComponent(value.slice('roomos_csrf='.length)) : null
}

async function request<T>(path: string, init?: RequestInit, csrf = true): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase()
  if (csrf && !['GET', 'HEAD', 'OPTIONS'].includes(method) && connectionStatus() !== 'connected') {
    throw new Error('RoomOS is offline. Controls are unavailable until the live connection returns.')
  }
  const token = csrf && !['GET', 'HEAD', 'OPTIONS'].includes(method) ? csrfToken() : null
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(token ? { 'X-CSRF-Token': token } : {}), ...init?.headers },
  })
  if (!response.ok) {
    if (response.status === 401 && typeof window !== 'undefined') {
      window.dispatchEvent(new Event('roomos:auth-required'))
    }
    const retryAfter = response.status === 429 ? Number(response.headers.get('Retry-After')) : NaN
    throw new ApiError(response.status, friendlyApiMessage(response.status), Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function parseSystemData(payload: unknown): SystemData {
  if (!isRecord(payload)
    || !isRecord(payload.primary)
    || !isRecord(payload.secondary)
    || !isRecord(payload.primary.metrics)
    || !isRecord(payload.secondary.metrics)
    || !isRecord(payload.dns_cluster)
    || !isRecord(payload.dns_cluster.primary)
    || !isRecord(payload.dns_cluster.secondary)
    || !Array.isArray(payload.services)) {
    throw new ApiError(502, 'RoomOS Core returned an incompatible system response.')
  }
  return payload as SystemData
}

export function parseReleaseInfo(payload: unknown): ReleaseInfo {
  if (!isRecord(payload)
    || typeof payload.release !== 'string'
    || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(payload.release)
    || typeof payload.revision !== 'string'
    || !/^(unknown|[0-9a-f]{40})$/.test(payload.revision)
    || payload.component !== 'core'
    || Object.keys(payload).some((key) => !['release', 'revision', 'component'].includes(key))) {
    throw new ApiError(502, 'RoomOS Core returned an incompatible release response.')
  }
  return payload as ReleaseInfo
}

export const api = {
  authStatus: () => request<AuthStatus>('/auth/status'),
  pair: (code: string, friendly_name: string) =>
    request<AuthStatus>('/auth/pair', {
      method: 'POST',
      body: JSON.stringify({ code, friendly_name }),
    }, false),
  health: () => request<Health>('/health'),
  panelLayout: () => request<PanelLayoutResponse>('/panel/layout'),
  savePanelLayout: (layout: PanelLayout) => request<PanelLayoutResponse>('/panel/layout', { method: 'PUT', body: JSON.stringify(layout) }),
  resetPanelLayout: () => request<PanelLayoutResponse>('/panel/layout', { method: 'DELETE' }),
  lights: () => request<Light[]>('/lights'),
  system: async () => parseSystemData(await request<unknown>('/system')),
  version: async () => parseReleaseInfo(await request<unknown>('/system/version')),
  systemHealth: async (signal?: AbortSignal) => parseHealth(await request<unknown>('/system/health', { signal, cache: 'no-store' })),
  history: async (node: HistoryNode, metric: HistoryMetric, range: HistoryRange) =>
    parseHistory(await request<unknown>(historyQuery(node, metric, range)), node, metric, range),
  scenes: () => request<SceneCatalog>('/scenes'),
  cameras: () => request<CameraCatalog>('/cameras'),
  alerts: () => request<AlertCatalog>('/alerts'),
  notifications: (filters: { status?: 'all' | 'active' | 'resolved'; severity?: AlertSeverity | ''; unread?: 'all' | 'true' | 'false' } = {}) => {
    const query = new URLSearchParams()
    if (filters.status && filters.status !== 'all') query.set('status', filters.status)
    if (filters.severity) query.set('severity', filters.severity)
    if (filters.unread && filters.unread !== 'all') query.set('unread', filters.unread)
    const suffix = query.toString()
    return request<NotificationCatalog>(`/notifications${suffix ? `?${suffix}` : ''}`)
  },
  markNotificationRead: (id: string) => request<AlertRecord>(`/notifications/${encodeURIComponent(id)}/read`, { method: 'POST' }),
  acknowledgeNotification: (id: string) => request<AlertRecord>(`/notifications/${encodeURIComponent(id)}/acknowledge`, { method: 'POST' }),
  muteNotification: (id: string, minutes = 60) => request<AlertRecord>(`/notifications/${encodeURIComponent(id)}/mute`, { method: 'POST', body: JSON.stringify({ minutes }) }),
  snoozeNotification: (id: string, hours: 2 | 8 | 24 | 168) => request<{ until: string }>(`/notifications/${encodeURIComponent(id)}/snooze`, { method: 'POST', body: JSON.stringify({ hours }) }),
  unsnoozeNotification: (id: string) => request<{ cleared: boolean }>(`/notifications/${encodeURIComponent(id)}/snooze`, { method: 'DELETE' }),
  alertProfiles: () => request<AlertProfiles>('/alerts/profiles'),
  setAlertProfile: (device: keyof AlertProfiles['devices'], profile: AlertProfile, hours: number | null) => request<{ profile: AlertProfile; until: string | null }>(`/alerts/profiles/${device}`, { method: 'PUT', body: JSON.stringify({ profile, hours }) }),
  pushSettings: () => request<PushSettings>('/push/settings'),
  updatePushSettings: (update: PushPreferencesUpdate) => request<PushSettings>('/push/settings', { method: 'PUT', body: JSON.stringify(update) }),
  subscribeWebPush: (subscription: WebPushSubscriptionJSON) => request<PushSettings>('/push/webpush/subscription', { method: 'PUT', body: JSON.stringify(subscription) }),
  unsubscribeWebPush: () => request<PushSettings>('/push/webpush/subscription', { method: 'DELETE' }),
  activity: (category: 'all' | 'alerts' | 'scenes' = 'all', limit = 50, offset = 0) => request<ActivityCatalog>(`/activity?category=${category}&limit=${limit}&offset=${offset}`),
  cameraSnapshotUrl: (id: 'room_camera') => `${BASE}/cameras/${id}/snapshot`,
  /** 7D.3: this PWA's own device telemetry; the server derives the device from the session. */
  sendTelemetry: (body: string) => request<unknown>('/devices/self/telemetry', { method: 'POST', body }).then(() => undefined),
  activateScene: (id: string) => request<SceneActivationResult>(`/scenes/${id}/activate`, { method: 'POST' }),
  routines: () => request<RoutineCatalog>('/routines'),
  devices: async () => parseDeviceList(await request<unknown>('/devices', { cache: 'no-store' })),
  clients: async () => parsePairedClients(await request<unknown>('/clients', { cache: 'no-store' })),
  device: async (id: string) => {
    if (!DEVICE_ID.test(id)) throw new ApiError(404, friendlyApiMessage(404))
    return parseDeviceDetail(await request<unknown>(`/devices/${id}`, { cache: 'no-store' }))
  },
  deviceTelemetry: async (id: string) => {
    if (!DEVICE_ID.test(id)) throw new ApiError(404, friendlyApiMessage(404))
    return parseDeviceTelemetry(await request<unknown>(`/devices/${id}/telemetry`, { cache: 'no-store' }))
  },
  diskForecast: (id: string) => request<DiskForecastData>(`/devices/${encodeURIComponent(id)}/forecast?metric=disk_usage`),
  metricHistory: (id: string, metric: string, range: MetricRange) => request<MetricHistoryData>(`/devices/${encodeURIComponent(id)}/history?metric=${encodeURIComponent(metric)}&range=${range}`),
  createRoutine: (body: RoutineDraft) => request<Routine>('/routines', { method: 'POST', body: JSON.stringify(body) }),
  updateRoutine: (id: string, body: Partial<RoutineDraft>) => request<Routine>(`/routines/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteRoutine: (id: string) => request<void>(`/routines/${id}`, { method: 'DELETE' }),
  runRoutine: (id: string) => request<RoutineRunResponse>(`/routines/${id}/run`, { method: 'POST' }),
  automations: () => request<AutomationCatalog>('/automations'),
  automationHistory: (limit = 50, cursor: string | null = null) => {
    const query = new URLSearchParams({ limit: String(limit) })
    if (cursor !== null) query.set('cursor', cursor)
    return request<AutomationHistoryPage>(`/automations/history?${query.toString()}`)
  },
  createAutomation: (body: AutomationDraft) => request<Automation>('/automations', { method: 'POST', body: JSON.stringify(body) }),
  updateAutomation: (id: string, body: Partial<Omit<AutomationDraft, 'enabled'>>) => request<Automation>(`/automations/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteAutomation: (id: string) => request<void>(`/automations/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  simulateAutomation: (id: string) => request<AutomationExecutionResult>(`/automations/${encodeURIComponent(id)}/simulate`, { method: 'POST' }),
  enableAutomation: (id: string, enabled: boolean) => request<Automation>(`/automations/${encodeURIComponent(id)}/enabled`, { method: 'POST', body: JSON.stringify({ enabled }) }),
  runAutomation: (id: string) => request<AutomationExecutionResult>(`/automations/${encodeURIComponent(id)}/run`, { method: 'POST' }),
  power: (id: string, on: boolean) =>
    request(`/lights/${id}/power`, { method: 'POST', body: JSON.stringify({ on }) }),
  mainPower: (on: boolean) =>
    request<GroupCommandResult>('/lights/main_room_lights/power', { method: 'POST', body: JSON.stringify({ on }) }),
  mainBrightness: (brightness: number) =>
    request<GroupCommandResult>('/lights/main_room_lights/brightness', { method: 'POST', body: JSON.stringify({ brightness }) }),
  mainTemperature: (kelvin: number) =>
    request<GroupCommandResult>('/lights/main_room_lights/temperature', { method: 'POST', body: JSON.stringify({ kelvin }) }),
  allPower: (on: boolean) =>
    request<GroupCommandResult>('/lights/all/power', {
      method: 'POST',
      body: JSON.stringify({ on }),
    }),
  brightness: (id: string, brightness: number) =>
    request(`/lights/${id}/brightness`, {
      method: 'POST',
      body: JSON.stringify({ brightness }),
    }),
  color: (id: string, rgb: [number, number, number]) =>
    request(`/lights/${id}/color`, { method: 'POST', body: JSON.stringify({ rgb }) }),
  temperature: (id: string, kelvin: number) =>
    request(`/lights/${id}/temperature`, {
      method: 'POST',
      body: JSON.stringify({ kelvin }),
    }),
  effect: (id: string, effect: string) =>
    request(`/lights/${id}/effect`, {
      method: 'POST',
      body: JSON.stringify({ effect }),
    }),
}

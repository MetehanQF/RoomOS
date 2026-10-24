import { ApiError, friendlyApiMessage } from './api'
import { groupControlEnabled } from './ui'
import type { AlertRecord, CameraAccess, ConnectionStatus, Health, HealthReport, Light, Routine, RoutineCatalog, SceneActivationResult, SceneCatalog, SceneSummary } from './types'

export type SceneId = SceneSummary['id']

/** Daily scene tiles. Work/Gaming are split tiles with explicit variants; All Off lives in the hero. */
export const SCENE_TILES: ReadonlyArray<
  | { kind: 'single'; id: SceneId; label: string; hint: string; icon: 'sun' | 'film' | 'moon' }
  | { kind: 'split'; key: 'work' | 'gaming'; label: string; icon: 'briefcase' | 'gamepad'; variants: ReadonlyArray<{ id: SceneId; label: '1' | '2'; hint: string }> }
> = [
  { kind: 'single', id: 'normal', label: 'Normal', hint: 'Günlük aydınlatma', icon: 'sun' },
  { kind: 'split', key: 'work', label: 'Work', icon: 'briefcase', variants: [{ id: 'work_1', label: '1', hint: 'Parlak' }, { id: 'work_2', label: '2', hint: 'Tam ışık' }] },
  { kind: 'split', key: 'gaming', label: 'Gaming', icon: 'gamepad', variants: [{ id: 'gaming_1', label: '1', hint: 'Aurora' }, { id: 'gaming_2', label: '2', hint: 'Lightning' }] },
  { kind: 'single', id: 'movie', label: 'Movie', hint: 'Loş film ışığı', icon: 'film' },
  { kind: 'single', id: 'night', label: 'Night', hint: 'Gece ışığı', icon: 'moon' },
]

export type RoomState = {
  mode: { kind: 'loading' } | { kind: 'scene'; id: SceneId; name: string } | { kind: 'off' } | { kind: 'custom' }
  total: number
  on: number
  available: number
  unavailable: number
}

/**
 * Room mode from authoritative data only: a scene is named only when server reconciliation says
 * it is active. Otherwise "Off" (no AVAILABLE light is on) or "Custom" — room states, not scenes.
 */
export function roomState(lights: Light[] | null, catalog: SceneCatalog | null): RoomState {
  if (!lights || !catalog) return { mode: { kind: 'loading' }, total: 0, on: 0, available: 0, unavailable: 0 }
  const available = lights.filter((light) => light.available)
  const on = available.filter((light) => light.on === true).length
  const active = catalog.active_scene ? catalog.scenes.find((scene) => scene.id === catalog.active_scene) : undefined
  const mode: RoomState['mode'] = active ? { kind: 'scene', id: active.id, name: active.name } : on === 0 ? { kind: 'off' } : { kind: 'custom' }
  return { mode, total: lights.length, on, available: available.length, unavailable: lights.length - available.length }
}

export function lightsSummary(state: RoomState) {
  if (state.mode.kind === 'loading') return null
  const base = `${state.on} of ${state.total} on`
  return state.unavailable ? `${base} · ${state.unavailable} unavailable` : base
}

export type ControlState = { scenes: boolean; group: boolean; reason: string | null }

/** Fail-closed write availability. Server enforcement stays authoritative; this prevents requests. */
export function controlState({ authResolved, scopes, connection, control }: {
  authResolved: boolean
  scopes: readonly string[]
  connection: ConnectionStatus
  control: Health | null
}): ControlState {
  const blocked = (reason: string): ControlState => ({ scenes: false, group: false, reason })
  if (!authResolved) return blocked('Checking access…')
  if (!scopes.includes('lights:write')) return blocked('This device can view the room but not control it.')
  if (!control) return blocked('Waiting for RoomOS…')
  if (!control.control_enabled) return blocked('Controls are turned off on the server.')
  if (connection === 'offline') return blocked('Offline · controls unavailable.')
  if (connection !== 'connected') return blocked('Reconnecting · controls paused.')
  if (control.home_assistant !== 'connected') return blocked('Home Assistant is unavailable.')
  return { scenes: true, group: groupControlEnabled(control), reason: null }
}

export function canActivate(control: ControlState, catalog: SceneCatalog | null, id: SceneId) {
  return control.scenes && Boolean(catalog?.scenes.find((scene) => scene.id === id)?.activatable)
}

const SEVERITY_ORDER: Record<AlertRecord['severity'], number> = { critical: 0, warning: 1, info: 2 }

/** Same ordering as the server's alert list: severity, then start time. */
export function sortAlerts(alerts: AlertRecord[]) {
  return [...alerts].sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity] || a.started_at.localeCompare(b.started_at) || a.id.localeCompare(b.id))
}

const DAY_LABELS: Record<string, string> = { mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun' }

/** Configured schedule text only — never a computed future timestamp. */
export function automationLines(catalog: RoutineCatalog | null) {
  if (!catalog) return []
  return catalog.routines.map((routine: Routine) => {
    const trigger = routine.trigger
    let schedule: string
    if (trigger.type === 'evening_home') schedule = 'At sunset · when home'
    else if (trigger.type === 'time') {
      const days = trigger.days.length === 7 ? 'Daily' : trigger.days.map((day) => DAY_LABELS[day]).join(', ')
      schedule = `${days} ${trigger.time} · ${catalog.timezone}`
    } else if (trigger.type === 'ha_state') schedule = 'On device state'
    else schedule = 'Manual'
    return { id: routine.id, name: routine.name, schedule, enabled: routine.enabled }
  })
}

export function healthSummary(report: HealthReport | null, failed: boolean) {
  if (failed) return { tone: 'unknown' as const, label: 'RoomOS status unavailable' }
  if (!report) return { tone: 'unknown' as const, label: failed ? 'RoomOS status unavailable' : 'RoomOS status…' }
  if (report.overall === 'healthy') return { tone: 'healthy' as const, label: 'RoomOS ok' }
  if (report.overall === 'degraded') return { tone: 'degraded' as const, label: 'RoomOS needs attention' }
  return { tone: 'offline' as const, label: 'RoomOS unavailable' }
}

export function cameraSummary(access: CameraAccess | null) {
  if (!access || !access.authorized) return null
  const camera = access.camera
  if (!camera) return 'Camera status unavailable'
  const state = camera.detection_status === 'paused' ? 'Detection paused' : camera.status === 'online' ? 'Online' : camera.status === 'offline' ? 'Offline' : 'Unknown'
  const frigate = camera.frigate_health === 'healthy' ? 'Frigate ok' : `Frigate ${camera.frigate_health}`
  return `${state} · ${frigate}`
}

export type HomeFeedback = {
  tone: 'pending' | 'success' | 'partial' | 'error'
  title: string
  detail?: string
  details: boolean
}

export function sceneFeedback(result: SceneActivationResult, activityAllowed: boolean): { feedback: HomeFeedback; announce: string } {
  if (result.status === 'success') return { feedback: { tone: 'success', title: `${result.name} applied`, details: false }, announce: `${result.name} applied.` }
  if (result.status === 'partial') {
    const parts = [result.skipped.length ? `${result.skipped.length} unavailable` : '', result.failed.length ? `${result.failed.length} failed` : ''].filter(Boolean)
    const lights = result.skipped.length + result.failed.length
    return {
      feedback: { tone: 'partial', title: `${result.name} partially applied`, detail: parts.join(' · '), details: activityAllowed },
      announce: `${result.name} partially applied. ${lights} light${lights === 1 ? '' : 's'} ${result.failed.length ? 'not updated' : 'unavailable'}.`,
    }
  }
  return { feedback: { tone: 'error', title: `${result.name} could not be applied`, details: activityAllowed }, announce: `${result.name} could not be applied.` }
}

/** Safe, server-owned or fixed text only; never raw transport errors. */
export function rejectionFeedback(name: string, cause: unknown): { feedback: HomeFeedback; announce: string } {
  const reason = cause instanceof ApiError ? friendlyApiMessage(cause.status) : 'Result unknown. Check room state before trying again.'
  return { feedback: { tone: 'error', title: `${name} could not be applied`, detail: reason, details: false }, announce: `${name} could not be applied. ${reason}` }
}

import { applyLightEvent, applySceneActivationResult, applySceneEvent, describeGroupResult } from './ui'
import { canActivate, controlState, rejectionFeedback, sceneFeedback, sortAlerts, type ControlState, type HomeFeedback, type SceneId } from './home-state'
import { actionGuard, type ActionGuard } from './routines-state'
import { routineErrorText } from './routines-store'
import type { AlertCatalog, AutomationCatalog, CameraAccess, ConnectionStatus, GroupCommandResult, Health, HealthReport, Light, RoomOSEvent, Routine, RoutineCatalog, RoutineRunResponse, SceneActivationResult, SceneCatalog } from './types'

/**
 * Event-driven Home data. One bounded REST bootstrap, then WebSocket events update local state.
 * Authoritative refresh happens only on (re)connect and return to foreground (via the lifecycle),
 * never after ordinary events. Each section loads independently; /api/v1/system is never used.
 */
export type HomeResources = {
  control: Health
  lights: Light[]
  scenes: SceneCatalog
  systemHealth: HealthReport
  alerts: AlertCatalog
  routines: RoutineCatalog
  automations: AutomationCatalog
  camera: CameraAccess
}
export type ResourceKey = keyof HomeResources
export type ResourceState<T> = { status: 'idle' | 'loading' | 'ready' | 'error' | 'forbidden'; data: T | null; error: string | null }

export interface HomeApi {
  health: () => Promise<Health>
  lights: () => Promise<Light[]>
  scenes: () => Promise<SceneCatalog>
  systemHealth: () => Promise<HealthReport>
  alerts: () => Promise<AlertCatalog>
  routines: () => Promise<RoutineCatalog>
  automations: () => Promise<AutomationCatalog>
  camera: (scopes: readonly string[]) => Promise<CameraAccess>
  activateScene: (id: SceneId) => Promise<SceneActivationResult>
  allPower: (on: boolean) => Promise<GroupCommandResult>
  /** 7L Dashboard quick run; optional so Home and Panel keep their existing API surface. */
  runRoutine?: (id: string) => Promise<RoutineRunResponse>
}

// null = any authenticated device (the /health control summary).
export const RESOURCE_SCOPES: Record<ResourceKey, string | null> = {
  control: null, lights: 'lights:read', scenes: 'lights:read', systemHealth: 'system:read',
  alerts: 'activity:read', routines: 'routines:read', automations: 'routines:read', camera: 'camera:read',
}
const KEYS = Object.keys(RESOURCE_SCOPES) as ResourceKey[]

export type HomeSnapshot = {
  resources: { [K in ResourceKey]: ResourceState<HomeResources[K]> }
  connection: ConnectionStatus
  loaded: boolean
  refreshing: boolean
  trusted: boolean
  pending: { kind: 'scene'; id: SceneId } | { kind: 'group' } | { kind: 'routine'; id: string } | null
  feedback: HomeFeedback | null
  announcement: { id: number; text: string } | null
}

export class HomeStore {
  private snapshot: HomeSnapshot
  private listeners = new Set<() => void>()
  private inflight: Promise<void> | null = null
  private announcements = 0
  private epoch = 0
  private loadEpoch = -1
  private queuedEvents: RoomOSEvent[] = []
  private targeted = new Map<ResourceKey, Promise<void>>()

  constructor(private api: HomeApi, private scopes: readonly string[], private authResolved: boolean, connection: ConnectionStatus = 'reconnecting') {
    const resources = Object.fromEntries(KEYS.map((key) => [key, { status: this.allowed(key) ? 'idle' : 'forbidden', data: null, error: null }])) as HomeSnapshot['resources']
    this.snapshot = { resources, connection, loaded: false, refreshing: false, trusted: false, pending: null, feedback: null, announcement: null }
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  getSnapshot = () => this.snapshot

  private allowed(key: ResourceKey) {
    const scope = RESOURCE_SCOPES[key]
    return scope === null || this.scopes.includes(scope)
  }

  private update(change: Partial<HomeSnapshot>) {
    this.snapshot = { ...this.snapshot, ...change }
    this.listeners.forEach((listener) => listener())
  }

  private setResource<K extends ResourceKey>(key: K, state: ResourceState<HomeResources[K]>) {
    this.update({ resources: { ...this.snapshot.resources, [key]: state } })
  }

  private fetch<K extends ResourceKey>(key: K): Promise<HomeResources[K]> {
    const api = this.api
    const loaders: { [P in ResourceKey]: () => Promise<HomeResources[P]> } = {
      control: api.health, lights: api.lights, scenes: api.scenes, systemHealth: api.systemHealth,
      alerts: api.alerts, routines: api.routines, automations: api.automations, camera: () => api.camera(this.scopes),
    }
    return loaders[key]()
  }

  /** Single-flight authoritative load of every permitted section; sections succeed or fail alone. */
  load(keys: ResourceKey[] = KEYS): Promise<void> {
    if (this.inflight) return this.loadEpoch === this.epoch ? this.inflight : this.inflight.then(() => this.load(keys))
    const epoch = this.epoch
    this.loadEpoch = epoch
    this.update({ refreshing: true, trusted: false })
    const wanted = keys.filter((key) => this.allowed(key))
    for (const key of wanted) {
      const current = this.snapshot.resources[key]
      if (!current.data) this.setResource(key, { ...current, status: 'loading' })
    }
    const run = Promise.allSettled(wanted.map(async (key) => {
      try {
        const data = await bounded(this.fetch(key))
        if (epoch !== this.epoch) return
        this.setResource(key, { status: 'ready', data, error: null } as ResourceState<HomeResources[typeof key]>)
      } catch (cause) {
        if (epoch !== this.epoch) return
        const current = this.snapshot.resources[key]
        // Keep last known data (shown as stale) rather than replacing it with nothing.
        this.setResource(key, { status: 'error', data: current.data, error: cause instanceof Error ? cause.message : 'Unavailable' } as ResourceState<HomeResources[typeof key]>)
      }
    })).then(() => {
      const events = this.queuedEvents.splice(0)
      this.update({ loaded: true, refreshing: false, trusted: epoch === this.epoch && this.snapshot.connection === 'connected' })
      if (epoch === this.epoch) events.forEach((event) => this.handleEvent(event))
    })
    this.inflight = run.finally(() => { this.inflight = null })
    return this.inflight
  }

  setConnection(connection: ConnectionStatus) {
    if (connection === this.snapshot.connection) return
    this.epoch += 1
    this.update({ connection, trusted: false })
  }

  control(): ControlState {
    const safety = ['control', 'lights', 'scenes'] as const
    if (!this.snapshot.trusted || this.snapshot.refreshing || safety.some((key) => this.snapshot.resources[key].status !== 'ready')) {
      return { scenes: false, group: false, reason: this.snapshot.connection === 'connected' ? 'Refreshing room state · controls paused.' : 'Offline or reconnecting · controls paused.' }
    }
    return controlState({ authResolved: this.authResolved, scopes: this.scopes, connection: this.snapshot.connection, control: this.snapshot.resources.control.data })
  }

  /** Local reducers; no REST call except the smallest targeted refresh where payload is insufficient. */
  handleEvent(event: RoomOSEvent) {
    if (this.snapshot.refreshing) { this.queuedEvents.push(event); return }
    const r = this.snapshot.resources
    switch (event.type) {
      case 'light.state_changed':
        if (r.lights.data) this.setResource('lights', { ...r.lights, data: applyLightEvent(r.lights.data, event) ?? r.lights.data })
        return
      case 'scene.active_changed':
        if (r.scenes.data) this.setResource('scenes', { ...r.scenes, data: applySceneEvent(r.scenes.data, event) })
        return
      case 'alert.started':
      case 'alert.resolved': {
        const alert = (event.data as { alert?: AlertCatalog['active'][number] }).alert
        if (!r.alerts.data || !alert || typeof alert.id !== 'string') return
        const rest = r.alerts.data.active.filter((item) => item.id !== alert.id)
        const active = sortAlerts(event.type === 'alert.started' && alert.status === 'active' ? [...rest, alert] : rest)
        this.setResource('alerts', { ...r.alerts, data: { ...r.alerts.data, active, active_count: active.length, highest_severity: active[0]?.severity ?? null } })
        return
      }
      case 'routine.changed':
      case 'routine.run_completed':
      case 'routine.deleted': {
        if (!r.routines.data) return
        const id = event.data.routine?.id ?? event.data.routine_id
        if (!id) return
        const rest = r.routines.data.routines.filter((item) => item.id !== id)
        const routines = event.type === 'routine.deleted' || !event.data.routine ? rest : [...rest, event.data.routine]
        const order = new Map(r.routines.data.routines.map((item, index) => [item.id, index]))
        routines.sort((a, b) => (order.get(a.id) ?? 1e6) - (order.get(b.id) ?? 1e6))
        this.setResource('routines', { ...r.routines, data: { ...r.routines.data, routines } })
        return
      }
      case 'automation.changed':
      case 'automation.executed':
      case 'automation.deleted': {
        if (!r.automations.data) return
        const id = event.data.automation?.id ?? event.data.automation_id
        if (!id) return
        const rest = r.automations.data.automations.filter((item) => item.id !== id)
        const automations = event.type === 'automation.deleted' || !event.data.automation ? rest : [...rest, event.data.automation]
        const order = new Map(r.automations.data.automations.map((item, index) => [item.id, index]))
        automations.sort((a, b) => (order.get(a.id) ?? 1e6) - (order.get(b.id) ?? 1e6))
        this.setResource('automations', { ...r.automations, data: { ...r.automations.data, automations } })
        return
      }
      case 'camera.updated':
        if (event.data.camera && this.allowed('camera')) this.setResource('camera', { status: 'ready', data: { authorized: true, camera: event.data.camera }, error: null })
        return
      case 'activity.recorded': {
        const activity = event.data.activity as { type?: string } | undefined
        // Scene completion events do not contain last_applied. Refresh only the catalog.
        if (activity?.type === 'scene_activated' || activity?.type === 'scene_failed') void this.refreshOne('scenes')
        return
      }
      case 'roomos.connection': {
        const ha = event.data.home_assistant
        const control = r.control.data
        if (!control || !ha || control.home_assistant === ha) return
        this.setResource('control', { ...r.control, data: { ...control, home_assistant: ha === 'connected' ? 'connected' : 'disconnected' } })
        // RoomOS Health has no event; the HA link change is the one moment it is worth re-reading.
        if (this.allowed('systemHealth')) void this.refreshOne('systemHealth')
        void this.refreshOne('control')
        if (this.allowed('scenes')) void this.refreshOne('scenes')
        return
      }
      default:
        return // system.* metric events are diagnostics; Home ignores them.
    }
  }

  private refreshOne(key: ResourceKey): Promise<void> {
    if (!this.allowed(key)) return Promise.resolve()
    const current = this.targeted.get(key)
    if (current) return current
    const epoch = this.epoch
    const previousScene = this.snapshot.resources.scenes.data
    const run = bounded(this.fetch(key)).then((data) => {
      if (epoch !== this.epoch) return
      if (key === 'scenes' && this.snapshot.resources.scenes.data !== previousScene) {
        data = { ...data as SceneCatalog, active_scene: this.snapshot.resources.scenes.data?.active_scene ?? null }
      }
      this.setResource(key, { status: 'ready', data, error: null } as ResourceState<HomeResources[typeof key]>)
    }).catch(() => {
      if (epoch === this.epoch) this.setResource(key, { ...this.snapshot.resources[key], status: 'error', error: 'Unavailable' } as ResourceState<HomeResources[typeof key]>)
    }).finally(() => { this.targeted.delete(key) })
    this.targeted.set(key, run)
    return run
  }

  private announce(text: string) {
    this.announcements += 1
    return { id: this.announcements, text }
  }

  /** Returns false without any request when the action is not currently allowed. */
  async activateScene(id: SceneId): Promise<boolean> {
    const scenes = this.snapshot.resources.scenes.data
    if (this.snapshot.pending || !canActivate(this.control(), scenes, id)) return false
    const name = scenes?.scenes.find((scene) => scene.id === id)?.name ?? 'Scene'
    this.update({ pending: { kind: 'scene', id }, feedback: { tone: 'pending', title: `Applying ${name}…`, details: false } })
    try {
      const result = await bounded(this.api.activateScene(id))
      const { feedback, announce } = sceneFeedback(result, this.allowed('alerts'))
      const current = this.snapshot.resources.scenes
      if (current.data) this.setResource('scenes', { ...current, data: applySceneActivationResult(current.data, result) })
      this.update({ pending: null, feedback, announcement: this.announce(announce) })
    } catch (cause) {
      const { feedback, announce } = rejectionFeedback(name, cause)
      this.update({ pending: null, feedback, announcement: this.announce(announce) })
    }
    return true
  }

  async allOn(): Promise<boolean> {
    if (this.snapshot.pending || !this.control().group) return false
    this.update({ pending: { kind: 'group' }, feedback: { tone: 'pending', title: 'Turning every light on…', details: false } })
    try {
      const result = await bounded(this.api.allPower(true))
      const described = describeGroupResult(result)
      const feedback: HomeFeedback = { tone: described.tone, title: described.title, detail: described.detail, details: false }
      this.update({ pending: null, feedback, announcement: this.announce(`${described.title}.`) })
    } catch (cause) {
      const { feedback, announce } = rejectionFeedback('All on', cause)
      this.update({ pending: null, feedback, announcement: this.announce(announce) })
    }
    return true
  }

  /** Same fail-closed guard as the Routines page; the server stays authoritative. */
  routineGuard(routine: Routine | null): ActionGuard {
    return actionGuard({
      routine, catalog: this.snapshot.resources.routines.data, scopes: this.scopes, authResolved: this.authResolved,
      control: this.snapshot.resources.control.data, connection: this.snapshot.connection,
      trusted: this.snapshot.trusted && !this.snapshot.refreshing, pending: this.snapshot.pending !== null, kind: 'run',
    })
  }

  /** Returns false without any request when the run is not currently allowed. */
  async runRoutine(id: string): Promise<boolean> {
    const routine = this.snapshot.resources.routines.data?.routines.find((item) => item.id === id) ?? null
    if (!routine || !this.api.runRoutine || !this.routineGuard(routine).ok) return false
    this.update({ pending: { kind: 'routine', id }, feedback: { tone: 'pending', title: `Running ${routine.name}…`, details: false } })
    try {
      const result = await bounded(this.api.runRoutine(id))
      const status = result.scene.status
      const routines = this.snapshot.resources.routines
      if (routines.data) {
        const list = routines.data.routines.map((item) => item.id === result.routine.id ? result.routine : item)
        this.setResource('routines', { ...routines, data: { ...routines.data, routines: list } })
      }
      const tone = status === 'success' ? 'success' : status === 'partial' ? 'partial' : 'error'
      const title = `${routine.name} ${status === 'success' ? 'ran' : status === 'partial' ? 'partly applied' : 'failed'}`
      this.update({ pending: null, feedback: { tone, title, details: tone !== 'success' && this.allowed('alerts') }, announcement: this.announce(`${title}.`) })
    } catch (cause) {
      const detail = routineErrorText(cause)
      this.update({ pending: null, feedback: { tone: 'error', title: `${routine.name} could not run`, detail, details: false }, announcement: this.announce(`${routine.name} could not run. ${detail}`) })
    }
    return true
  }

  dismissFeedback() {
    this.update({ feedback: null })
  }
}

/** Bound UI waiting; never retry or replay a control command after a transport timeout. */
function bounded<T>(promise: Promise<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Request timed out')), 15_000)
    promise.then(resolve, reject).finally(() => clearTimeout(timer))
  })
}

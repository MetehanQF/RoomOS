import { actionGuard, isNewerRoutine, type ActionGuard, type ExecutionIndex } from './routines-state'
import type { ActivityCatalog, Automation, AutomationCatalog, AutomationDraft, AutomationExecutionResult, AutomationHistoryPage, AutomationHistoryRecord, ConnectionStatus, Health, HealthComponent, HealthReport, Routine, RoutineCatalog, RoutineDraft, RoutineRunResponse, RoomOSEvent } from './types'

/**
 * Event-driven Routines data. One bounded bootstrap (routines + the execution, health and
 * control data this device is allowed to read), then routine.* WebSocket payloads update local
 * state. Unrelated events (system metrics, lights, cameras) are ignored. When an event refers to
 * an execution whose details are not loaded, a single coalesced Activity read fetches them.
 * Authoritative reloads happen only on (re)connect and return to foreground via the lifecycle.
 */
export interface RoutinesApi {
  routines: () => Promise<RoutineCatalog>
  health: () => Promise<Health>
  systemHealth: () => Promise<HealthReport>
  executions: () => Promise<ActivityCatalog>
  run: (id: string) => Promise<RoutineRunResponse>
  update: (id: string, body: Partial<RoutineDraft>) => Promise<Routine>
  create: (body: RoutineDraft) => Promise<Routine>
  remove: (id: string) => Promise<void>
  automations: () => Promise<AutomationCatalog>
  automationHistory: (limit: number, cursor: string | null) => Promise<AutomationHistoryPage>
  createAutomation: (body: AutomationDraft) => Promise<Automation>
  updateAutomation: (id: string, body: Partial<Omit<AutomationDraft, 'enabled'>>) => Promise<Automation>
  deleteAutomation: (id: string) => Promise<void>
  enableAutomation: (id: string, enabled: boolean) => Promise<Automation>
  simulateAutomation: (id: string) => Promise<AutomationExecutionResult>
  runAutomation: (id: string) => Promise<AutomationExecutionResult>
}

export type Availability = 'loading' | 'ready' | 'error' | 'forbidden'
export type Feedback = { tone: 'pending' | 'success' | 'partial' | 'error'; title: string; detail?: string }
export type HaState = 'connected' | 'disconnected' | 'unknown'

export type RoutinesSnapshot = {
  catalog: RoutineCatalog | null
  catalogStatus: Availability
  executions: ExecutionIndex
  deviceNames: Record<string, string>
  executionsStatus: Availability
  executionStorage: 'ok' | 'corrupt' | 'write_failed'
  control: Health | null
  controlStatus: Availability
  scheduler: HealthComponent | null
  schedulerStatus: Availability
  homeAssistant: HaState
  connection: ConnectionStatus
  trusted: boolean
  refreshing: boolean
  loadedAt: number | null
  pending: { routineId: string | null; kind: 'run' | 'toggle' | 'delete' | 'save' | 'simulate' | 'automation_toggle' | 'automation_delete' | 'automation_save' | 'automation_run' } | null
  feedback: { scope: string; value: Feedback } | null
  automations: AutomationCatalog | null
  automationsStatus: Availability
  automationHistory: AutomationHistoryRecord[]
  automationHistoryStatus: Availability
  automationHistoryCursor: string | null
  automationSimulation: AutomationExecutionResult | null
}

const REQUEST_TIMEOUT_MS = 15_000
export const EXECUTION_WINDOW = 100

export class RoutinesStore {
  private snapshot: RoutinesSnapshot
  private listeners = new Set<() => void>()
  private inflight: Promise<void> | null = null
  private epoch = 0
  private loadEpoch = -1
  private queued: RoomOSEvent[] = []
  private executionFetch: Promise<void> | null = null
  private wanted = new Set<string>()
  private wantSeq = 0
  private historyInflight: Promise<boolean> | null = null

  constructor(private api: RoutinesApi, private scopes: readonly string[], private authResolved: boolean, connection: ConnectionStatus = 'reconnecting', private now: () => number = Date.now) {
    const optional = (scope: string): Availability => scopes.includes(scope) ? 'loading' : 'forbidden'
    this.snapshot = {
      catalog: null, catalogStatus: scopes.includes('routines:read') ? 'loading' : 'forbidden',
      executions: new Map(), deviceNames: {}, executionsStatus: optional('activity:read'), executionStorage: 'ok',
      control: null, controlStatus: optional('routines:write'),
      scheduler: null, schedulerStatus: optional('system:read'),
      homeAssistant: 'unknown', connection, trusted: false, refreshing: false, loadedAt: null, pending: null, feedback: null,
      automations: null, automationsStatus: scopes.includes('routines:read') ? 'loading' : 'forbidden',
      automationHistory: [], automationHistoryStatus: scopes.includes('routines:read') ? 'loading' : 'forbidden', automationHistoryCursor: null,
      automationSimulation: null,
    }
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  getSnapshot = () => this.snapshot

  private update(change: Partial<RoutinesSnapshot>) {
    this.snapshot = { ...this.snapshot, ...change }
    this.listeners.forEach((listener) => listener())
  }

  private has(scope: string) { return this.scopes.includes(scope) }

  /** Single-flight authoritative load; a load started before a disconnect never re-trusts state. */
  load(): Promise<void> {
    if (!this.has('routines:read')) return Promise.resolve()
    if (this.inflight) return this.loadEpoch === this.epoch ? this.inflight : this.inflight.then(() => this.load())
    const epoch = this.epoch
    this.loadEpoch = epoch
    this.update({ refreshing: true, trusted: false })
    const current = () => epoch === this.epoch
    const tasks: Promise<unknown>[] = [
      bounded(this.api.routines()).then((catalog) => { if (current()) this.update({ catalog: this.mergeCatalog(catalog), catalogStatus: 'ready' }) },
        (cause) => { if (current()) this.update({ catalogStatus: isStatus(cause, 403) ? 'forbidden' : 'error' }) }),
      bounded(this.api.automations()).then((automations) => { if (current()) this.update({ automations: this.mergeAutomations(automations), automationsStatus: 'ready' }) },
        (cause) => { if (current()) this.update({ automationsStatus: isStatus(cause, 403) ? 'forbidden' : 'error' }) }),
      bounded(this.api.automationHistory(50, null)).then((page) => { if (current()) this.update({ automationHistory: page.items, automationHistoryCursor: page.next_cursor, automationHistoryStatus: 'ready' }) },
        (cause) => { if (current()) this.update({ automationHistoryStatus: isStatus(cause, 403) ? 'forbidden' : 'error' }) }),
    ]
    if (this.has('activity:read')) tasks.push(this.readExecutions(epoch))
    if (this.has('system:read')) tasks.push(bounded(this.api.systemHealth()).then((report) => {
      if (!current()) return
      const ha = report.components.find((item) => item.id === 'home_assistant')
      this.update({ scheduler: report.components.find((item) => item.id === 'automations') ?? null, schedulerStatus: 'ready',
        homeAssistant: this.snapshot.control ? this.snapshot.homeAssistant : ha ? (ha.status === 'healthy' ? 'connected' : 'disconnected') : this.snapshot.homeAssistant })
    }, (cause) => { if (current()) this.update({ schedulerStatus: isStatus(cause, 403) ? 'forbidden' : 'error' }) }))
    if (this.has('routines:write')) tasks.push(bounded(this.api.health()).then((control) => {
      if (current()) this.update({ control, controlStatus: 'ready', homeAssistant: control.home_assistant === 'connected' ? 'connected' : 'disconnected' })
    }, () => { if (current()) this.update({ controlStatus: 'error' }) }))
    const run = Promise.allSettled(tasks).then(() => {
      this.inflight = null
      const events = this.queued.splice(0)
      this.update({ refreshing: false, loadedAt: this.now(), trusted: current() && this.snapshot.catalogStatus === 'ready' && this.snapshot.connection === 'connected' })
      if (current()) events.forEach((event) => this.handleEvent(event))
    })
    this.inflight = run
    return run
  }

  /** Keep a routine already updated by a newer event when an older snapshot response arrives. */
  private mergeCatalog(next: RoutineCatalog): RoutineCatalog {
    const previous = this.snapshot.catalog
    if (!previous) return next
    return { ...next, routines: next.routines.map((routine) => {
      const known = previous.routines.find((item) => item.id === routine.id)
      return known && !isNewerRoutine(routine, known) ? known : routine
    }) }
  }

  private mergeAutomations(next: AutomationCatalog): AutomationCatalog {
    const previous = this.snapshot.automations
    if (!previous) return next
    return { ...next, automations: next.automations.map((item) => {
      const known = previous.automations.find((candidate) => candidate.id === item.id)
      return known && Date.parse(known.updated_at) > Date.parse(item.updated_at) ? known : item
    }) }
  }

  private readExecutions(epoch = this.epoch): Promise<void> {
    return bounded(this.api.executions()).then((catalog) => {
      if (epoch !== this.epoch) return
      const executions: ExecutionIndex = new Map()
      const deviceNames: Record<string, string> = {}
      for (const item of catalog.activity) {
        if (!item.execution) continue
        executions.set(item.execution.id, item.execution)
        Object.assign(deviceNames, item.device_names ?? {})
      }
      this.update({ executions, deviceNames, executionsStatus: 'ready', executionStorage: catalog.execution_storage_status ?? 'ok' })
    }, (cause) => { if (epoch === this.epoch) this.update({ executionsStatus: isStatus(cause, 403) ? 'forbidden' : 'error' }) })
  }

  /**
   * Targeted refresh for an execution referenced by an event but not yet loaded. Coalesced:
   * concurrent wants share one request; one follow-up runs only if a new want arrived after the
   * request started and is still unresolved. Ids missing after that are left unresolved.
   */
  private want(executionId: string | null | undefined) {
    if (!executionId || !this.has('activity:read') || this.snapshot.executions.has(executionId)) return
    this.wanted.add(executionId)
    this.wantSeq += 1
    if (this.executionFetch) return
    const fetchOnce = (): Promise<void> => {
      const started = this.wantSeq
      return this.readExecutions().then(() => {
        for (const id of [...this.wanted]) if (this.snapshot.executions.has(id)) this.wanted.delete(id)
        if (this.wanted.size && this.wantSeq > started) return fetchOnce()
        this.wanted.clear()
      })
    }
    this.executionFetch = fetchOnce().finally(() => { this.executionFetch = null })
  }

  setConnection(connection: ConnectionStatus) {
    if (connection === this.snapshot.connection) return
    this.epoch += 1
    this.update({ connection, trusted: false })
  }

  guard(routine: Routine | null, kind: 'run' | 'write'): ActionGuard {
    const s = this.snapshot
    return actionGuard({ routine, catalog: s.catalog, scopes: this.scopes, authResolved: this.authResolved,
      control: s.control && { ...s.control, home_assistant: s.homeAssistant === 'connected' ? 'connected' : 'disconnected' },
      connection: s.connection, trusted: s.trusted && !s.refreshing, pending: s.pending !== null, kind })
  }

  handleEvent(event: RoomOSEvent) {
    if (this.snapshot.refreshing) { this.queued.push(event); return }
    const catalog = this.snapshot.catalog
    const automations = this.snapshot.automations
    if (event.type === 'automation.changed' || event.type === 'automation.executed') {
      const next = event.data.automation
      if (next && automations) {
        const current = automations.automations.find((item) => item.id === next.id)
        if (!current || Date.parse(next.updated_at) >= Date.parse(current.updated_at)) {
          const items = current ? automations.automations.map((item) => item.id === next.id ? next : item) : [...automations.automations, next]
          this.update({ automations: { ...automations, automations: items } })
        }
      }
      const result = event.data.result
      if (event.type === 'automation.executed' && result && next && !this.snapshot.automationHistory.some((item) => item.correlation_id === result.correlation_id)) {
        const record: AutomationHistoryRecord = {
          timestamp: event.timestamp, automation_id: next.id, automation_name: next.name, outcome: result.outcome,
          trigger_source: 'event', trigger_reason: result.reason ?? 'executed', reason: result.reason,
          correlation_id: result.correlation_id, duration_ms: result.duration_ms,
          error: result.actions.find((action) => action.status === 'failed')?.detail ?? null,
        }
        this.update({ automationHistory: [record, ...this.snapshot.automationHistory].slice(0, 500) })
      }
      return
    }
    if (event.type === 'automation.deleted') {
      const id = event.data.automation_id
      if (id && automations) this.update({ automations: { ...automations, automations: automations.automations.filter((item) => item.id !== id) } })
      return
    }
    if (event.type === 'routine.diagnostics') {
      if (catalog && this.has('routines:read') && event.data.diagnostics) {
        this.update({ catalog: { ...catalog, diagnostics: event.data.diagnostics } })
      }
      return
    }
    if (event.type === 'routine.changed' || event.type === 'routine.run_completed') {
      const next = event.data.routine
      if (!next || !catalog) return
      const current = catalog.routines.find((item) => item.id === next.id)
      if (!isNewerRoutine(next, current)) return // delayed/older event
      const routines = current ? catalog.routines.map((item) => item.id === next.id ? next : item) : [...catalog.routines, next]
      this.update({ catalog: { ...catalog, routines } })
      this.want(next.last_result?.execution_id)
      return
    }
    if (event.type === 'routine.deleted') {
      const id = event.data.routine_id
      if (!id || !catalog) return
      this.update({ catalog: { ...catalog, routines: catalog.routines.filter((item) => item.id !== id) } })
      return
    }
    if (event.type === 'activity.recorded') {
      const activity = event.data.activity as { type?: string; execution_id?: string | null; source?: string } | undefined
      if (activity?.source === 'routine' && activity.type?.startsWith('scene_')) this.want(activity.execution_id)
      return
    }
    if (event.type === 'roomos.connection') {
      const ha = event.data.home_assistant
      if (ha === 'connected' || ha === 'disconnected') this.update({ homeAssistant: ha, control: this.snapshot.control && { ...this.snapshot.control, home_assistant: ha } })
    }
    // system.*, light.*, camera.*, scene.* and alert events do not change routine state.
  }

  private apply(routine: Routine) {
    const catalog = this.snapshot.catalog
    if (!catalog) return
    const current = catalog.routines.find((item) => item.id === routine.id)
    if (!isNewerRoutine(routine, current)) return
    this.update({ catalog: { ...catalog, routines: current ? catalog.routines.map((item) => item.id === routine.id ? routine : item) : [...catalog.routines, routine] } })
  }

  private applyAutomation(next: Automation) {
    const catalog = this.snapshot.automations
    if (!catalog) return
    const current = catalog.automations.find((item) => item.id === next.id)
    if (current && Date.parse(current.updated_at) > Date.parse(next.updated_at)) return
    this.update({ automations: { ...catalog, automations: current ? catalog.automations.map((item) => item.id === next.id ? next : item) : [...catalog.automations, next] } })
  }

  private finish(scope: string, value: Feedback) {
    this.update({ pending: null, feedback: { scope, value } })
  }

  private async act<T>(routine: Routine | null, kind: 'run' | 'toggle' | 'delete' | 'save', title: string, send: () => Promise<T>, done: (result: T) => Feedback) {
    const scope = routine?.id ?? 'page'
    const current = routine ? this.snapshot.catalog?.routines.find((item) => item.id === routine.id) ?? null : null
    if (routine && !current) return false
    if (!this.guard(current, kind === 'run' ? 'run' : 'write').ok) return false
    this.update({ pending: { routineId: routine?.id ?? null, kind }, feedback: { scope, value: { tone: 'pending', title } } })
    try {
      this.finish(scope, done(await bounded(send())))
    } catch (cause) {
      this.finish(scope, { tone: 'error', title: 'Action failed', detail: routineErrorText(cause) })
    }
    return true
  }

  run(routine: Routine) {
    return this.act(routine, 'run', `Running ${routine.name}…`, () => this.api.run(routine.id), (result) => {
      this.apply(result.routine)
      this.want(result.scene.execution_id ?? result.routine.last_result?.execution_id)
      const status = result.scene.status
      return { tone: status === 'success' ? 'success' : status === 'partial' ? 'partial' : 'error', title: `${routine.name}: ${status === 'success' ? 'applied' : status === 'partial' ? 'partly applied' : 'failed'}` }
    })
  }

  toggle(routine: Routine) {
    return this.act(routine, 'toggle', 'Updating routine…', () => this.api.update(routine.id, { enabled: !routine.enabled }), (updated) => {
      this.apply(updated)
      return { tone: 'success', title: updated.enabled ? `${updated.name} turned on` : `${updated.name} turned off` }
    })
  }

  remove(routine: Routine) {
    return this.act(routine, 'delete', 'Deleting routine…', () => this.api.remove(routine.id), () => {
      const catalog = this.snapshot.catalog
      if (catalog) this.update({ catalog: { ...catalog, routines: catalog.routines.filter((item) => item.id !== routine.id) } })
      return { tone: 'success', title: `${routine.name} deleted` }
    })
  }

  save(draft: RoutineDraft, existing: Routine | null) {
    return this.act(existing, 'save', existing ? 'Saving changes…' : 'Creating routine…',
      () => existing ? this.api.update(existing.id, draft) : this.api.create(draft),
      (routine) => { this.apply(routine); return { tone: 'success', title: existing ? 'Routine updated' : 'Routine created' } })
  }

  dismiss() { this.update({ feedback: null }) }

  automationGuard(item: Automation | null, kind: 'write' | 'simulate' | 'toggle' | 'run'): ActionGuard {
    const catalog = this.snapshot.automations
    const hidden = { visible: false, ok: false, reason: null }
    if (!this.authResolved || !catalog || !this.has('routines:write') || !catalog.can_write) return hidden
    if (kind === 'run' && !this.has('lights:write')) return hidden
    const blocked = (reason: string): ActionGuard => ({ visible: true, ok: false, reason })
    if (catalog.storage_status !== 'ok') return blocked('Automation storage needs repair.')
    if (this.snapshot.connection !== 'connected') return blocked(this.snapshot.connection === 'offline' ? 'Offline · actions unavailable.' : 'Reconnecting · actions paused.')
    if (!this.snapshot.trusted || this.snapshot.refreshing) return blocked('Refreshing · actions paused.')
    if (this.snapshot.pending) return blocked('Another action is in progress.')
    const needsLiveControl = kind === 'run' || kind === 'toggle' && item?.enabled === false
    if (needsLiveControl) {
      if (!catalog.can_enable || !this.has('lights:write')) return blocked('This device cannot enable live actions.')
      if (!this.snapshot.control?.control_enabled) return blocked('Controls are turned off on the server.')
      if (kind === 'run' && this.snapshot.homeAssistant !== 'connected') return blocked('Home Assistant is unavailable.')
    }
    return { visible: true, ok: true, reason: null }
  }

  loadMoreAutomationHistory(): Promise<boolean> {
    const cursor = this.snapshot.automationHistoryCursor
    if (!cursor || this.historyInflight) return this.historyInflight ?? Promise.resolve(false)
    this.update({ automationHistoryStatus: 'loading' })
    const run = bounded(this.api.automationHistory(50, cursor)).then((page) => {
      const seen = new Set(this.snapshot.automationHistory.map((item) => item.correlation_id))
      this.update({ automationHistory: [...this.snapshot.automationHistory, ...page.items.filter((item) => !seen.has(item.correlation_id))], automationHistoryCursor: page.next_cursor, automationHistoryStatus: 'ready' })
      return true
    }, () => { this.update({ automationHistoryStatus: 'error' }); return false }).finally(() => { this.historyInflight = null })
    this.historyInflight = run
    return run
  }

  simulateAutomation(item: Automation): Promise<boolean> {
    if (!this.automationGuard(item, 'simulate').ok) return Promise.resolve(false)
    this.update({ pending: { routineId: item.id, kind: 'simulate' }, automationSimulation: null, feedback: { scope: item.id, value: { tone: 'pending', title: `Simulating ${item.name}…` } } })
    return bounded(this.api.simulateAutomation(item.id)).then((result) => {
      this.update({ pending: null, automationSimulation: result, feedback: null })
      return true
    }, (cause) => {
      this.update({ pending: null, feedback: { scope: item.id, value: { tone: 'error', title: 'Simulation failed', detail: routineErrorText(cause) } } })
      return true
    })
  }

  saveAutomation(draft: AutomationDraft, existing: Automation | null): Promise<boolean> {
    if (!this.automationGuard(existing, 'write').ok) return Promise.resolve(false)
    const body: AutomationDraft = {
      name: draft.name, description: draft.description, enabled: false, trigger: draft.trigger, conditions: draft.conditions,
      actions: draft.actions, cooldown_seconds: draft.cooldown_seconds, failure_policy: draft.failure_policy,
    }
    this.update({ pending: { routineId: existing?.id ?? null, kind: 'automation_save' }, feedback: null })
    const update = {
      name: body.name, description: body.description, trigger: body.trigger, conditions: body.conditions,
      actions: body.actions, cooldown_seconds: body.cooldown_seconds, failure_policy: body.failure_policy,
    }
    const request = existing
      ? this.api.updateAutomation(existing.id, update)
      : this.api.createAutomation(body)
    return bounded(request).then((item) => {
      this.applyAutomation(item)
      this.finish(existing?.id ?? 'page', { tone: 'success', title: existing ? 'Automation updated' : 'Automation created disabled' })
      return true
    }, (cause) => {
      this.finish(existing?.id ?? 'page', { tone: 'error', title: 'Automation could not be saved', detail: routineErrorText(cause) })
      return true
    })
  }

  toggleAutomation(item: Automation): Promise<boolean> {
    if (!this.automationGuard(item, 'toggle').ok) return Promise.resolve(false)
    this.update({ pending: { routineId: item.id, kind: 'automation_toggle' }, feedback: null })
    return bounded(this.api.enableAutomation(item.id, !item.enabled)).then((next) => {
      this.applyAutomation(next)
      this.finish(item.id, { tone: 'success', title: next.enabled ? `${next.name} turned on` : `${next.name} turned off` })
      return true
    }, (cause) => { this.finish(item.id, { tone: 'error', title: 'Automation could not be changed', detail: routineErrorText(cause) }); return true })
  }

  deleteAutomation(item: Automation): Promise<boolean> {
    if (!this.automationGuard(item, 'write').ok) return Promise.resolve(false)
    this.update({ pending: { routineId: item.id, kind: 'automation_delete' }, feedback: null })
    return bounded(this.api.deleteAutomation(item.id)).then(() => {
      const catalog = this.snapshot.automations
      if (catalog) this.update({ automations: { ...catalog, automations: catalog.automations.filter((candidate) => candidate.id !== item.id) } })
      this.finish('page', { tone: 'success', title: `${item.name} deleted` })
      return true
    }, (cause) => { this.finish(item.id, { tone: 'error', title: 'Automation could not be deleted', detail: routineErrorText(cause) }); return true })
  }

  runAutomation(item: Automation): Promise<boolean> {
    if (!this.automationGuard(item, 'run').ok) return Promise.resolve(false)
    this.update({ pending: { routineId: item.id, kind: 'automation_run' }, feedback: null })
    return bounded(this.api.runAutomation(item.id)).then((result) => {
      const tone = result.outcome === 'success' ? 'success' : result.outcome === 'partial' ? 'partial' : 'error'
      this.finish(item.id, { tone, title: `${item.name}: ${result.outcome}` })
      return true
    }, (cause) => { this.finish(item.id, { tone: 'error', title: 'Automation could not run', detail: routineErrorText(cause) }); return true })
  }
}

function isStatus(cause: unknown, status: number) {
  return typeof cause === 'object' && cause !== null && (cause as { status?: unknown }).status === status
}

export function routineErrorText(cause: unknown) {
  const status = typeof cause === 'object' && cause !== null ? (cause as { status?: unknown }).status : undefined
  if (status === 409) return 'Managed by RoomOS or in conflict with current state.'
  if (status === 429) return 'This routine is already running.'
  if (status === 403) return 'Not allowed on this device, or controls are turned off.'
  if (status === 422) return 'The routine settings were rejected.'
  if (status === 503) return 'RoomOS cannot reach Home Assistant right now.'
  if (cause instanceof Error && (cause.message.startsWith('RoomOS is offline') || cause.message === 'Request timed out')) return cause.message
  return 'Please try again.'
}

/** Bound waiting; never retry or replay a write after a timeout. */
function bounded<T>(promise: Promise<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Request timed out')), REQUEST_TIMEOUT_MS)
    promise.then(resolve, reject).finally(() => clearTimeout(timer))
  })
}

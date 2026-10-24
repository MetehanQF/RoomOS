import { observationTime } from './lights-state'
import type { AutomationAction, AutomationDraft, AutomationExecutionResult, ConnectionStatus, ExecutionRecord, Health, HealthComponent, Routine, RoutineCatalog, RoutineTrigger, SceneSummary } from './types'

/**
 * Pure presentation rules for Routines. Every statement shown to the user must be backed by
 * persisted Core data: no inferred skip reasons, no fabricated next runs, and executions are
 * associated with a routine only through routine IDs and execution IDs — never scene names.
 */

export const WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const
type Weekday = typeof WEEKDAYS[number]
const DAY_LABEL: Record<Weekday, string> = { mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun' }

export function timezoneLabel(timezone: string) {
  return timezone === 'Europe/Istanbul' ? 'Istanbul time' : `${timezone} time`
}

function dayPhrase(days: readonly Weekday[]) {
  const set = new Set(days)
  if (set.size === 7) return 'Every day'
  if (set.size === 5 && ['mon', 'tue', 'wed', 'thu', 'fri'].every((day) => set.has(day as Weekday))) return 'Weekdays'
  if (set.size === 2 && set.has('sat') && set.has('sun')) return 'Weekends'
  return WEEKDAYS.filter((day) => set.has(day)).map((day) => DAY_LABEL[day]).join(', ')
}

/** Short, card-level description. Never exposes raw trigger or HA identifiers. */
export function triggerLines(trigger: RoutineTrigger): string[] {
  if (trigger.type === 'evening_home') return ["At sunset, if you're home", 'After arriving home, until 02:00']
  if (trigger.type === 'time') return [`${dayPhrase(trigger.days)} at ${trigger.time}`]
  if (trigger.type === 'manual') return ['Only when run by hand']
  return ['When a connected device changes state']
}

/** Full rules for the details section, matching Core scheduler semantics. */
export function triggerRules(trigger: RoutineTrigger, timezone: string): string[] {
  if (trigger.type === 'evening_home') return [
    "At sunset, if you're home at that moment.",
    "If you're away at sunset: when you arrive home after sunset, until 02:00.",
    'Each arrival before 02:00 can run it again; there is no once-per-evening limit.',
    'Never between 02:00 and sunrise, and never just because RoomOS started.',
    'Sunsets or arrivals missed while RoomOS or Home Assistant is offline are not made up later.',
  ]
  if (trigger.type === 'time') return [
    `${dayPhrase(trigger.days)} at ${trigger.time} (${timezoneLabel(timezone)}).`,
    'Usually starts within a few seconds of the scheduled minute.',
    'A time missed while RoomOS is offline is not made up later.',
  ]
  if (trigger.type === 'manual') return ['Runs only when started by hand from an authorised device.']
  return ['Runs when a connected device reports the configured state.']
}

export function triggerIcon(trigger: RoutineTrigger) {
  return trigger.type === 'evening_home' ? 'moon' as const : trigger.type === 'time' ? 'clock' as const : trigger.type === 'manual' ? 'play' as const : 'zap' as const
}

type ZonedParts = { year: number; month: number; day: number; hour: number; minute: number; second: number }

function zonedParts(instant: number, timezone: string): ZonedParts {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone: timezone, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric',
  }).formatToParts(new Date(instant)).filter((part) => part.type !== 'literal').map((part) => [part.type, Number(part.value)]))
  return parts as unknown as ZonedParts
}

function offsetAt(instant: number, timezone: string) {
  const p = zonedParts(instant, timezone)
  return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - Math.floor(instant / 1000) * 1000
}

/** Convert a wall-clock time in `timezone` to an instant (two-pass for offset changes). */
export function zonedInstant(year: number, month: number, day: number, hour: number, minute: number, timezone: string) {
  const wall = Date.UTC(year, month - 1, day, hour, minute)
  let guess = wall - offsetAt(wall, timezone)
  guess = wall - offsetAt(guess, timezone)
  return guess
}

/**
 * Next scheduled evaluation of a time trigger, strictly after `now`. A scheduled time is not a
 * confirmed execution: Core must be running in that minute and control must be enabled.
 * Returns null for non-time triggers, disabled routines or invalid data.
 */
export function nextScheduled(routine: Routine, timezone: string, now: number): Date | null {
  const trigger = routine.trigger
  if (!routine.enabled || trigger.type !== 'time') return null
  const match = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(trigger.time)
  if (!match || trigger.days.length === 0) return null
  const today = zonedParts(now, timezone)
  for (let offset = 0; offset <= 7; offset += 1) {
    const civil = new Date(Date.UTC(today.year, today.month - 1, today.day + offset))
    const weekday = WEEKDAYS[(civil.getUTCDay() + 6) % 7]
    if (!trigger.days.includes(weekday)) continue
    const instant = zonedInstant(civil.getUTCFullYear(), civil.getUTCMonth() + 1, civil.getUTCDate(), Number(match[1]), Number(match[2]), timezone)
    if (instant > now) return new Date(instant)
  }
  return null
}

export function formatDateTime(value: string | Date, timezone: string) {
  return new Intl.DateTimeFormat('en-GB', { timeZone: timezone, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date(value))
}

export function formatTime(value: string | Date, timezone: string) {
  return new Intl.DateTimeFormat('en-GB', { timeZone: timezone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date(value))
}

/** "Today 02:00", "Tomorrow 02:00" or "Sat 17 Oct, 02:00", relative to local dates in `timezone`. */
export function relativeSchedule(target: Date, timezone: string, now: number) {
  const a = zonedParts(now, timezone), b = zonedParts(target.getTime(), timezone)
  const days = Math.round((Date.UTC(b.year, b.month - 1, b.day) - Date.UTC(a.year, a.month - 1, a.day)) / 86_400_000)
  const time = formatTime(target, timezone)
  if (days === 0) return `Today ${time}`
  if (days === 1) return `Tomorrow ${time}`
  return `${new Intl.DateTimeFormat('en-GB', { timeZone: timezone, weekday: 'short', day: 'numeric', month: 'short' }).format(target)}, ${time}`
}

export type NextLine = { kind: 'scheduled'; text: string; at: Date } | { kind: 'conditional' | 'manual' | 'off' | 'unknown'; text: string }

export function nextLine(routine: Routine, timezone: string, now: number): NextLine {
  if (!routine.enabled) return { kind: 'off', text: "Won't run while off" }
  if (routine.trigger.type === 'evening_home') return { kind: 'conditional', text: 'Depends on sunset and arrival · no fixed time' }
  if (routine.trigger.type === 'manual') return { kind: 'manual', text: 'Only when run by hand' }
  const at = nextScheduled(routine, timezone, now)
  return at ? { kind: 'scheduled', text: `Scheduled ${relativeSchedule(at, timezone, now)}`, at } : { kind: 'unknown', text: 'Next time unavailable' }
}

/* ---------- execution correlation ---------- */

export type ExecutionIndex = Map<string, ExecutionRecord>
export type ResultTone = 'success' | 'partial' | 'failed' | 'rejected' | 'running' | 'none' | 'interrupted'

const REASON: Record<string, string> = {
  control_disabled: 'control was turned off', duplicate: 'already running', policy_rejected: 'not allowed',
  not_configured: 'scene not configured', invalid_action: 'invalid action', internal_error: 'internal error', interrupted: 'RoomOS restarted',
  scene_activation_failed: 'scene could not be applied',
}

export function reasonText(reason: string | null | undefined) {
  return reason ? REASON[reason] ?? 'unknown reason' : null
}

/** Executions explicitly attributed to this routine by Core, newest first. */
export function routineExecutions(routine: Routine, index: ExecutionIndex): ExecutionRecord[] {
  return [...index.values()]
    .filter((record) => record.source === 'routine' && record.routine_id === routine.id)
    .sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at))
}

export type LastRun = {
  tone: ResultTone
  label: string
  at: string | null
  execution: ExecutionRecord | null
  /** Why target details are absent, when the routine has a run but no resolvable execution. */
  detailNote: string | null
}

export function resultLabel(result: ExecutionRecord['result'], reason: string | null) {
  if (result === 'success') return { tone: 'success' as const, label: 'Succeeded' }
  if (result === 'partial') return { tone: 'partial' as const, label: 'Partly applied' }
  if (result === 'rejected') return { tone: 'rejected' as const, label: `Not run · ${reasonText(reason) ?? 'refused'}` }
  if (result === 'interrupted') return { tone: 'interrupted' as const, label: 'Interrupted · outcome unknown' }
  if (result === 'running') return { tone: 'running' as const, label: 'Running…' }
  return { tone: 'failed' as const, label: reason ? `Failed · ${reasonText(reason)}` : 'Failed' }
}

/**
 * The routine's last recorded run. Primary source: last_result.execution_id. Without it, only
 * the routine's own persisted result is shown — never a guess from scene-matching history.
 */
export function lastRun(routine: Routine, index: ExecutionIndex, details: 'ready' | 'unavailable' | 'forbidden' | 'loading'): LastRun {
  const result = routine.last_result
  if (!result || !routine.last_run) return { tone: 'none', label: 'No recorded execution', at: null, execution: null, detailNote: null }
  const execution = result.execution_id ? index.get(result.execution_id) ?? null : null
  if (execution && execution.routine_id === routine.id) {
    const described = resultLabel(execution.result, execution.reason)
    return { ...described, at: execution.started_at, execution, detailNote: null }
  }
  // Core stores refusals as last_result.status="failed" with a detail code.
  const refused = result.detail === 'control_disabled'
  const described = refused ? { tone: 'rejected' as const, label: `Not run · ${reasonText(result.detail)}` } : resultLabel(result.status, result.detail)
  const detailNote = !result.execution_id ? 'Target details were not recorded for this run.'
    : details === 'forbidden' ? 'Run details are not shared with this device.'
      : details === 'ready' ? 'Run details are no longer retained.'
        : details === 'loading' ? 'Loading run details…' : 'Run details are unavailable right now.'
  return { ...described, at: routine.last_run, execution: null, detailNote }
}

export function targetCounts(execution: ExecutionRecord) {
  const count = (result: 'applied' | 'skipped' | 'failed') => execution.targets.filter((target) => target.result === result).length
  return { applied: count('applied'), skipped: count('skipped'), failed: count('failed'), total: execution.targets.length }
}

export function countsText(execution: ExecutionRecord) {
  const c = targetCounts(execution)
  if (!c.total) return 'No light outcomes recorded'
  const parts = [`${c.applied} of ${c.total} lights applied`]
  if (c.failed) parts.push(`${c.failed} failed`)
  if (c.skipped) parts.push(`${c.skipped} skipped`)
  return parts.join(' · ')
}

export function durationText(ms: number | null) {
  if (ms === null) return 'In progress'
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
}

export function triggerSourceText(execution: ExecutionRecord) {
  if (execution.trigger === 'schedule') return 'Scheduled time'
  if (execution.trigger === 'ha_state') return 'Sunset or arrival'
  return 'Run by hand'
}

/** Neutral "not run" wording for conditional routines: skip decisions are not recorded. */
export function quietNote(routine: Routine, timezone: string): string | null {
  if (routine.trigger.type !== 'evening_home' || !routine.enabled) return null
  return routine.last_run
    ? `No execution recorded since ${formatDateTime(routine.last_run, timezone)}. RoomOS does not record evaluations that end without a run, so a reason is not available.`
    : 'No execution recorded yet. RoomOS does not record evaluations that end without a run.'
}

/* ---------- scheduler status ---------- */

export type SchedulerState = 'healthy' | 'degraded' | 'unavailable' | 'unknown'
export type SchedulerView = { state: SchedulerState; label: string; summary: string; checkedAt: string | null }

export function schedulerView(component: HealthComponent | null, availability: 'ready' | 'forbidden' | 'error' | 'loading', storageCorrupt: boolean): SchedulerView {
  if (storageCorrupt) return { state: 'unavailable', label: 'Unavailable', summary: 'Automation storage needs repair; nothing will run.', checkedAt: component?.checked_at ?? null }
  if (availability === 'forbidden') return { state: 'unknown', label: 'Unknown', summary: 'Scheduler status is not shared with this device.', checkedAt: null }
  if (availability === 'loading') return { state: 'unknown', label: 'Checking…', summary: 'Reading scheduler status…', checkedAt: null }
  if (availability === 'error' || !component) return { state: 'unknown', label: 'Unknown', summary: 'Scheduler status could not be read.', checkedAt: null }
  if (component.status === 'healthy') return { state: 'healthy', label: 'Running', summary: component.summary, checkedAt: component.checked_at }
  if (component.status === 'degraded') return { state: 'degraded', label: 'Degraded', summary: component.summary, checkedAt: component.checked_at }
  if (component.status === 'unhealthy') return { state: 'unavailable', label: 'Not running', summary: component.summary, checkedAt: component.checked_at }
  return { state: 'unknown', label: 'Unknown', summary: component.summary, checkedAt: component.checked_at }
}

/* ---------- permissions ---------- */

export type ActionGuard = { visible: boolean; ok: boolean; reason: string | null }

/**
 * Fail-closed guard for user-routine actions. Managed routines and devices without the scope
 * get no controls at all; transient conditions keep controls visible but disabled with a reason.
 */
export function actionGuard(input: {
  routine: Routine | null; catalog: RoutineCatalog | null; scopes: readonly string[]; authResolved: boolean
  control: Health | null; connection: ConnectionStatus; trusted: boolean; pending: boolean; kind: 'run' | 'write'
}): ActionGuard {
  const { routine, catalog, scopes, authResolved, control, connection, trusted, pending, kind } = input
  const hidden = { visible: false, ok: false, reason: null }
  if (!authResolved || !catalog || routine?.managed) return hidden
  const scoped = kind === 'run' ? catalog.can_run && scopes.includes('routines:write') && scopes.includes('lights:write') : catalog.can_write && scopes.includes('routines:write')
  if (!scoped) return hidden
  const blocked = (reason: string) => ({ visible: true, ok: false, reason })
  if (catalog.storage_status !== 'ok') return blocked('Automation storage needs repair.')
  if (!control) return blocked('Waiting for RoomOS…')
  if (!control.control_enabled) return blocked('Controls are turned off on the server.')
  if (connection === 'offline') return blocked('Offline · actions unavailable.')
  if (connection !== 'connected') return blocked('Reconnecting · actions paused.')
  if (!trusted) return blocked('Refreshing · actions paused.')
  if (kind === 'run' && control.home_assistant !== 'connected') return blocked('Home Assistant is unavailable.')
  if (pending) return blocked('Another action is in progress.')
  return { visible: true, ok: true, reason: null }
}

/** Newer-or-equal routine state wins; delayed older events never overwrite. */
export function isNewerRoutine(next: Routine, current: Routine | undefined) {
  if (!current) return true
  const a = observationTime(current.updated_at), b = observationTime(next.updated_at)
  if (b === null) return false
  return a === null || b >= a
}


/** Diagnostic labels never derive a decision from absence of execution. */
export function evaluationText(record: import('./types').RoutineEvaluation): string {
  if (record.completion_state !== 'complete' || record.outcome === null) return 'Outcome not recorded'
  const reasons: Record<string, string> = {
    presence_away: 'presence was away', presence_unknown: 'presence was unknown',
    solar_unknown: 'sunset information was unavailable', outside_window: 'outside the evening window',
    routine_disabled: 'routine was disabled', control_disabled: 'control was disabled',
    execution_rejected: 'execution was rejected', ha_unavailable: 'Home Assistant was unavailable',
    execution_failed: 'execution failed',
  }
  if (record.outcome === 'executed') return record.execution_result === 'partial' ? 'Executed · partial result' : 'Executed'
  const label = record.outcome === 'skipped' ? 'Skipped' : 'Failed'
  return `${label}: ${reasons[record.reason_code ?? ''] ?? 'reason unavailable'}`
}

export function originText(origin: import('./types').RoutineEvaluation['trigger_origin']): string {
  const labels: Record<string, string> = { sunset: 'at sunset', arrival: 'on arrival', schedule: 'on schedule', manual: 'manually' }
  return labels[origin ?? ''] ?? 'origin unknown'
}

/** Reporting recency captured when a presence-driven candidate was evaluated; never GPS age. */
export function reportingAtEvaluationText(record: import('./types').RoutineEvaluation): string | null {
  const labels: Record<string, string> = {
    fresh: 'Tracker reporting was recent', stale: 'No recent tracker update', unknown: 'Tracker reporting recency unknown',
  }
  return record.presence_reporting ? `${labels[record.presence_reporting]} at evaluation` : null
}

export function blankAutomationDraft(scene: SceneSummary['id'] = 'normal'): AutomationDraft {
  return {
    name: '', description: '', enabled: false,
    trigger: { type: 'time', time: '22:30', days: ['mon', 'tue', 'wed', 'thu', 'fri'] },
    conditions: null,
    actions: [{ type: 'scene', scene_id: scene }],
    cooldown_seconds: 0,
    failure_policy: 'stop_on_failure',
  }
}

export function validateAutomationDraft(draft: AutomationDraft): string | null {
  if (!draft.name.trim()) return 'Add an automation name.'
  if (draft.name.trim().length > 60) return 'Keep the name to 60 characters.'
  if (draft.description.trim().length > 240) return 'Keep the description to 240 characters.'
  if (draft.actions.length < 1) return 'Add at least one action.'
  if (draft.actions.length > 8) return 'Use no more than 8 actions.'
  if (draft.trigger.type === 'time') {
    if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(draft.trigger.time)) return 'Choose a valid time.'
    if (draft.trigger.days.length < 1) return 'Choose at least one day.'
  }
  if (draft.trigger.type === 'device_health' && !draft.trigger.device_id) return 'Choose a device.'
  return null
}

export function moveAutomationAction(draft: AutomationDraft, from: number, to: number): AutomationDraft {
  if (from < 0 || from >= draft.actions.length || to < 0 || to >= draft.actions.length || from === to) return draft
  const actions = [...draft.actions]
  const [action] = actions.splice(from, 1)
  actions.splice(to, 0, action)
  return { ...draft, actions }
}

const LIGHT_NAMES: Record<string, string> = {
  room_main: 'Main light', room_2: 'Room light 2', room_3: 'Room light 3', room_4: 'Room light 4',
  desk_ambient: 'Desk ambient', main_room_lights: 'Room lights',
}
const SCENE_NAMES: Record<SceneSummary['id'], string> = {
  normal: 'Normal', work_1: 'Work 1', work_2: 'Work 2', gaming_1: 'Gaming 1', gaming_2: 'Gaming 2', movie: 'Movie', night: 'Night', all_off: 'All off',
}

export function describeAutomationAction(action: AutomationAction): string {
  if (action.type === 'scene') return `Activate ${SCENE_NAMES[action.scene_id]}`
  if (action.type === 'routine') return 'Run a routine'
  const target = LIGHT_NAMES[action.target_id] ?? 'Light'
  if (action.operation === 'power') return `Turn ${target} ${action.on ? 'on' : 'off'}`
  if (action.operation === 'brightness') return `Set ${target} brightness to ${action.brightness}`
  if (action.operation === 'temperature') return `Set ${target} to ${action.kelvin} K`
  return `Set ${target} colour`
}

export function simulationLabel(result: AutomationExecutionResult): { tone: 'success' | 'partial' | 'error'; title: string; detail: string } {
  if (result.outcome === 'success') return { tone: 'success', title: 'Simulation · would run', detail: `${result.actions.length} action${result.actions.length === 1 ? '' : 's'} checked. No command was sent.` }
  if (result.reason === 'condition_unknown') return { tone: 'partial', title: 'Simulation · would not run', detail: 'Required evidence is unknown or stale. No command was sent.' }
  if (result.reason === 'condition_false') return { tone: 'partial', title: 'Simulation · would not run', detail: 'The conditions are false. No command was sent.' }
  return { tone: 'error', title: 'Simulation could not decide', detail: 'No command was sent.' }
}

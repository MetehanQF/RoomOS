import { useEffect, useId, useMemo, useState, useSyncExternalStore } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { useAuthStatus } from '../auth-context'
import { roomosLifecycle } from '../lifecycle'
import { CommandNotice, LoadingPanel, PageHeader, RealtimePill, StatusPill } from '../components/Common'
import { Icon } from '../components/Icons'
import { routineWeekdays, validateRoutineDraft } from '../routines-ui'
import {
  blankAutomationDraft, describeAutomationAction, evaluationText, moveAutomationAction, originText, reportingAtEvaluationText, countsText, durationText, formatDateTime, lastRun, nextLine, quietNote, resultLabel, routineExecutions, schedulerView, simulationLabel, validateAutomationDraft,
  timezoneLabel, triggerIcon, triggerLines, triggerRules, triggerSourceText, type ActionGuard, type ExecutionIndex, type SchedulerView,
} from '../routines-state'
import { EXECUTION_WINDOW, RoutinesStore, type Availability, type RoutinesApi, type RoutinesSnapshot } from '../routines-store'
import type { Automation, AutomationAction, AutomationCatalog, AutomationDraft, AutomationExecutionResult, AutomationHistoryRecord, AutomationPredicate, ExecutionRecord, Routine, RoutineCatalog, RoutineDraft, RoutineTrigger, SceneSummary } from '../types'

const routinesApi: RoutinesApi = {
  routines: api.routines,
  health: api.health,
  systemHealth: api.systemHealth,
  executions: () => api.activity('scenes', EXECUTION_WINDOW, 0),
  run: api.runRoutine,
  update: api.updateRoutine,
  create: api.createRoutine,
  remove: api.deleteRoutine,
  automations: api.automations,
  automationHistory: api.automationHistory,
  createAutomation: api.createAutomation,
  updateAutomation: api.updateAutomation,
  deleteAutomation: api.deleteAutomation,
  enableAutomation: api.enableAutomation,
  simulateAutomation: api.simulateAutomation,
  runAutomation: api.runAutomation,
}

const blankDraft = (scene: SceneSummary['id'] = 'normal'): RoutineDraft => ({
  name: '', enabled: false, trigger: { type: 'manual' },
  action: { type: 'activate_scene', scene_id: scene },
})

/** Existing user-routine editor (Manual/Time only); not a new builder. */
export function RoutineForm({ catalog, initial, onSave, onCancel, controlsAvailable = true }: {
  catalog: RoutineCatalog
  initial?: Routine
  onSave: (draft: RoutineDraft) => Promise<void>
  onCancel: () => void
  controlsAvailable?: boolean
}) {
  const [draft, setDraft] = useState<RoutineDraft>(() => initial ? {
    name: initial.name, enabled: initial.enabled, trigger: initial.trigger, action: initial.action,
  } : blankDraft(catalog.scenes[0]?.id))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const triggerType = draft.trigger.type
  const save = async () => {
    const validation = validateRoutineDraft(draft)
    if (validation) { setError(validation); return }
    setBusy(true); setError(null)
    try { await onSave({ ...draft, name: draft.name.trim() }) }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Routine could not be saved.') }
    finally { setBusy(false) }
  }
  const setTriggerType = (type: 'manual' | 'time') => setDraft((current) => ({
    ...current,
    trigger: type === 'manual' ? { type: 'manual' } : { type: 'time', time: '22:30', days: ['mon', 'tue', 'wed', 'thu', 'fri'] },
  }))
  const toggleDay = (day: typeof routineWeekdays[number][0]) => setDraft((current) => {
    if (current.trigger.type !== 'time') return current
    const selected = current.trigger.days.includes(day)
    return { ...current, trigger: { ...current.trigger, days: selected ? current.trigger.days.filter((item) => item !== day) : [...current.trigger.days, day] } }
  })
  return <section className="routine-form surface" aria-labelledby="routine-form-title">
    <div className="routine-form-head"><div><p className="eyebrow">Routine editor</p><h2 id="routine-form-title">{initial ? 'Edit routine' : 'New routine'}</h2></div><button className="routine-close" onClick={onCancel} aria-label="Close routine editor">×</button></div>
    <div className="routine-fields">
      <label>Name<input value={draft.name} maxLength={60} placeholder="Evening focus" onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
      <label>Trigger<select value={triggerType} onChange={(event) => setTriggerType(event.target.value as 'manual' | 'time')}><option value="manual">Manual</option><option value="time">Time</option><option value="ha_state" disabled>HA device state · unavailable</option></select></label>
      <label>Scene<select value={draft.action.scene_id} onChange={(event) => setDraft({ ...draft, action: { type: 'activate_scene', scene_id: event.target.value as SceneSummary['id'] } })}>{catalog.scenes.map((scene) => <option key={scene.id} value={scene.id}>{scene.name}</option>)}</select></label>
      <label className="routine-enabled"><input type="checkbox" checked={draft.enabled} onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} /><span>Enabled</span></label>
    </div>
    {draft.trigger.type === 'time' && <div className="routine-schedule"><label>Time<input type="time" value={draft.trigger.time} onChange={(event) => setDraft({ ...draft, trigger: { ...draft.trigger as Extract<RoutineTrigger, {type: 'time'}>, time: event.target.value } })} /></label><fieldset><legend>Days</legend><div className="weekday-grid">{routineWeekdays.map(([day, label]) => <button type="button" key={day} aria-pressed={draft.trigger.type === 'time' && draft.trigger.days.includes(day)} onClick={() => toggleDay(day)}>{label}</button>)}</div></fieldset><small>Timezone: {catalog.timezone}. Missed runs are not caught up.</small></div>}
    <div className="ha-trigger-disabled"><Icon name="system" /><span><strong>No trigger devices available</strong><small>HA-state triggers require a server allowlist.</small></span></div>
    {error && <p className="routine-form-error" role="alert">{error}</p>}
    <div className="routine-form-actions"><button className="routine-secondary" onClick={onCancel}>Cancel</button><button className="routine-primary" disabled={busy || !controlsAvailable} onClick={() => void save()}>{busy ? 'Saving…' : initial ? 'Save changes' : 'Create routine'}</button></div>
  </section>
}

const triggerDraft = (type: AutomationDraft['trigger']['type'], catalog: AutomationCatalog): AutomationDraft['trigger'] => {
  if (type === 'time') return { type: 'time', time: '22:30', days: ['mon', 'tue', 'wed', 'thu', 'fri'] }
  if (type === 'ha_state') return { type: 'ha_state', target_id: catalog.targets.lights[0]?.id ?? 'desk_ambient', from_state: null, to_state: 'on', attribute: null, operator: null, threshold: null, for_seconds: 0 }
  const target = catalog.targets.devices[0]
  return { type: 'device_health', device_id: target?.id ?? '', metric: 'online', operator: 'equals', value: false, for_seconds: 0 }
}

const defaultAction = (catalog: RoutineCatalog): AutomationAction => ({ type: 'scene', scene_id: catalog.scenes[0]?.id ?? 'normal' })
const lightAction = (target_id: Extract<AutomationAction, { type: 'light' }>['target_id'], operation: Extract<AutomationAction, { type: 'light' }>['operation']): Extract<AutomationAction, { type: 'light' }> => ({
  type: 'light', target_id, operation,
  on: operation === 'power' ? true : null,
  brightness: operation === 'brightness' ? 128 : null,
  rgb: operation === 'color' ? [255, 255, 255] : null,
  kelvin: operation === 'temperature' ? 4000 : null,
})
const conditionDraft = (source: AutomationPredicate['source'], catalog: AutomationCatalog): AutomationPredicate => source === 'time'
  ? { kind: 'predicate', source, target_id: null, field: 'local_time', operator: 'between', value: 18, upper: 23 }
  : source === 'light'
    ? { kind: 'predicate', source, target_id: catalog.targets.lights[0]?.id ?? 'desk_ambient', field: 'state', operator: 'equals', value: 'on', upper: null }
    : { kind: 'predicate', source, target_id: catalog.targets.devices[0]?.id ?? '', field: 'online', operator: 'equals', value: true, upper: null }

export function SmartAutomationBuilder({ catalog, routines, initial, controlsAvailable, onSave, onCancel }: {
  catalog: AutomationCatalog; routines: RoutineCatalog; initial?: Automation; controlsAvailable: boolean
  onSave: (draft: AutomationDraft) => Promise<void>; onCancel: () => void
}) {
  const [draft, setDraft] = useState<AutomationDraft>(() => initial ? {
    name: initial.name, description: initial.description, enabled: false, trigger: initial.trigger, conditions: initial.conditions,
    actions: initial.actions, cooldown_seconds: initial.cooldown_seconds, failure_policy: initial.failure_policy,
  } : blankAutomationDraft(routines.scenes[0]?.id))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const deviceTrigger = draft.trigger.type === 'device_health' ? draft.trigger : null
  const condition = draft.conditions?.kind === 'predicate' ? draft.conditions : null
  const updateAction = (index: number, action: AutomationAction) => setDraft((current) => ({ ...current, actions: current.actions.map((item, position) => position === index ? action : item) }))
  const save = async () => {
    const normalized = { ...draft, name: draft.name.trim(), description: draft.description.trim(), enabled: false }
    const invalid = validateAutomationDraft(normalized)
    if (invalid) { setError(invalid); return }
    setBusy(true); setError(null)
    try { await onSave(normalized) } catch (cause) { setError(cause instanceof Error ? cause.message : 'Automation could not be saved.') } finally { setBusy(false) }
  }
  return <section className="smart-builder surface" aria-labelledby="smart-builder-title">
    <header className="smart-builder-head"><div><p className="eyebrow">Smart automation</p><h2 id="smart-builder-title">{initial ? 'Edit rule' : 'New rule'}</h2><p>Build one bounded decision. New rules are saved disabled.</p></div><button type="button" className="routine-close" onClick={onCancel} aria-label="Close automation builder">×</button></header>
    <div className="smart-builder-grid">
      <div className="smart-builder-form">
        <div className="smart-identity"><label>Name<input name="automation-name" maxLength={60} value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label><label>Description<textarea maxLength={240} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></label></div>
        <div className="smart-rail" aria-label="Automation decision">
          <section className="smart-rail-step"><span className="smart-rail-node">WHEN</span><div className="smart-step-body"><h3>Observe</h3><label>Trigger<select value={draft.trigger.type} onChange={(event) => setDraft({ ...draft, trigger: triggerDraft(event.target.value as AutomationDraft['trigger']['type'], catalog) })}><option value="time">A time arrives</option><option value="ha_state">A light changes</option><option value="device_health">A device changes</option></select></label>
            {draft.trigger.type === 'time' && <><label>Time<input type="time" value={draft.trigger.time} onChange={(event) => setDraft({ ...draft, trigger: { ...draft.trigger as Extract<AutomationDraft['trigger'], {type: 'time'}>, time: event.target.value } })} /></label><div className="smart-weekdays">{routineWeekdays.map(([day, label]) => <button type="button" key={day} aria-pressed={draft.trigger.type === 'time' && draft.trigger.days.includes(day)} onClick={() => setDraft((current) => current.trigger.type !== 'time' ? current : ({ ...current, trigger: { ...current.trigger, days: current.trigger.days.includes(day) ? current.trigger.days.filter((item) => item !== day) : [...current.trigger.days, day] } }))}>{label}</button>)}</div></>}
            {draft.trigger.type === 'ha_state' && <><label>Light<select value={draft.trigger.target_id} onChange={(event) => setDraft({ ...draft, trigger: { ...draft.trigger as Extract<AutomationDraft['trigger'], {type: 'ha_state'}>, target_id: event.target.value as Extract<AutomationDraft['trigger'], {type: 'ha_state'}>['target_id'] } })}>{catalog.targets.lights.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>Becomes<select value={draft.trigger.to_state ?? 'on'} onChange={(event) => setDraft({ ...draft, trigger: { ...draft.trigger as Extract<AutomationDraft['trigger'], {type: 'ha_state'}>, from_state: null, to_state: event.target.value as 'on'|'off' } })}><option value="on">On</option><option value="off">Off</option></select></label></>}
            {deviceTrigger && <><label>Device<select value={deviceTrigger.device_id} onChange={(event) => setDraft({ ...draft, trigger: { ...deviceTrigger, device_id: event.target.value } })}>{catalog.targets.devices.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>Signal<select value={deviceTrigger.metric} onChange={(event) => { const metric = event.target.value as typeof deviceTrigger.metric; setDraft({ ...draft, trigger: { ...deviceTrigger, metric, operator: metric === 'online' ? 'equals' : 'above', value: metric === 'online' ? false : 80 } }) }}>{(catalog.targets.devices.find((item) => item.id === deviceTrigger.device_id)?.metrics ?? ['online']).map((metric) => <option key={metric} value={metric}>{metric}</option>)}</select></label>{deviceTrigger.metric === 'online' ? <label>State<select name="device-online" value={String(deviceTrigger.value)} onChange={(event) => setDraft({ ...draft, trigger: { ...deviceTrigger, operator: 'equals', value: event.target.value === 'true' } })}><option value="false">Offline</option><option value="true">Online</option></select></label> : <><label>Comparison<select name="device-operator" value={deviceTrigger.operator} onChange={(event) => setDraft({ ...draft, trigger: { ...deviceTrigger, operator: event.target.value as 'above' | 'below' } })}><option value="above">Above</option><option value="below">Below</option></select></label><label>Threshold<input name="device-value" type="number" value={Number(deviceTrigger.value)} onChange={(event) => setDraft({ ...draft, trigger: { ...deviceTrigger, value: Number(event.target.value) } })} /></label></>}<label>Hold for seconds<input name="trigger-hold" type="number" min="0" max="86400" value={deviceTrigger.for_seconds} onChange={(event) => setDraft({ ...draft, trigger: { ...deviceTrigger, for_seconds: Number(event.target.value) } })} /></label></>}
          </div></section>
          <section className="smart-rail-step"><span className="smart-rail-node">IF</span><div className="smart-step-body"><h3>Check evidence</h3>{condition ? <><label>Source<select name="condition-source" value={condition.source} onChange={(event) => setDraft({ ...draft, conditions: conditionDraft(event.target.value as AutomationPredicate['source'], catalog) })}><option value="time">Local time</option><option value="light">Light</option><option value="device">Device</option></select></label>{condition.source === 'light' && <label>Light<select value={condition.target_id ?? ''} onChange={(event) => setDraft({ ...draft, conditions: { ...condition, target_id: event.target.value } })}>{catalog.targets.lights.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}{condition.source === 'device' && <><label>Device<select value={condition.target_id ?? ''} onChange={(event) => setDraft({ ...draft, conditions: { ...condition, target_id: event.target.value } })}>{catalog.targets.devices.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>Signal<select name="condition-field" value={condition.field} onChange={(event) => { const field = event.target.value as AutomationPredicate['field']; const logical = field === 'online'; setDraft({ ...draft, conditions: { ...condition, field, operator: logical ? 'equals' : 'greater_than', value: logical ? true : 80, upper: null } }) }}>{(catalog.targets.devices.find((item) => item.id === condition.target_id)?.metrics ?? ['online']).map((field) => <option key={field} value={field}>{field}</option>)}</select></label></>}{condition.source === 'time' ? <><label>From hour<input name="condition-value" type="number" min="0" max="23" value={Number(condition.value)} onChange={(event) => setDraft({ ...draft, conditions: { ...condition, value: Number(event.target.value) } })} /></label><label>Until hour<input name="condition-upper" type="number" min="0" max="23" value={condition.upper ?? 23} onChange={(event) => setDraft({ ...draft, conditions: { ...condition, upper: Number(event.target.value) } })} /></label></> : <><label>Comparison<select name="condition-operator" value={condition.operator} onChange={(event) => setDraft({ ...draft, conditions: { ...condition, operator: event.target.value as AutomationPredicate['operator'] } })}><option value="equals">Equals</option><option value="not_equals">Does not equal</option>{!['state', 'available', 'online'].includes(condition.field) && <><option value="greater_than">Greater than</option><option value="less_than">Less than</option></>}</select></label><label>Value{['available', 'online'].includes(condition.field) ? <select name="condition-value" value={String(condition.value)} onChange={(event) => setDraft({ ...draft, conditions: { ...condition, value: event.target.value === 'true' } })}><option value="true">True</option><option value="false">False</option></select> : condition.field === 'state' ? <select name="condition-value" value={String(condition.value)} onChange={(event) => setDraft({ ...draft, conditions: { ...condition, value: event.target.value } })}><option value="on">On</option><option value="off">Off</option></select> : <input name="condition-value" type="number" value={Number(condition.value)} onChange={(event) => setDraft({ ...draft, conditions: { ...condition, value: Number(event.target.value) } })} />}</label></>}<p>UNKNOWN evidence always stops this rule.</p><button type="button" className="smart-secondary" onClick={() => setDraft({ ...draft, conditions: null })}>Remove condition</button></> : draft.conditions ? <><p>This existing grouped condition is preserved. Replace it to edit as one predicate.</p><button type="button" className="smart-secondary" onClick={() => setDraft({ ...draft, conditions: conditionDraft('time', catalog) })}>Replace condition</button><button type="button" className="smart-secondary" onClick={() => setDraft({ ...draft, conditions: null })}>Remove condition</button></> : <><p>No extra condition. Trigger evidence still must be fresh.</p><button type="button" className="smart-secondary" onClick={() => setDraft({ ...draft, conditions: conditionDraft('time', catalog) })}>Add condition</button></>}</div></section>
          <section className="smart-rail-step"><span className="smart-rail-node">THEN</span><div className="smart-step-body"><h3>Run in order</h3><ol className="smart-action-list">{draft.actions.map((action, index) => <li className="smart-action-row" key={index}><span>{index + 1}</span><label>Action<select name={`action-${index + 1}-type`} value={action.type} onChange={(event) => { const type = event.target.value as AutomationAction['type']; updateAction(index, type === 'scene' ? defaultAction(routines) : type === 'routine' ? { type: 'routine', routine_id: routines.routines.find((item) => !item.managed)?.id ?? '' } : lightAction(catalog.targets.lights[0]?.id ?? 'desk_ambient', 'power')) }}><option value="scene">Scene</option><option value="routine">Routine</option><option value="light">Light</option></select></label>
              {action.type === 'scene' && <label>Scene<select value={action.scene_id} onChange={(event) => updateAction(index, { ...action, scene_id: event.target.value as SceneSummary['id'] })}>{routines.scenes.map((scene) => <option key={scene.id} value={scene.id}>{scene.name}</option>)}</select></label>}
              {action.type === 'routine' && <label>Routine<select value={action.routine_id} onChange={(event) => updateAction(index, { ...action, routine_id: event.target.value })}>{routines.routines.filter((item) => !item.managed).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}
              {action.type === 'light' && <><label>Light<select value={action.target_id} onChange={(event) => updateAction(index, { ...action, target_id: event.target.value as typeof action.target_id })}>{catalog.targets.lights.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>Operation<select name={`action-${index + 1}-operation`} value={action.operation} onChange={(event) => updateAction(index, lightAction(action.target_id, event.target.value as typeof action.operation))}><option value="power">Power</option><option value="brightness">Brightness</option><option value="color" disabled={action.target_id === 'main_room_lights'}>Color</option><option value="temperature">Temperature</option></select></label>{action.operation === 'power' && <label>Power<select value={String(action.on)} onChange={(event) => updateAction(index, { ...action, on: event.target.value === 'true' })}><option value="true">On</option><option value="false">Off</option></select></label>}{action.operation === 'brightness' && <label>Brightness<input name={`action-${index + 1}-brightness`} type="number" min="1" max="255" value={action.brightness ?? 128} onChange={(event) => updateAction(index, { ...action, brightness: Number(event.target.value) })} /></label>}{action.operation === 'temperature' && <label>Kelvin<input type="number" min="2500" max={action.target_id === 'main_room_lights' ? 6500 : 9000} value={action.kelvin ?? 4000} onChange={(event) => updateAction(index, { ...action, kelvin: Number(event.target.value) })} /></label>}{action.operation === 'color' && <label>RGB<input aria-label={`Action ${index + 1} RGB`} value={(action.rgb ?? [255, 255, 255]).join(',')} onChange={(event) => { const rgb = event.target.value.split(',').map(Number); if (rgb.length === 3) updateAction(index, { ...action, rgb: rgb as [number, number, number] }) }} /></label>}</>}
              <div className="smart-row-actions"><button type="button" aria-label={`Move action ${index + 1} up`} disabled={index === 0} onClick={() => setDraft((current) => moveAutomationAction(current, index, index - 1))}>↑</button><button type="button" aria-label={`Move action ${index + 1} down`} disabled={index === draft.actions.length - 1} onClick={() => setDraft((current) => moveAutomationAction(current, index, index + 1))}>↓</button><button type="button" aria-label={`Remove action ${index + 1}`} disabled={draft.actions.length === 1} onClick={() => setDraft({ ...draft, actions: draft.actions.filter((_, position) => position !== index) })}>×</button></div></li>)}</ol><button type="button" className="smart-secondary" disabled={draft.actions.length >= catalog.limits.actions} onClick={() => setDraft({ ...draft, actions: [...draft.actions, defaultAction(routines)] })}>Add action</button></div></section>
        </div>
        <div className="smart-identity smart-safety"><label>Cooldown seconds<input name="automation-cooldown" type="number" min="0" max="604800" value={draft.cooldown_seconds} onChange={(event) => setDraft({ ...draft, cooldown_seconds: Number(event.target.value) })} /></label><label>On action failure<select name="failure-policy" value={draft.failure_policy} onChange={(event) => setDraft({ ...draft, failure_policy: event.target.value as AutomationDraft['failure_policy'] })}><option value="stop_on_failure">Stop remaining actions</option><option value="continue">Continue and record failures</option></select></label></div>
      </div>
      <aside className="smart-preview" aria-label="Rule preview"><span className="smart-disabled-mark">Saved disabled</span><h3>{draft.name.trim() || 'Untitled rule'}</h3><p>{draft.description.trim() || 'No description yet.'}</p><strong>Decision path</strong><ol>{draft.actions.map((action, index) => <li key={index}>{describeAutomationAction(action)}</li>)}</ol><details><summary>Server-owned targets</summary><p>{catalog.targets.devices.map((item) => item.name).join(', ')}</p><p>{catalog.targets.lights.map((item) => item.name).join(', ')}</p></details><small>Simulation never sends commands or writes real history.</small></aside>
    </div>
    {error && <p className="routine-form-error" role="alert">{error}</p>}
    <footer className="routine-form-actions"><button type="button" className="routine-secondary" onClick={onCancel}>Cancel</button><button type="button" className="routine-primary" disabled={busy || !controlsAvailable} onClick={() => void save()}>{busy ? 'Saving…' : 'Save automation'}</button></footer>
  </section>
}

export function SmartAutomationCard({ automation, catalog, simulation, canWrite, canEnable, canRun = false, live, enableLive = live, runLive = live, busy, onEdit, onToggle, onSimulate, onRun, onDelete }: {
  automation: Automation; catalog: AutomationCatalog; simulation: AutomationExecutionResult | null; canWrite: boolean; canEnable: boolean; canRun?: boolean; live: boolean; enableLive?: boolean; runLive?: boolean; busy: boolean
  onEdit: () => void; onToggle: () => void; onSimulate: () => void; onRun?: () => void; onDelete: () => void
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const result = simulation?.automation_id === automation.id ? simulationLabel(simulation) : null
  return <article className={`smart-card surface ${automation.enabled ? 'is-on' : 'is-off'}`}>
    <header><div><p>{automation.enabled ? 'Enabled' : 'Draft · off'}</p><h3>{automation.name}</h3></div><StatusPill status={automation.enabled ? 'online' : 'locked'} label={automation.enabled ? 'Active' : 'Off'} /></header>
    {automation.description && <p>{automation.description}</p>}
    <div className="smart-mini-rail"><span>WHEN</span><strong>{automation.trigger.type === 'time' ? `${automation.trigger.time} · ${automation.trigger.days.join(', ')}` : automation.trigger.type === 'ha_state' ? 'A light changes' : 'A device changes'}</strong><span>IF</span><strong>{automation.conditions ? 'Evidence is true' : 'No extra condition'}</strong><span>THEN</span><strong>{automation.actions.map(describeAutomationAction).join(' · ')}</strong></div>
    <dl><div><dt>Last decision</dt><dd>{automation.last_outcome ?? 'Not run'}</dd></div><div><dt>Cooldown</dt><dd>{automation.cooldown_seconds ? `${automation.cooldown_seconds}s` : 'None'}</dd></div></dl>
    {result && <div className={`smart-simulation is-${result.tone}`} role="status"><strong>{result.title}</strong><span>{result.detail}</span></div>}
    {canWrite && <div className="auto-actions"><button type="button" className="auto-action" disabled={!live || busy} onClick={onEdit}>Edit</button><button type="button" className="auto-action" disabled={!live || busy} onClick={onSimulate}>Simulate</button>{canRun && onRun && <button type="button" className="auto-action" disabled={!runLive || busy} onClick={onRun}>Run now</button>}{canEnable && <button type="button" className="auto-action is-primary" disabled={!enableLive || busy || catalog.storage_status !== 'ok'} onClick={onToggle}>{automation.enabled ? 'Turn off' : 'Turn on'}</button>}{confirmingDelete ? <span className="auto-confirm" role="group" aria-label={`Delete ${automation.name}?`}><button type="button" className="auto-action" onClick={() => setConfirmingDelete(false)}>Cancel</button><button type="button" className="auto-action is-danger" disabled={!live || busy} onClick={() => { setConfirmingDelete(false); onDelete() }}>Confirm delete</button></span> : <button type="button" className="auto-action is-danger" disabled={!live || busy} onClick={() => setConfirmingDelete(true)}>Delete</button>}</div>}
  </article>
}

export function SmartAutomationHistory({ records, nextCursor, loading, onMore }: { records: AutomationHistoryRecord[]; nextCursor: string | null; loading: boolean; onMore: () => void }) {
  const reason = (record: AutomationHistoryRecord) => record.reason === 'condition_unknown' ? 'Required evidence was unknown' : record.reason === 'condition_false' ? 'Conditions were false' : record.reason === 'cooldown' ? 'Cooldown active' : record.trigger_reason || 'Decision recorded'
  return <section className="smart-history surface" aria-labelledby="smart-history-title"><header><div><p className="eyebrow">Decision log</p><h2 id="smart-history-title">Recent history</h2></div><span>{records.length} shown</span></header>{records.length ? <ol>{records.map((record) => <li key={record.correlation_id}><span className={`auto-result is-${record.outcome}`}>{record.outcome}</span><strong>{record.automation_name || 'Automation'}</strong><time dateTime={record.timestamp}>{formatDateTime(record.timestamp, 'Europe/Istanbul')}</time><p>{reason(record)}</p></li>)}</ol> : <p className="smart-history-empty">No real decisions recorded. Simulations never appear here.</p>}{nextCursor && <button type="button" className="smart-secondary" disabled={loading} onClick={onMore}>{loading ? 'Loading…' : 'Load older decisions'}</button>}</section>
}

export function RoutineEmptyState({ canWrite, onCreate }: { canWrite: boolean; onCreate: () => void }) {
  return <article className="routine-empty surface"><span><Icon name="routines" /></span><h2>No routines yet</h2><p>Automate your RoomOS scenes.</p>{canWrite && <button className="routine-primary" onClick={onCreate}>Create your first routine</button>}</article>
}

const PILL: Record<SchedulerView['state'], 'healthy' | 'degraded' | 'offline' | 'unknown'> = { healthy: 'healthy', degraded: 'degraded', unavailable: 'offline', unknown: 'unknown' }

export function SchedulerStrip({ view, timezone, active, total, next, stale }: { view: SchedulerView; timezone: string; active: number; total: number; next: { name: string; text: string; at: Date } | null; stale: boolean }) {
  return <section className={`auto-overview surface is-${view.state}`} aria-label="Automation status">
    <div className="auto-overview-main">
      <span className="auto-overview-icon"><Icon name="routines" /></span>
      <div>
        <p className="auto-overview-label">Scheduler <StatusPill status={PILL[view.state]} label={view.label} /></p>
        <p className="auto-overview-summary">{view.summary}{view.checkedAt && <> · <span>checked <time dateTime={view.checkedAt}>{formatDateTime(view.checkedAt, timezone)}</time>{stale ? ' (last known)' : ''}</span></>}</p>
      </div>
    </div>
    <dl className="auto-overview-facts">
      <div><dt>Active</dt><dd>{active} of {total}</dd></div>
      <div><dt>Next scheduled</dt><dd>{next ? <><time dateTime={next.at.toISOString()}>{next.text.replace(/^Scheduled /, '')}</time><small>{next.name}</small></> : 'None'}</dd></div>
    </dl>
  </section>
}

function ResultBadge({ tone, label }: { tone: string; label: string }) {
  return <span className={`auto-result is-${tone}`}>{label}</span>
}

function ExecutionDetail({ execution, deviceNames, timezone }: { execution: ExecutionRecord; deviceNames: Record<string, string>; timezone: string }) {
  const described = resultLabel(execution.result, execution.reason)
  return <div className="auto-execution">
    <dl className="auto-kv">
      <div><dt>Result</dt><dd><ResultBadge tone={described.tone} label={described.label} /></dd></div>
      <div><dt>Started</dt><dd><time dateTime={execution.started_at}>{formatDateTime(execution.started_at, timezone)}</time></dd></div>
      <div><dt>Duration</dt><dd>{durationText(execution.duration_ms)}</dd></div>
      <div><dt>Lights</dt><dd>{countsText(execution)}</dd></div>
      <div><dt>Started by</dt><dd>{triggerSourceText(execution)}</dd></div>
      <div><dt>Reference</dt><dd><code>{execution.id.slice(0, 11)}</code></dd></div>
    </dl>
    {execution.targets.length > 0 && <ul className="auto-targets" aria-label="Light outcomes">{execution.targets.map((target) => <li key={target.device_id} className={`is-${target.result}`}><span>{deviceNames[target.device_id] ?? 'Light'}</span><strong>{target.result === 'applied' ? 'Applied' : target.result === 'skipped' ? 'Skipped' : 'Failed'}</strong></li>)}</ul>}
  </div>
}

export type CardActions = {
  run: ActionGuard; write: ActionGuard; busy: boolean
  onRun: () => void; onEdit: () => void; onToggle: () => void; onDelete: () => void
}

export function RoutineCard({ routine, catalog, executions, deviceNames, executionsStatus, now, actions, feedback, onDismiss, diagnosticsLive = true }: {
  routine: Routine; catalog: RoutineCatalog; executions: ExecutionIndex; deviceNames: Record<string, string>; executionsStatus: Availability; now: number
  diagnosticsLive?: boolean; actions?: CardActions; feedback?: RoutinesSnapshot['feedback']; onDismiss?: () => void
}) {
  const titleId = useId()
  const [confirming, setConfirming] = useState(false)
  const timezone = catalog.timezone
  const scene = catalog.scenes.find((item) => item.id === routine.action.scene_id)?.name ?? 'Unknown scene'
  const last = lastRun(routine, executions, executionsStatus === 'error' ? 'unavailable' : executionsStatus)
  const next = nextLine(routine, timezone, now)
  const history = routineExecutions(routine, executions).slice(0, 5)
  const quiet = quietNote(routine, timezone)
  const diagnostics = catalog.diagnostics
  const evaluations = diagnostics?.evaluations.filter((record) => record.routine_id === routine.id) ?? []
  const evaluated = evaluations[0]
  const executionOrigin = evaluations.find((record) => record.execution_id && record.execution_id === routine.last_result?.execution_id)?.trigger_origin
  const presence = diagnosticsLive ? diagnostics?.presence : null
  const showActions = !routine.managed && actions && (actions.run.visible || actions.write.visible)
  return <article className={`auto-card surface ${routine.enabled ? 'is-on' : 'is-off'}`} aria-labelledby={titleId}>
    <header className="auto-card-head">
      <span className="auto-icon"><Icon name={triggerIcon(routine.trigger)} /></span>
      <div className="auto-title"><h2 id={titleId}>{routine.name}</h2><p className="auto-kind">{routine.managed ? 'Managed by RoomOS' : 'Your routine'}</p></div>
      <StatusPill status={routine.enabled ? 'online' : 'locked'} label={routine.enabled ? 'Active' : 'Off'} />
    </header>
    <ul className="auto-when" aria-label="When it runs">{triggerLines(routine.trigger).map((line) => <li key={line}>{line}</li>)}</ul>
    <p className="auto-then">Activates <strong>{scene}</strong></p>
    <dl className="auto-facts">
      <div><dt>Last run</dt><dd><ResultBadge tone={last.tone} label={last.label} />{last.at && <time dateTime={last.at}>{formatDateTime(last.at, timezone)}</time>}
        {last.execution ? <small>{countsText(last.execution)} · {durationText(last.execution.duration_ms)}</small> : last.detailNote && <small>{last.detailNote}</small>}</dd></div>
      <div><dt>Last evaluation</dt><dd>{evaluated ? <><span>Last evaluated {originText(evaluated.trigger_origin)}</span><time dateTime={evaluated.evaluated_at}>{formatDateTime(evaluated.evaluated_at, timezone)}</time><small>{evaluationText(evaluated)}</small>{reportingAtEvaluationText(evaluated) && <small>{reportingAtEvaluationText(evaluated)}</small>}</> : <span>{diagnostics ? 'No recorded evaluation' : 'Diagnostics unavailable'}</span>}</dd></div>
      <div><dt>Last run origin</dt><dd>{originText(executionOrigin ?? null)}</dd></div>
      <div><dt>Next</dt><dd>{next.kind === 'scheduled' ? <><time dateTime={next.at.toISOString()}>{next.text}</time><small>Scheduled time, not a confirmed run</small></> : <span>{next.text}</span>}</dd></div>
    </dl>
    {routine.trigger.type === 'evening_home' && <div className="auto-diagnostics">
      <strong>Presence freshness unknown</strong>
      <p>GPS observation time is not available. This never blocks an automation.</p>
      <p>{presence?.reporting === 'fresh' ? 'Tracker reporting recently observed' : presence?.reporting === 'stale' ? `No tracker update observed for ${Math.round(presence.threshold_seconds / 3600)} hours` : 'Tracker reporting recency unknown'}{presence?.observed_at && <> · <time dateTime={presence.observed_at}>{formatDateTime(presence.observed_at, timezone)}</time></>}</p>
    </div>}
    {diagnostics && diagnostics.storage_status !== 'ok' && <p className="auto-diagnostics" role="status">Evaluation history needs attention. Evidence may be incomplete; automation behavior is unchanged.</p>}
    <details className="auto-details">
      <summary><span>Details</span><Icon name="chevron" /></summary>
      <div className="auto-details-body">
        <section><h3>When</h3><ul>{triggerRules(routine.trigger, timezone).map((rule) => <li key={rule}>{rule}</li>)}</ul></section>
        <section><h3>Then</h3><p>Activates the {scene} scene. Activating a scene by hand does not run this automation.</p></section>
        <section><h3>Last run</h3>
          {last.execution ? <ExecutionDetail execution={last.execution} deviceNames={deviceNames} timezone={timezone} />
            : last.at ? <p><ResultBadge tone={last.tone} label={last.label} /> <time dateTime={last.at}>{formatDateTime(last.at, timezone)}</time>. {last.detailNote}</p>
              : <p>No recorded execution.</p>}
        </section>
        <section><h3>Recent evaluations</h3>{evaluations.length ? <ul className="auto-history">{evaluations.slice(0, 10).map((record) => <li key={record.evaluation_id}><strong>{evaluationText(record)}</strong><time dateTime={record.evaluated_at}>{formatDateTime(record.evaluated_at, timezone)}</time><span>{originText(record.trigger_origin)}{reportingAtEvaluationText(record) && <> · {reportingAtEvaluationText(record)}</>}</span></li>)}</ul> : <p>No decision was recorded. This does not mean the routine was skipped.</p>}<p>Up to 250 evaluations retained for 30 days. Older trigger origins remain unknown.</p></section>
        {quiet && <section><h3>Not running?</h3><p>{quiet}</p></section>}
        {executionsStatus === 'ready' && <section><h3>Recent runs</h3>
          {history.length ? <ul className="auto-history">{history.map((record) => { const d = resultLabel(record.result, record.reason); return <li key={record.id}><ResultBadge tone={d.tone} label={d.label} /><time dateTime={record.started_at}>{formatDateTime(record.started_at, timezone)}</time><span>{countsText(record)}</span></li> })}</ul>
            : <p>No runs of this automation in recent history.</p>}
        </section>}
        {routine.managed && <section><h3>Managed by RoomOS</h3><p>Created and protected by RoomOS. It can't be edited, turned off, deleted or run by hand from here.</p></section>}
        <section><h3>Good to know</h3><p>Times are {timezoneLabel(timezone)}. Automations don't send push notifications; runs appear in Activity.</p>
          <Link className="auto-link" to="/activity">Open Activity <Icon name="chevron" /></Link></section>
      </div>
    </details>
    {showActions && actions && <div className="auto-actions">
      {!confirming ? <>
        {actions.run.visible && <button className="auto-action is-primary" disabled={!actions.run.ok || actions.busy} onClick={actions.onRun}><Icon name="play" />{actions.busy ? 'Working…' : 'Run now'}</button>}
        {actions.write.visible && <>
          <button className="auto-action" disabled={!actions.write.ok || actions.busy} onClick={actions.onEdit}><Icon name="edit" />Edit</button>
          <button className="auto-action" disabled={!actions.write.ok || actions.busy} onClick={actions.onToggle}>{routine.enabled ? 'Turn off' : 'Turn on'}</button>
          <button className="auto-action is-danger" disabled={!actions.write.ok || actions.busy} onClick={() => setConfirming(true)}><Icon name="trash" />Delete</button>
        </>}
      </> : <div className="auto-confirm" role="group" aria-label={`Delete ${routine.name}?`}>
        <span>Delete {routine.name}?</span>
        <button className="auto-action" autoFocus onClick={() => setConfirming(false)}>Cancel</button>
        <button className="auto-action is-danger" disabled={!actions.write.ok || actions.busy} onClick={() => { setConfirming(false); actions.onDelete() }}>Delete</button>
      </div>}
      {(actions.run.reason ?? actions.write.reason) && <p className="auto-action-reason">{actions.run.reason ?? actions.write.reason}</p>}
    </div>}
    {feedback?.scope === routine.id && <CommandNotice feedback={feedback.value} onDismiss={onDismiss} />}
  </article>
}

function useNow(intervalMs = 30_000) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), intervalMs); return () => window.clearInterval(timer) }, [intervalMs])
  return now
}

export function RoutinesView({ store, snapshot, now }: { store: RoutinesStore; snapshot: RoutinesSnapshot; now: number }) {
  const [editor, setEditor] = useState<Routine | 'new' | null>(null)
  const [smartEditor, setSmartEditor] = useState<Automation | 'new' | null>(null)
  const catalog = snapshot.catalog
  const smartCatalog = snapshot.automations
  const timezone = catalog?.timezone ?? 'Europe/Istanbul'
  const view = schedulerView(snapshot.scheduler, snapshot.schedulerStatus, catalog?.storage_status === 'corrupt')
  const stale = snapshot.connection !== 'connected'
  const nextAny = useMemo(() => {
    if (!catalog) return null
    return catalog.routines.map((routine) => ({ name: routine.name, line: nextLine(routine, timezone, now) }))
      .filter((item): item is { name: string; line: Extract<ReturnType<typeof nextLine>, { kind: 'scheduled' }> } => item.line.kind === 'scheduled')
      .sort((a, b) => a.line.at.getTime() - b.line.at.getTime())
      .map((item) => ({ name: item.name, text: item.line.text, at: item.line.at }))[0] ?? null
  }, [catalog, timezone, now])
  const pageWrite = store.guard(null, 'write')
  const hasEvening = catalog?.routines.some((routine) => routine.trigger.type === 'evening_home' && routine.enabled)
  const save = async (draft: RoutineDraft) => {
    await store.save(draft, editor && editor !== 'new' ? editor : null)
    if (store.getSnapshot().feedback?.value.tone === 'success') setEditor(null)
  }
  const saveSmart = async (draft: AutomationDraft) => {
    const existing = smartEditor && smartEditor !== 'new' ? smartEditor : null
    await store.saveAutomation(draft, existing)
    if (store.getSnapshot().feedback?.value.tone === 'success') setSmartEditor(null)
  }

  if (snapshot.catalogStatus === 'forbidden') return <article className="auto-locked surface"><span><Icon name="routines" /></span><h2>Automations aren't shared with this device</h2><p>Routine access requires routines:read permission.</p></article>
  return <>
    {catalog?.storage_status === 'corrupt' && <div className="notice error" role="alert"><span className="notice-icon">!</span><div><strong>Automation storage needs repair</strong><p>The scheduler is paused and nothing will run until an operator repairs it.</p></div></div>}
    {view.state === 'unavailable' && catalog?.storage_status !== 'corrupt' && <div className="notice error" role="alert"><span className="notice-icon">!</span><div><strong>Automations are not running</strong><p>{view.summary}</p></div></div>}
    {stale && catalog && <div className="auto-stale" role="status">{snapshot.connection === 'offline' ? 'Offline' : 'Reconnecting'} · showing last known data{snapshot.loadedAt ? ` from ${formatDateTime(new Date(snapshot.loadedAt), timezone)}` : ''}.</div>}
    {hasEvening && snapshot.homeAssistant === 'disconnected' && <div className="notice partial" role="status"><span className="notice-icon">!</span><div><strong>Home Assistant is disconnected</strong><p>Evening Home Lighting can't see sunset or arrivals. Events missed while disconnected are not made up later.</p></div></div>}
    {snapshot.executionStorage !== 'ok' && <div className="notice partial" role="status"><span className="notice-icon">!</span><div><strong>Run history needs attention</strong><p>Recent run details may be incomplete.</p></div></div>}
    {snapshot.catalogStatus === 'loading' && !catalog && <LoadingPanel label="Reading automations…" />}
    {snapshot.catalogStatus === 'error' && !catalog && <div className="notice error" role="alert"><span className="notice-icon">!</span><div><strong>Automations unavailable</strong><p>RoomOS Core could not be reached. The page will refresh when the connection returns.</p></div></div>}
    {catalog && <>
      <SchedulerStrip view={view} timezone={timezone} active={catalog.routines.filter((routine) => routine.enabled).length} total={catalog.routines.length} next={nextAny} stale={stale} />
      {snapshot.automationsStatus === 'error' && <div className="notice error" role="alert"><span className="notice-icon">!</span><div><strong>Smart rules unavailable</strong><p>Legacy routines remain available.</p></div></div>}
      {smartCatalog && <section className="smart-section" aria-labelledby="smart-section-title">
        <header className="smart-section-head"><div><p className="eyebrow">Conditional decisions</p><h2 id="smart-section-title">Smart rules</h2><p>Fresh evidence in, bounded actions out. New rules stay off until explicitly enabled.</p></div>{store.automationGuard(null, 'write').visible && !smartEditor && <button type="button" className="routine-primary" disabled={!store.automationGuard(null, 'write').ok} onClick={() => setSmartEditor('new')}>New automation</button>}</header>
        {smartEditor && <SmartAutomationBuilder catalog={smartCatalog} routines={catalog} initial={smartEditor === 'new' ? undefined : smartEditor} controlsAvailable={store.automationGuard(smartEditor === 'new' ? null : smartEditor, 'write').ok} onSave={saveSmart} onCancel={() => setSmartEditor(null)} />}
        {!smartEditor && <div className="smart-grid">{smartCatalog.automations.length ? smartCatalog.automations.map((automation) => {
          const write = store.automationGuard(automation, 'write'), toggle = store.automationGuard(automation, 'toggle'), run = store.automationGuard(automation, 'run')
          const pending = snapshot.pending?.routineId === automation.id
          return <SmartAutomationCard key={automation.id} automation={automation} catalog={smartCatalog} simulation={snapshot.automationSimulation} canWrite={write.visible} canEnable={toggle.visible} canRun={run.visible} live={write.ok} enableLive={toggle.ok} runLive={run.ok} busy={pending}
            onEdit={() => setSmartEditor(automation)} onToggle={() => void store.toggleAutomation(automation)} onSimulate={() => void store.simulateAutomation(automation)} onRun={() => void store.runAutomation(automation)} onDelete={() => void store.deleteAutomation(automation)} />
        }) : <article className="smart-empty surface"><h3>No smart rules yet</h3><p>Create a disabled draft, simulate it, then enable it only when the evidence is clear.</p></article>}</div>}
        <SmartAutomationHistory records={snapshot.automationHistory} nextCursor={snapshot.automationHistoryCursor} loading={snapshot.automationHistoryStatus === 'loading'} onMore={() => void store.loadMoreAutomationHistory()} />
      </section>}
      {pageWrite.visible && !editor && <div className="auto-page-actions"><button className="routine-primary" disabled={!pageWrite.ok} onClick={() => setEditor('new')}>+ New routine</button>{pageWrite.reason && <span>{pageWrite.reason}</span>}</div>}
      {snapshot.feedback?.scope === 'page' && <CommandNotice feedback={snapshot.feedback.value} onDismiss={() => store.dismiss()} />}
      {editor && <RoutineForm catalog={catalog} initial={editor === 'new' ? undefined : editor} onSave={save} onCancel={() => setEditor(null)} controlsAvailable={pageWrite.ok} />}
      {!editor && catalog.routines.length === 0 && <RoutineEmptyState canWrite={pageWrite.ok} onCreate={() => setEditor('new')} />}
      <div className="auto-grid">{catalog.routines.map((routine) => {
        const run = store.guard(routine, 'run'), write = store.guard(routine, 'write')
        return <RoutineCard key={routine.id} routine={routine} catalog={catalog} executions={snapshot.executions} deviceNames={snapshot.deviceNames} executionsStatus={snapshot.executionsStatus} now={now}
          diagnosticsLive={snapshot.trusted && snapshot.connection === 'connected'} feedback={snapshot.feedback} onDismiss={() => store.dismiss()}
          actions={{ run, write, busy: snapshot.pending?.routineId === routine.id, onRun: () => void store.run(routine), onEdit: () => setEditor(routine), onToggle: () => void store.toggle(routine), onDelete: () => void store.remove(routine) }} />
      })}</div>
    </>}
  </>
}

export function RoutinesPage() {
  const auth = useAuthStatus()
  const scopeKey = (auth?.device?.scopes ?? []).join(' ')
  const store = useMemo(() => new RoutinesStore(routinesApi, scopeKey ? scopeKey.split(' ') : [], auth?.authenticated === true, roomosLifecycle?.snapshot ?? 'reconnecting'), [scopeKey, auth])
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot)
  const now = useNow()

  useEffect(() => {
    if (!roomosLifecycle) { void store.load(); return }
    if (roomosLifecycle.snapshot === 'connected') void store.load()
    const unsubscribeStatus = roomosLifecycle.subscribeStatus((status) => {
      store.setConnection(status)
      if (status === 'offline' && !store.getSnapshot().catalog) void store.load()
    })
    // refreshOnEvent:false — ordinary events are applied locally; the lifecycle still calls
    // load() once on (re)connect and on return to foreground.
    const unsubscribeEvents = roomosLifecycle.subscribeEvents((event) => store.handleEvent(event), () => store.load(), { refreshOnEvent: false })
    return () => { unsubscribeStatus(); unsubscribeEvents() }
  }, [store])

  return <section className="page routines-page auto-page">
    <PageHeader eyebrow="Automations" title="Routines" subtitle="Scenes set your lights. Automations decide when a scene runs."><RealtimePill status={snapshot.connection} /></PageHeader>
    <RoutinesView store={store} snapshot={snapshot} now={now} />
  </section>
}

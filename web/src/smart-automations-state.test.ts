import { describe, expect, it } from 'vitest'
import {
  blankAutomationDraft,
  describeAutomationAction,
  moveAutomationAction,
  simulationLabel,
  validateAutomationDraft,
} from './routines-state'
import type { AutomationDraft, AutomationExecutionResult } from './types'

const valid = (): AutomationDraft => ({
  name: 'Cool the room',
  description: 'Reduce heat when the workstation is busy.',
  enabled: false,
  trigger: { type: 'device_health', device_id: 'workstation', metric: 'temperature', operator: 'above', value: 78, for_seconds: 300 },
  conditions: { kind: 'predicate', source: 'time', target_id: null, field: 'local_time', operator: 'between', value: 18, upper: 23 },
  actions: [
    { type: 'scene', scene_id: 'night' },
    { type: 'light', target_id: 'desk_ambient', operation: 'power', on: false, brightness: null, rgb: null, kelvin: null },
  ],
  cooldown_seconds: 900,
  failure_policy: 'stop_on_failure',
})

describe('Smart Automation builder state', () => {
  it('starts as a disabled, bounded time-to-scene draft', () => {
    expect(blankAutomationDraft('normal')).toEqual({
      name: '', description: '', enabled: false,
      trigger: { type: 'time', time: '22:30', days: ['mon', 'tue', 'wed', 'thu', 'fri'] },
      conditions: null,
      actions: [{ type: 'scene', scene_id: 'normal' }],
      cooldown_seconds: 0,
      failure_policy: 'stop_on_failure',
    })
  })

  it('validates the client-visible safety bounds without pretending to replace Core validation', () => {
    expect(validateAutomationDraft(valid())).toBeNull()
    expect(validateAutomationDraft({ ...valid(), name: ' ' })).toContain('name')
    expect(validateAutomationDraft({ ...valid(), actions: [] })).toContain('action')
    expect(validateAutomationDraft({ ...valid(), actions: Array.from({ length: 9 }, () => ({ type: 'scene' as const, scene_id: 'night' as const })) })).toContain('8')
    expect(validateAutomationDraft({ ...valid(), trigger: { type: 'time', time: '25:00', days: ['mon'] } })).toContain('time')
    expect(validateAutomationDraft({ ...valid(), trigger: { type: 'time', time: '22:00', days: [] } })).toContain('day')
  })

  it('reorders actions without mutating the saved draft', () => {
    const draft = valid()
    const moved = moveAutomationAction(draft, 1, 0)
    expect(moved.actions.map(describeAutomationAction)).toEqual(['Turn Desk ambient off', 'Activate Night'])
    expect(draft.actions.map(describeAutomationAction)).toEqual(['Activate Night', 'Turn Desk ambient off'])
    expect(moveAutomationAction(draft, 0, -1)).toBe(draft)
  })

  it('labels simulations explicitly and explains UNKNOWN without implying a command ran', () => {
    const result: AutomationExecutionResult = {
      mode: 'SIMULATION', correlation_id: `ac_${'a'.repeat(32)}`, automation_id: `au_${'b'.repeat(32)}`,
      outcome: 'skipped', reason: 'condition_unknown', duration_ms: 0, actions: [],
      condition: { result: 'unknown', summary: 'device.temperature greater_than', children: [] },
    }
    expect(simulationLabel(result)).toEqual({ tone: 'partial', title: 'Simulation · would not run', detail: 'Required evidence is unknown or stale. No command was sent.' })
  })
})

import { describe, expect, it } from 'vitest'
import { activityKindLabel, relativeActivityTime, severityLabel, executionReason, executionDuration } from './activity-ui'
import type { ActivityRecord } from './types'

const item = (type: ActivityRecord['type'], severity: ActivityRecord['severity'] = null): ActivityRecord => ({ id: 'act_00000000000000000000000000000000', type, title: 'Title', message: 'Message', source: 'roomos', occurred_at: '2026-10-03T10:00:00Z', severity, reference_id: 'safe' })

describe('activity presentation', () => {
  it('labels alert lifecycle and scenes without infrastructure details', () => {
    expect(activityKindLabel(item('alert_started', 'critical'))).toBe('Critical started')
    expect(activityKindLabel(item('alert_resolved', 'warning'))).toBe('Resolved')
    expect(activityKindLabel(item('scene_activated'))).toBe('Scene')
    expect(severityLabel('warning')).toBe('Warning')
  })
  it('formats bounded client-local relative time', () => {
    expect(relativeActivityTime('2026-10-03T09:57:00Z', new Date('2026-10-03T10:00:00Z'))).toBe('3 min ago')
    expect(relativeActivityTime('2026-10-02T10:00:00Z', new Date('2026-10-03T10:00:00Z'))).toBe('1 day ago')
  })
})

it('labels bounded reasons and never echoes unknown raw text', () => {
  for (const reason of ['unavailable','ha_unavailable','capability_rejected','power_on_not_confirmed','ha_error','policy_rejected','invalid_action','not_configured','duplicate','control_disabled','internal_error','interrupted']) expect(executionReason(reason)).not.toBe('Unknown result')
  expect(executionReason('SECRET http://192.0.2.1')).toBe('Unknown result')
  expect(executionDuration(412)).toBe('0.4 s')
  expect(executionDuration(null)).toBe('In progress')
})

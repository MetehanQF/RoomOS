import { describe, expect, it } from 'vitest'
import { insightLines, remaining, snoozeLabel, sourceLabel } from './alert-insight'

describe('alert insight wording', () => {
  it('states only server-proven durations', () => {
    const proven = insightLines({ value: 99.4, unit: '%', profile: 'normal', severity_basis: 'critical', threshold: 98, minutes: 30, coverage: 0.97, since: '2026-10-16T18:00:00Z', free_gb: null, reason: 'above 98% for at least 30 min' })
    expect(proven).toEqual(expect.arrayContaining(['Now 99.4%', '≥ 98% for at least 30 min', '97% sample coverage']))
    const unproven = insightLines({ value: 87, unit: '%', profile: 'gaming', severity_basis: null, threshold: null, minutes: null, coverage: null, since: null, free_gb: 65.8, reason: 'within policy' })
    expect(unproven).toEqual(['Now 87%', '65.8 GB free', 'Currently within policy', 'Gaming profile'])
    expect(unproven.join(' ')).not.toMatch(/for at least|hours? high/)
  })

  it('formats snooze countdowns and device names', () => {
    const now = Date.parse('2026-10-16T20:00:00Z')
    expect(remaining('2026-10-16T23:12:00Z', now)).toBe('3 h 12 min')
    expect(remaining('2026-10-23T20:00:00Z', now)).toBe('7 d')
    expect(remaining('2026-10-19T03:00:00Z', now)).toBe('2 d 7 h')
    expect(snoozeLabel({ until: '2026-10-16T20:45:00Z', severity: 'warning' }, now)).toBe('Snoozed · 45 min left')
    expect(snoozeLabel(undefined, now)).toBeNull()
    expect(sourceLabel('workstation')).toBe('Workstation')
  })
})

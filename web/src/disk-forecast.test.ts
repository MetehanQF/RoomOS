import { describe, expect, it } from 'vitest'
import { forecastText, type DiskForecastData } from './disk-forecast'

const base: DiskForecastData = { device_id: 'workstation', metric: 'disk_usage', status: 'insufficient', reason: 'Fewer than three days of observations', observations: 20, window_start: null, window_end: null, slope_per_day: null, latest: 61, days_until_full: null }

describe('disk forecast text', () => {
  it('states an estimate only when the server proved one', () => {
    expect(forecastText({ ...base, status: 'ok', reason: null, slope_per_day: 1.2, days_until_full: 36.7 })).toContain('about 37 days')
  })
  it('never invents a date for insufficient or unreliable evidence', () => {
    for (const status of ['insufficient', 'unreliable', 'not_increasing', 'beyond_horizon'] as const) {
      const text = forecastText({ ...base, status, days_until_full: null })
      expect(text).not.toMatch(/days\./)
    }
    expect(forecastText({ ...base, status: 'unreliable', reason: 'Large step change (deletion or capacity change)' })).toContain('deletion')
  })
})

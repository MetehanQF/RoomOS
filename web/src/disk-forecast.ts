export type DiskForecastData = { device_id: string; metric: string; status: 'ok'|'insufficient'|'unreliable'|'not_increasing'|'beyond_horizon'|'unavailable'; reason: string|null; observations: number; window_start: string|null; window_end: string|null; slope_per_day: number|null; latest: number|null; days_until_full: number|null }

export const VERDICT: Record<DiskForecastData['status'], string> = {
  ok: 'Estimated', insufficient: 'Not enough evidence', unreliable: 'Unreliable', not_increasing: 'Not growing', beyond_horizon: 'Beyond one year', unavailable: 'Unavailable',
}

export function forecastText(data: DiskForecastData) {
  if (data.status === 'ok' && data.days_until_full !== null && data.slope_per_day !== null)
    return `At the recent rate (+${data.slope_per_day.toFixed(2)} points/day) the disk would be full in about ${Math.round(data.days_until_full)} days.`
  return data.reason ?? 'No estimate.'
}

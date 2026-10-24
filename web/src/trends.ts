import type { HistoryMetric, HistoryNode, HistoryPoint, HistoryRange, HistorySeries } from './types'

export const HISTORY_NODES: ReadonlyArray<readonly [HistoryNode, string]> = [['primary', 'Primary'], ['secondary', 'Secondary']]
export const HISTORY_RANGES: ReadonlyArray<readonly [HistoryRange, string]> = [['1h', '1h'], ['6h', '6h'], ['24h', '24h']]
export const HISTORY_METRICS: ReadonlyArray<{ id: HistoryMetric; label: string; note?: string }> = [
  { id: 'cpu', label: 'CPU', note: 'CPU utilization · ~60 s average' },
  { id: 'ram', label: 'RAM' },
  { id: 'temperature', label: 'Temperature' },
]

const BOUNDS: Record<HistoryRange, { buckets: number; seconds: number }> = {
  '1h': { buckets: 60, seconds: 60 },
  '6h': { buckets: 120, seconds: 180 },
  '24h': { buckets: 144, seconds: 600 },
}
const UNITS: Record<HistoryMetric, string> = { cpu: '%', ram: '%', temperature: '°C' }
const KEYS = ['node', 'metric', 'unit', 'range', 'bucket_seconds', 'points', 'gaps', 'source', 'freshness', 'generated_at', 'cached']

export class HistoryContractError extends Error {
  constructor() {
    super('RoomOS Core returned an incompatible history response.')
    this.name = 'HistoryContractError'
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const finiteOrNull = (value: unknown) => value === null || (typeof value === 'number' && Number.isFinite(value))
const isTime = (value: unknown) => typeof value === 'string' && !Number.isNaN(Date.parse(value))

export function historyQuery(node: HistoryNode, metric: HistoryMetric, range: HistoryRange) {
  return `/system/history?${new URLSearchParams({ node, metric, range }).toString()}`
}

/** Strict client-side contract check: bounded, finite, logical identifiers only. */
export function parseHistory(payload: unknown, node: HistoryNode, metric: HistoryMetric, range: HistoryRange): HistorySeries {
  const bound = BOUNDS[range]
  if (!isRecord(payload)
    || Object.keys(payload).some((key) => !KEYS.includes(key))
    || payload.node !== node || payload.metric !== metric || payload.range !== range
    || payload.unit !== UNITS[metric] || payload.source !== 'home_assistant'
    || payload.bucket_seconds !== bound.seconds
    || !['fresh', 'stale', 'unknown', 'unavailable'].includes(payload.freshness as string)
    || !isTime(payload.generated_at) || typeof payload.cached !== 'boolean'
    || !Array.isArray(payload.points) || payload.points.length > bound.buckets
    || !Array.isArray(payload.gaps) || payload.gaps.length > bound.buckets) {
    throw new HistoryContractError()
  }
  for (const point of payload.points) {
    if (!isRecord(point) || !isTime(point.ts) || !finiteOrNull(point.avg) || !finiteOrNull(point.min) || !finiteOrNull(point.max)
      || typeof point.samples !== 'number' || !Number.isInteger(point.samples) || point.samples < 0) throw new HistoryContractError()
  }
  for (const gap of payload.gaps) {
    if (!isRecord(gap) || !isTime(gap.start) || !isTime(gap.end) || !['no_data', 'unavailable', 'offline'].includes(gap.reason as string)) throw new HistoryContractError()
  }
  return payload as HistorySeries
}

export type TrendSummary = { latest: number | null; min: number | null; max: number | null; valid: number }

export function summarize(points: HistoryPoint[]): TrendSummary {
  const valid = points.filter((point) => point.avg !== null)
  const lastValid = valid[valid.length - 1]
  const mins = valid.map((point) => point.min ?? point.avg as number)
  const maxes = valid.map((point) => point.max ?? point.avg as number)
  return {
    latest: lastValid ? lastValid.avg : null,
    min: mins.length ? Math.min(...mins) : null,
    max: maxes.length ? Math.max(...maxes) : null,
    valid: valid.length,
  }
}

export type TrendGeometry = { lines: string[]; bands: string[]; yMin: number; yMax: number }

/** avg → polylines broken at empty buckets; min/max → closed band paths per contiguous run. */
export function trendGeometry(points: HistoryPoint[], metric: HistoryMetric, width = 300, height = 96): TrendGeometry {
  const summary = summarize(points)
  let yMin = metric === 'temperature' ? Math.floor(((summary.min ?? 30) - 3) / 5) * 5 : 0
  let yMax = metric === 'temperature' ? Math.ceil(((summary.max ?? 60) + 3) / 5) * 5 : 100
  if (metric !== 'temperature' && (summary.max ?? 0) > 100) yMax = Math.ceil((summary.max as number) / 10) * 10
  if (yMax <= yMin) { yMin -= 1; yMax += 1 }
  const step = points.length > 1 ? width / (points.length - 1) : width
  const x = (index: number) => +(index * step).toFixed(1)
  const y = (value: number) => +(height - ((value - yMin) / (yMax - yMin)) * height).toFixed(1)
  const runs: number[][] = []
  points.forEach((point, index) => {
    if (point.avg === null) return
    const run = runs[runs.length - 1]
    if (run && run[run.length - 1] === index - 1) run.push(index)
    else runs.push([index])
  })
  const lines = runs.map((run) => run.map((index) => `${x(index)},${y(points[index].avg as number)}`).join(' '))
  const bands = runs.map((run) => {
    const top = run.map((index) => `${x(index)},${y(points[index].max ?? points[index].avg as number)}`)
    const bottom = [...run].reverse().map((index) => `${x(index)},${y(points[index].min ?? points[index].avg as number)}`)
    return `M${top.join(' L')} L${bottom.join(' L')} Z`
  })
  return { lines, bands, yMin, yMax }
}

export type TrendState = 'loading' | 'ready' | 'stale' | 'error' | 'insufficient'

export function trendState({ series, error, loading }: { series: HistorySeries | null; error: string | null; loading: boolean }): TrendState {
  if (error && !series) return 'error'
  if (!series) return loading ? 'loading' : 'error'
  if (summarize(series.points).valid < 2) return 'insufficient'
  if (error || series.freshness === 'stale' || series.freshness === 'unavailable') return 'stale'
  return 'ready'
}

export function formatValue(value: number | null, unit: string) {
  if (value === null) return '—'
  return `${value.toFixed(value >= 100 ? 0 : 1)}${unit === '%' ? '%' : ` ${unit}`}`
}

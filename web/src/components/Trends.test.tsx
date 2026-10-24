import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { TrendChart, TrendsControls, TrendsSection } from './Trends'
import { formatValue, historyQuery, HistoryContractError, parseHistory, summarize, trendGeometry, trendState } from '../trends'
import { MemoryRouter } from 'react-router-dom'
import { SystemPage } from '../pages/System'
import type { HistoryPoint, HistorySeries } from '../types'

const point = (index: number, avg: number | null, min = avg, max = avg): HistoryPoint => ({
  ts: new Date(Date.UTC(2026, 9, 6, 9, 0) + index * 180_000).toISOString(), avg, min, max, samples: avg === null ? 0 : 3,
})

function series(overrides: Partial<HistorySeries> = {}): HistorySeries {
  return {
    node: 'primary', metric: 'temperature', unit: '°C', range: '6h', bucket_seconds: 180,
    points: Array.from({ length: 120 }, (_, index) => point(index, 50 + (index % 5), 49, 52 + (index === 60 ? 9 : 0))),
    gaps: [], source: 'home_assistant', freshness: 'fresh', generated_at: '2026-10-06T15:00:00Z', cached: false,
    ...overrides,
  }
}

describe('history contract parsing', () => {
  it('accepts the bounded logical response', () => {
    const parsed = parseHistory(series(), 'primary', 'temperature', '6h')
    expect(parsed.points).toHaveLength(120)
  })

  it('builds only logical query parameters', () => {
    expect(historyQuery('secondary', 'ram', '24h')).toBe('/system/history?node=secondary&metric=ram&range=24h')
  })

  it.each([
    ['raw HA identifier', { ...series(), entity_id: 'sensor.primary_temperature' }],
    ['wrong node', series({ node: 'secondary' })],
    ['wrong unit', series({ unit: 'F' })],
    ['wrong bucket size', series({ bucket_seconds: 60 })],
    ['too many points', series({ points: Array.from({ length: 121 }, (_, index) => point(index, 50)) })],
    ['non-finite value', series({ points: [{ ...point(0, 1), avg: Number.NaN }] })],
    ['bad gap reason', series({ gaps: [{ start: '2026-10-06T09:00:00Z', end: '2026-10-06T09:03:00Z', reason: 'sql' as 'offline' }] })],
  ])('rejects %s', (_, payload) => {
    expect(() => parseHistory(payload, 'primary', 'temperature', '6h')).toThrow(HistoryContractError)
  })
})

describe('trend geometry and summary', () => {
  it('breaks the average line at gaps and keeps a min/max band per run', () => {
    const points = [point(0, 40), point(1, 41), point(2, null), point(3, null), point(4, 45), point(5, 46, 44, 60)]
    const geometry = trendGeometry(points, 'cpu')
    expect(geometry.lines).toHaveLength(2)
    expect(geometry.bands).toHaveLength(2)
    expect(geometry.bands.every((band) => band.startsWith('M') && band.endsWith('Z'))).toBe(true)
    expect(summarize(points)).toEqual({ latest: 46, min: 40, max: 60, valid: 4 })
  })

  it('renders 144 buckets without exceeding the bound', () => {
    const points = Array.from({ length: 144 }, (_, index) => point(index, index % 3 === 0 ? null : 30))
    const geometry = trendGeometry(points, 'ram')
    const vertices = geometry.lines.reduce((sum, line) => sum + line.split(' ').length, 0)
    expect(vertices).toBeLessThanOrEqual(144)
  })

  it('derives independent chart states', () => {
    expect(trendState({ series: null, error: null, loading: true })).toBe('loading')
    expect(trendState({ series: null, error: 'down', loading: false })).toBe('error')
    expect(trendState({ series: series(), error: null, loading: false })).toBe('ready')
    expect(trendState({ series: series({ freshness: 'stale' }), error: null, loading: false })).toBe('stale')
    expect(trendState({ series: series(), error: 'refresh failed', loading: false })).toBe('stale')
    expect(trendState({ series: series({ points: [point(0, 1), point(1, null)] }), error: null, loading: false })).toBe('insufficient')
  })

  it('formats units', () => {
    expect(formatValue(55.123, '°C')).toBe('55.1 °C')
    expect(formatValue(100, '%')).toBe('100%')
    expect(formatValue(null, '%')).toBe('—')
  })
})

describe('Trends UI', () => {
  it('renders the section with node and range controls and three charts', () => {
    const html = renderToStaticMarkup(<TrendsSection />)
    expect(html).toContain('Trends')
    expect(html).toContain('aria-label="Trend node"')
    expect(html).toContain('aria-label="Trend range"')
    expect((html.match(/data-trend="/g) ?? []).length).toBe(3)
    for (const metric of ['cpu', 'ram', 'temperature']) expect(html).toContain(`data-trend="${metric}"`)
    expect((html.match(/data-state="loading"/g) ?? []).length).toBe(3)
  })

  it('reflects Primary/Secondary and 1h/6h/24h selection with aria-pressed', () => {
    const secondary = renderToStaticMarkup(<TrendsControls node="secondary" range="24h" onNode={() => undefined} onRange={() => undefined} />)
    expect(secondary).toMatch(/aria-pressed="true"[^>]*>Secondary/)
    expect(secondary).toMatch(/aria-pressed="false"[^>]*>Primary/)
    expect(secondary).toMatch(/aria-pressed="true"[^>]*>24h/)
    expect((secondary.match(/<button/g) ?? []).length).toBe(5)
    const initial = renderToStaticMarkup(<TrendsSection initialNode="secondary" initialRange="1h" />)
    expect(initial).toMatch(/aria-pressed="true"[^>]*>Secondary/)
    expect(initial).toMatch(/aria-pressed="true"[^>]*>1h/)
  })

  it('renders a ready chart with band, line, latest and window min/max', () => {
    const html = renderToStaticMarkup(<TrendChart metric="temperature" series={series()} state="ready" />)
    expect(html).toContain('class="trend-band"')
    expect(html).toContain('class="trend-line"')
    expect(html).toContain('role="img"')
    expect(html).toContain('Latest')
    expect(html).toContain('61.0 °C')
    expect(html).not.toContain('sensor.')
  })

  it('labels CPU as interval utilization, not snapshots', () => {
    const html = renderToStaticMarkup(<TrendChart metric="cpu" series={series({ metric: 'cpu', unit: '%' })} state="ready" />)
    expect(html).toContain('CPU utilization · ~60 s average')
    expect(html).not.toContain('snapshot')
  })

  it('isolates errors per chart with retry', () => {
    const html = renderToStaticMarkup(<TrendChart metric="ram" series={null} state="error" error="RoomOS cannot reach Home Assistant right now." onRetry={() => undefined} />)
    expect(html).toContain('role="alert"')
    expect(html).toContain('Retry')
    expect(html).not.toContain('trend-line')
  })

  it('shows stale and insufficient states distinctly', () => {
    const stale = renderToStaticMarkup(<TrendChart metric="cpu" series={series({ freshness: 'stale', metric: 'cpu', unit: '%' })} state="stale" onRetry={() => undefined} />)
    expect(stale).toContain('is-stale')
    expect(stale).toContain('History may be out of date')
    const insufficient = renderToStaticMarkup(<TrendChart metric="ram" series={series({ points: [point(0, 1)] })} state="insufficient" />)
    expect(insufficient).toContain('Not enough history')
  })

  it('draws gaps as line breaks', () => {
    const points = [point(0, 40), point(1, 41), point(2, null), point(3, 43), point(4, 44)]
    const html = renderToStaticMarkup(<TrendChart metric="cpu" series={series({ metric: 'cpu', unit: '%', points })} state="ready" />)
    expect((html.match(/<polyline/g) ?? []).length).toBe(2)
  })

  it('does not put the System page into an error state when history is unavailable', () => {
    const html = renderToStaticMarkup(<MemoryRouter><SystemPage /></MemoryRouter>)
    expect(html).not.toContain('role="alert"')
  })
})

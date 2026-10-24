import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Icon } from './Icons'
import type { IconName } from './Icons'
import { formatValue, HISTORY_METRICS, HISTORY_NODES, HISTORY_RANGES, summarize, trendGeometry, trendState, type TrendState } from '../trends'
import type { HistoryMetric, HistoryNode, HistoryRange, HistorySeries } from '../types'

const REFRESH_MS = 60_000
const ICONS: Record<HistoryMetric, IconName> = { cpu: 'cpu', ram: 'memory', temperature: 'thermometer' }
const STATE_LABEL: Record<TrendState, string> = { loading: 'Loading', ready: 'Live', stale: 'Stale', error: 'Unavailable', insufficient: 'Not enough data' }

type Loader = (node: HistoryNode, metric: HistoryMetric, range: HistoryRange) => Promise<HistorySeries>

/** Independent per-chart fetch; a failure here never touches the live System dashboard. */
function useHistorySeries(node: HistoryNode, metric: HistoryMetric, range: HistoryRange, loader: Loader = api.history) {
  const key = `${node}:${metric}:${range}`
  const [state, setState] = useState<{ key: string; series: HistorySeries | null; error: string | null; loading: boolean }>({ key, series: null, error: null, loading: true })
  const current = useRef(key)
  useEffect(() => { current.current = key }, [key])
  const load = useCallback(async () => {
    setState((previous) => previous.key === key ? { ...previous, loading: true } : { key, series: null, error: null, loading: true })
    try {
      const series = await loader(node, metric, range)
      if (current.current === key) setState({ key, series, error: null, loading: false })
    } catch (cause) {
      if (current.current === key) setState((previous) => ({ key, series: previous.key === key ? previous.series : null, error: cause instanceof Error ? cause.message : 'History unavailable', loading: false }))
    }
  }, [key, loader, node, metric, range])
  useEffect(() => {
    void load()
    const timer = window.setInterval(() => { void load() }, REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [load])
  const view = state.key === key ? state : { key, series: null, error: null, loading: true }
  return { ...view, retry: load }
}

export function TrendsControls({ node, range, onNode, onRange }: { node: HistoryNode; range: HistoryRange; onNode: (node: HistoryNode) => void; onRange: (range: HistoryRange) => void }) {
  return <div className="trends-controls">
    <div className="activity-filters trends-segment" role="group" aria-label="Trend node">{HISTORY_NODES.map(([id, label]) => <button key={id} type="button" aria-pressed={node === id} onClick={() => onNode(id)}>{label}</button>)}</div>
    <div className="activity-filters trends-segment" role="group" aria-label="Trend range">{HISTORY_RANGES.map(([id, label]) => <button key={id} type="button" aria-pressed={range === id} onClick={() => onRange(id)}>{label}</button>)}</div>
  </div>
}

export function TrendChart({ metric, series, state, error, onRetry }: { metric: HistoryMetric; series: HistorySeries | null; state: TrendState; error?: string | null; onRetry?: () => void }) {
  const definition = HISTORY_METRICS.find((item) => item.id === metric)!
  const unit = series?.unit ?? (metric === 'temperature' ? '°C' : '%')
  const summary = series ? summarize(series.points) : null
  const geometry = series && state !== 'insufficient' ? trendGeometry(series.points, metric) : null
  const label = `${definition.label} trend${summary ? `, latest ${formatValue(summary.latest, unit)}, range ${formatValue(summary.min, unit)} to ${formatValue(summary.max, unit)}` : ''}`
  return <article className={`trend-card surface is-${state}`} data-trend={metric} data-state={state} aria-busy={state === 'loading'}>
    <header className="trend-card-head">
      <span className="service-card-icon"><Icon name={ICONS[metric]} /></span>
      <div><h3>{definition.label}</h3>{definition.note && <p className="trend-note">{definition.note}</p>}</div>
      <span className={`trend-state trend-state-${state}`}>{STATE_LABEL[state]}</span>
    </header>
    {state === 'loading' && <div className="trend-placeholder" aria-label={`Loading ${definition.label} history`}><span /></div>}
    {state === 'error' && <div className="trend-placeholder trend-error" role="alert"><p>{error ?? 'History is unavailable.'}</p>{onRetry && <button type="button" onClick={onRetry}><Icon name="pulse" />Retry</button>}</div>}
    {state === 'insufficient' && <div className="trend-placeholder"><p>Not enough history for this window yet.</p></div>}
    {geometry && <svg className="trend-svg" viewBox="0 0 300 96" preserveAspectRatio="none" role="img" aria-label={label}>
      <line x1="0" x2="300" y1="48" y2="48" className="trend-grid" />
      {geometry.bands.map((band, index) => <path key={`b${index}`} d={band} className="trend-band" />)}
      {geometry.lines.map((line, index) => <polyline key={`l${index}`} points={line} className="trend-line" />)}
    </svg>}
    {summary && state !== 'loading' && state !== 'error' && <footer className="trend-stats">
      <span><small>Latest</small><strong>{formatValue(summary.latest, unit)}</strong></span>
      <span><small>Min</small>{formatValue(summary.min, unit)}</span>
      <span><small>Max</small>{formatValue(summary.max, unit)}</span>
    </footer>}
    {state === 'stale' && <p className="trend-stale" role="status">{error ? 'Showing last good history · refresh failed' : 'History may be out of date'}{onRetry && <button type="button" onClick={onRetry}>Retry</button>}</p>}
  </article>
}

function TrendCard({ node, metric, range }: { node: HistoryNode; metric: HistoryMetric; range: HistoryRange }) {
  const { series, error, loading, retry } = useHistorySeries(node, metric, range)
  return <TrendChart metric={metric} series={series} error={error} state={trendState({ series, error, loading })} onRetry={() => { void retry() }} />
}

export function TrendsSection({ initialNode = 'primary', initialRange = '6h' }: { initialNode?: HistoryNode; initialRange?: HistoryRange }) {
  const [node, setNode] = useState<HistoryNode>(initialNode)
  const [range, setRange] = useState<HistoryRange>(initialRange)
  return <section className="trends-section" aria-labelledby="trends-title">
    <div className="trends-head">
      <div><p className="eyebrow">Recorder history · min/avg/max</p><h2 id="trends-title">Trends</h2></div>
      <TrendsControls node={node} range={range} onNode={setNode} onRange={setRange} />
    </div>
    <div className="trends-grid">{HISTORY_METRICS.map(({ id }) => <TrendCard key={`${node}-${id}-${range}`} node={node} metric={id} range={range} />)}</div>
  </section>
}

import { useEffect, useState, useSyncExternalStore } from 'react'
import { api } from '../api'
import { HealthStore, type HealthSnapshot } from '../health-store'
import { HEALTH_LABEL, orderComponents, overallHeadline } from '../health'
import { Icon } from './Icons'
import { StatusPill } from './Common'
import type { HealthComponent, HealthReport, HealthStatus } from '../types'

const PILL: Record<HealthStatus, 'healthy' | 'degraded' | 'offline' | 'unknown'> = { healthy: 'healthy', degraded: 'degraded', unhealthy: 'offline', unknown: 'unknown' }

function HealthPill({ status, historical = false }: { status: HealthStatus; historical?: boolean }) {
  return <StatusPill status={historical ? 'unknown' : PILL[status]} label={`${historical ? 'Last known: ' : ''}${HEALTH_LABEL[status]}`} />
}

export function HealthRow({ component, historical = false }: { component: HealthComponent; historical?: boolean }) {
  const head = <><span className="health-row-text"><strong>{component.name}</strong><span>{component.summary}</span></span><HealthPill status={component.status} historical={historical} /></>
  if (!component.details.length) {
    return <li className={`health-row is-${component.status}`} data-health={component.id}><div className="health-row-head">{head}</div></li>
  }
  return <li className={`health-row is-${component.status}`} data-health={component.id}>
    <details>
      <summary className="health-row-head">{head}<Icon name="chevron" className="health-chevron" /></summary>
      <ul className="health-details" aria-label={`${component.name} details`}>{component.details.map((detail) => <li key={detail}>{detail}</li>)}</ul>
    </details>
  </li>
}

export function HealthPanel({ report, historical = false }: { report: HealthReport; historical?: boolean }) {
  return <article className={`health-panel surface is-${historical ? 'stale' : report.overall}`} aria-labelledby="roomos-health-title" data-overall={historical ? 'unknown' : report.overall}>
    <div className="health-head">
      <div><p className="eyebrow">Internal subsystems</p><h2 id="roomos-health-title">RoomOS Health</h2><p className="health-headline">{historical ? 'Last known data · current system health is unknown' : overallHeadline(report)}</p></div>
      <HealthPill status={report.overall} historical={historical} />
    </div>
    <ul className="health-list" aria-label={historical ? 'Last known subsystem health' : 'RoomOS subsystem health'}>{orderComponents(report.components).map((component) => <HealthRow key={component.id} component={component} historical={historical} />)}</ul>
  </article>
}

export function HealthUnavailable({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <article className="health-panel surface is-error" role="alert"><div className="health-head"><div><p className="eyebrow">Internal subsystems</p><h2>RoomOS Health</h2><p className="health-headline">Health could not be loaded. {message}</p></div><button type="button" className="health-retry" onClick={onRetry}><Icon name="pulse" />Retry</button></div></article>
}

function CheckTime({ at }: { at: number | null }) {
  return at === null ? <>Never</> : <time dateTime={new Date(at).toISOString()}>{new Date(at).toLocaleString()}</time>
}

export function HealthView({ state, onRetry }: { state: HealthSnapshot; onRetry: () => void }) {
  const initial = !state.report && (state.refreshing || state.lastAttemptResult === 'none')
  const label = state.freshness === 'fresh' ? 'Current data' : state.report ? 'Stale data · Last known data' : initial ? 'Reading RoomOS health…' : 'Health unavailable'
  const age = state.ageMs === null ? null : Math.floor(state.ageMs / 1_000)
  return <section className="health-experience" aria-label="Health reliability" data-freshness={state.freshness}>
    <div className="health-panel health-freshness surface" aria-busy={state.refreshing}>
      <div className="health-head"><div><p className="eyebrow">Health data freshness</p><p className="health-freshness-label" role="status">{label}</p></div>
        <button type="button" className="health-retry" onClick={onRetry} disabled={state.refreshing}><Icon name="pulse" />{state.refreshing ? 'Checking…' : 'Retry'}</button>
      </div>
      {!state.report && <p>No health data received yet.</p>}
      {state.error && <p role="alert">{state.error} Automatic retries resume while this page is visible.</p>}
      <dl className="health-check-times">
        <div><dt>Last successful check</dt><dd><CheckTime at={state.lastSuccessAt} /></dd></div>
        <div><dt>Last attempt</dt><dd><CheckTime at={state.lastAttemptAt} /> · {state.lastAttemptResult === 'pending' ? 'Checking' : state.lastAttemptResult === 'success' ? 'Succeeded' : state.lastAttemptResult === 'error' ? 'Failed' : state.lastAttemptResult === 'cancelled' ? 'Interrupted' : 'Not started'}</dd></div>
        {age !== null && <div><dt>Report age</dt><dd>{age < 60 ? `${age}s` : `${Math.floor(age / 60)}m ${age % 60}s`}</dd></div>}
      </dl>
      {state.report && state.freshness === 'stale' && <p>These measurements are historical, not a confirmation that the system is healthy now.</p>}
    </div>
    {state.report ? <HealthPanel report={state.report} historical={state.freshness !== 'fresh'} /> : <h2 className="health-empty-title">RoomOS Health</h2>}
  </section>
}

/** Dedicated lifecycle: failures never change other System loaders or device controls. */
export function HealthSection() {
  const [store] = useState(() => new HealthStore((signal) => api.systemHealth(signal)))
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot)
  useEffect(() => { store.start(); return store.stop }, [store])
  return <HealthView state={state} onRetry={store.refresh} />
}

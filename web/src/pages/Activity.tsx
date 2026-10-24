import { useCallback, useState } from 'react'
import { api } from '../api'
import { executionDuration, executionReason, executionSummary, activityKindLabel, relativeActivityTime, severityLabel, type ActivityFilter } from '../activity-ui'
import { ErrorPanel, LoadingPanel, PageHeader, RealtimePill } from '../components/Common'
import { Icon } from '../components/Icons'
import { ACTIVITY_PAGE_REFRESH, useApi, useRoomOSEvents } from '../hooks'
import type { ActivityCatalog, ActivityRecord, AlertCatalog } from '../types'

type ActivityAccess = { authorized: false } | { authorized: true; alerts: AlertCatalog; history: ActivityCatalog }

export function ActivityLocked() {
  return <article className="activity-locked surface"><span><Icon name="alert" /></span><h2>Activity access required</h2><p>System alerts and history stay private on explicitly authorized devices.</p></article>
}

export function ActivityRow({ item }: { item: ActivityRecord }) {
  const execution = item.execution
  if (!execution) return <article className={`activity-row surface is-${item.type}`}><span className="activity-row-icon"><Icon name={item.type.startsWith('scene_') ? 'pulse' : item.type === 'alert_resolved' ? 'power' : 'alert'} /></span><div><span>{activityKindLabel(item)}</span><h3>{item.title}</h3><p>{item.message}</p>{item.type.startsWith('scene_') && <p>Detail unavailable</p>}</div><time dateTime={item.occurred_at}>{relativeActivityTime(item.occurred_at)}</time></article>
  return <details className={`execution-row surface execution-${execution.result}`}>
    <summary><span className="activity-row-icon"><Icon name="pulse" /></span><span className="execution-heading"><strong>{execution.routine_name ?? execution.scene_name}</strong><span>{execution.scene_name} · <b className="execution-status">{execution.result}</b></span><span>{executionSummary(execution)}</span></span><span className="execution-time"><time dateTime={execution.started_at}>{relativeActivityTime(execution.started_at)}</time><span>{executionDuration(execution.duration_ms)}</span></span></summary>
    <div className="execution-detail"><p>{execution.source === 'routine' ? 'Routine' : 'Manual'} · {execution.trigger} · <time dateTime={execution.started_at}>{new Date(execution.started_at).toLocaleString()}</time></p>
      {execution.result === 'interrupted' && <p>Completion was not observed before Core restarted. Target outcomes may be incomplete; no retry was attempted.</p>}
      {execution.reason && <p>{executionReason(execution.reason)}</p>}
      <ul aria-label="Target execution details">{execution.targets.map(target => <li key={target.device_id}><strong>{item.device_names?.[target.device_id] ?? target.device_id}</strong><span>{target.result} · {executionReason(target.reason) || 'Applied'}</span><span>{target.stage} · {executionDuration(target.duration_ms)}</span><span>{target.actions.join(', ')}</span></li>)}</ul>
      {!execution.targets.length && <p>No target outcome recorded.</p>}
    </div>
  </details>
}

export function ActivityDashboard({ alerts, history, filter, onFilter }: { alerts: AlertCatalog; history: ActivityCatalog; filter: ActivityFilter; onFilter: (filter: ActivityFilter) => void }) {
  return <>
    <section className="activity-alerts" aria-labelledby="active-alerts-title">
      <div className="activity-section-head"><div><p className="eyebrow">Current state</p><h2 id="active-alerts-title">Active Alerts</h2></div><span className={`alert-count is-${alerts.highest_severity ?? 'clear'}`}>{alerts.active_count} active</span></div>
      {alerts.active.length === 0 ? <article className="activity-clear surface"><Icon name="pulse" /><div><strong>All systems operational</strong><p>No active RoomOS alerts.</p></div></article> : <div className="active-alert-grid">{alerts.active.map((alert) => <article className={`alert-card surface is-${alert.severity}`} key={alert.id}><div className="alert-card-head"><span><Icon name="alert" /></span><strong>{severityLabel(alert.severity)}</strong><time dateTime={alert.started_at}>{relativeActivityTime(alert.started_at)}</time></div><h3>{alert.title}</h3><p>{alert.message}</p></article>)}</div>}
    </section>
    <section className="activity-history" aria-labelledby="recent-activity-title">
      <div className="activity-section-head"><div><p className="eyebrow">Lifecycle history</p><h2 id="recent-activity-title">Recent Activity</h2></div><div className="activity-filters" aria-label="Activity filter">{(['all', 'alerts', 'scenes'] as const).map((item) => <button key={item} aria-pressed={filter === item} onClick={() => onFilter(item)}>{item === 'all' ? 'All' : item === 'alerts' ? 'Alerts' : 'Scenes'}</button>)}</div></div>
      {history.activity.length === 0 ? <article className="activity-empty surface"><Icon name="clock" /><div><strong>No recent activity</strong><p>Important alert and scene lifecycle events will appear here.</p></div></article> : <div className="activity-list">{history.activity.map((item) => <ActivityRow key={item.id} item={item} />)}</div>}
      {history.execution_storage_status && history.execution_storage_status !== 'ok' && <ErrorPanel message="Execution history storage needs operator attention. Light control remains available; recent details may not survive a restart." />}
      {(alerts.storage_status === 'corrupt' || history.storage_status === 'corrupt') && <ErrorPanel message="Alert history storage requires operator repair. Live evaluation remains fail-safe." />}
    </section>
  </>
}

export function ActivityPage() {
  const [filter, setFilter] = useState<ActivityFilter>('all')
  const loader = useCallback(async (): Promise<ActivityAccess> => {
    const auth = await api.authStatus()
    if (!auth.device?.scopes.includes('activity:read')) return { authorized: false }
    const [alerts, history] = await Promise.all([api.alerts(), api.activity(filter)])
    return { authorized: true, alerts, history }
  }, [filter])
  const { data, error, loading, refresh } = useApi(loader)
  const realtime = useRoomOSEvents(useCallback(() => undefined, []), refresh, { refreshOnEvent: ACTIVITY_PAGE_REFRESH })
  return <section className="page activity-page">
    <PageHeader eyebrow="System intelligence" title="Activity" subtitle="Actionable alerts and quiet, bounded history."><RealtimePill status={realtime} /></PageHeader>
    {error && <ErrorPanel message={error} />}
    {loading && <LoadingPanel label="Reading activity…" />}
    {data && !data.authorized && <ActivityLocked />}
    {data?.authorized && <ActivityDashboard alerts={data.alerts} history={data.history} filter={filter} onFilter={setFilter} />}
  </section>
}

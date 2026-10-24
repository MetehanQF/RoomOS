import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { relativeActivityTime, severityLabel } from '../activity-ui'
import { useAuthStatus } from '../auth-context'
import { ErrorPanel, LoadingPanel, PageHeader, RealtimePill } from '../components/Common'
import { Icon } from '../components/Icons'
import { NotificationSettingsPanel } from '../components/NotificationSettings'
import { AlertProfilesPanel } from '../components/AlertProfiles'
import { insightLines, snoozeLabel, sourceLabel } from '../alert-insight'
import { NOTIFICATION_PAGE_REFRESH, useApi, useRoomOSEvents } from '../hooks'
import type { AlertRecord, AlertSeverity, NotificationCatalog } from '../types'

type Filters = { status: 'all' | 'active' | 'resolved'; severity: AlertSeverity | ''; unread: 'all' | 'true' | 'false' }
type Access = { authorized: false } | { authorized: true; catalog: NotificationCatalog }
const NO_SCOPES: string[] = []

export function NotificationsLocked() {
  return <article className="notifications-locked surface"><span><Icon name="alert" /></span><h2>Notification access required</h2><p>Smart alerts stay private on devices with Activity access.</p></article>
}

type SnoozeHours = 2 | 8 | 24 | 168
const SNOOZE_HOURS: SnoozeHours[] = [2, 8, 24, 168]
const snoozeText = (hours: SnoozeHours) => hours === 168 ? ['7 d', '7 days'] : [`${hours} h`, `${hours} hours`]

function NotificationActions({ item, canAcknowledge, canSnooze, snoozed, onRead, onAcknowledge, onMute, onSnooze, onUnsnooze }: {
  item: AlertRecord; canAcknowledge: boolean; canSnooze: boolean; snoozed: boolean
  onRead: (id: string) => void; onAcknowledge: (id: string) => void; onMute: (id: string) => void
  onSnooze: (id: string, hours: SnoozeHours) => void; onUnsnooze: (id: string) => void
}) {
  return <div className="notification-actions">
    {!item.read_at && <button type="button" onClick={() => onRead(item.id)}>Mark read</button>}
    {canAcknowledge && item.status === 'active' && !item.acknowledged_at && <button type="button" onClick={() => onAcknowledge(item.id)}>Acknowledge</button>}
    {canAcknowledge && item.status === 'active' && <button type="button" onClick={() => onMute(item.id)}>Mute 1 hour</button>}
    {canSnooze && item.status === 'active' && (snoozed
      ? <button type="button" onClick={() => onUnsnooze(item.id)}>Remove snooze</button>
      : <div className="notification-snooze" role="group" aria-label="Snooze repeat notifications">{SNOOZE_HOURS.map((hours) => { const [short, long] = snoozeText(hours); return <button key={hours} type="button" aria-label={`Snooze ${long}`} onClick={() => onSnooze(item.id, hours)}>{short}</button> })}</div>)}
  </div>
}

export function NotificationCenterView({ catalog, canAcknowledge, onRead, onAcknowledge, onMute, focusId = null, canSnooze = false, onSnooze = () => undefined, onUnsnooze = () => undefined, now }: {
  catalog: NotificationCatalog; canAcknowledge: boolean
  onRead: (id: string) => void; onAcknowledge: (id: string) => void; onMute: (id: string) => void
  focusId?: string | null; canSnooze?: boolean; now?: number
  onSnooze?: (id: string, hours: SnoozeHours) => void; onUnsnooze?: (id: string) => void
}) {
  useEffect(() => {
    if (!focusId) return
    document.getElementById(`notification-${focusId}`)?.scrollIntoView?.({ block: 'center' })
  }, [focusId, catalog])
  return <>
    <section className="notification-signal-stack" aria-label="Notification summary">
      <div className={`notification-signal is-${catalog.highest_severity ?? 'clear'}`}><Icon name="alert" /><strong>{catalog.active_count}</strong><span>Active</span></div>
      <div className="notification-signal"><Icon name="pulse" /><strong>{catalog.unread_count}</strong><span>Unread</span></div>
      <div className="notification-signal"><Icon name="clock" /><strong>{catalog.total}</strong><span>Shown</span></div>
    </section>
    {!catalog.notifications.length ? <article className="notification-empty surface"><Icon name="pulse" /><div><strong>No notifications match</strong><p>Change the filters or wait for a trusted RoomOS signal.</p></div></article> :
      <div className="notification-timeline">{catalog.notifications.map((item) => <article key={item.id} id={`notification-${item.id}`} aria-current={item.id === focusId ? 'true' : undefined} className={`notification-item surface is-${item.severity} ${item.read_at ? 'is-read' : 'is-unread'}${item.id === focusId ? ' is-focused' : ''}`}>
        <span className="notification-rail" aria-hidden="true" />
        <div className="notification-item-head"><span className="notification-severity"><Icon name="alert" />{severityLabel(item.severity)}</span><span className={`notification-lifecycle is-${item.status}`}>{item.status === 'active' ? 'Active' : 'Resolved'}</span><time dateTime={item.updated_at}>{relativeActivityTime(item.updated_at)}</time></div>
        <h2>{item.title}</h2><p>{item.message}</p>
        {insightLines(catalog.insights?.[item.id]).length > 0 && item.status === 'active' && <ul className="notification-insight" aria-label="Evaluation">{insightLines(catalog.insights?.[item.id]).map((line) => <li key={line}>{line}</li>)}</ul>}
        <div className="notification-meta"><span>{sourceLabel(item.source)}</span>{catalog.snoozes?.[item.id] && <span className="notification-snoozed">{now === undefined ? 'Snoozed' : snoozeLabel(catalog.snoozes[item.id], now)}</span>}<span>{item.occurrence_count ?? 1} observation{(item.occurrence_count ?? 1) === 1 ? '' : 's'}</span>{item.acknowledged_at && <span>Acknowledged</span>}{item.muted_until && <span>Muted until {new Date(item.muted_until).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>}</div>
        <NotificationActions item={item} canAcknowledge={canAcknowledge} canSnooze={canSnooze} snoozed={Boolean(catalog.snoozes?.[item.id])} onRead={onRead} onAcknowledge={onAcknowledge} onMute={onMute} onSnooze={onSnooze} onUnsnooze={onUnsnooze} />
      </article>)}</div>}
    {catalog.storage_status === 'corrupt' && <ErrorPanel message="Notification history storage requires operator repair. Live monitoring remains fail-safe." />}
  </>
}

export function NotificationsPage() {
  const scopes = useAuthStatus()?.device?.scopes ?? NO_SCOPES
  const [params] = useSearchParams()
  const focusId = /^[a-z0-9._-]{1,80}$/.test(params.get('alert') ?? '') ? params.get('alert') : null
  const [showSettings, setShowSettings] = useState(false)
  // Snooze countdowns only: one re-render per minute on this page, never on the Panel.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 60000); return () => window.clearInterval(timer) }, [])
  const [filters, setFilters] = useState<Filters>({ status: 'all', severity: '', unread: 'all' })
  const loader = useCallback(async (): Promise<Access> => {
    if (!scopes.includes('activity:read')) return { authorized: false }
    return { authorized: true, catalog: await api.notifications(filters) }
  }, [filters, scopes])
  const { data, error, loading, refresh } = useApi(loader)
  const realtime = useRoomOSEvents(useCallback(() => undefined, []), refresh, { refreshOnEvent: NOTIFICATION_PAGE_REFRESH })
  const act = useCallback(async (action: (id: string) => Promise<AlertRecord>, id: string) => { await action(id); await refresh() }, [refresh])
  return <section className="page notifications-page">
    <PageHeader eyebrow="Operational signal" title="Notifications" subtitle="Current faults, recoveries, and reviewed alerts in one place."><RealtimePill status={realtime} /></PageHeader>
    {data?.authorized && <div className="notification-filterbar surface" aria-label="Notification filters">
      <label>Status<select value={filters.status} onChange={(event) => setFilters(current => ({ ...current, status: event.target.value as Filters['status'] }))}><option value="all">All</option><option value="active">Active</option><option value="resolved">Resolved</option></select></label>
      <label>Severity<select value={filters.severity} onChange={(event) => setFilters(current => ({ ...current, severity: event.target.value as Filters['severity'] }))}><option value="">All</option><option value="critical">Critical</option><option value="warning">Warning</option><option value="info">Info</option></select></label>
      <label>Read state<select value={filters.unread} onChange={(event) => setFilters(current => ({ ...current, unread: event.target.value as Filters['unread'] }))}><option value="all">All</option><option value="true">Unread</option><option value="false">Read</option></select></label>
      <Link to="/activity">Activity history</Link>
      <button type="button" className="notification-settings-toggle" aria-expanded={showSettings} onClick={() => setShowSettings((value) => !value)}>Notification settings</button>
    </div>}
    {data?.authorized && showSettings && <section className="surface push-settings-surface"><h2>Notification settings</h2><NotificationSettingsPanel /><AlertProfilesPanel canEdit={scopes.includes('alerts:profile')} /></section>}
    {error && <ErrorPanel message={error} />}{loading && <LoadingPanel label="Reading notifications…" />}
    {data && !data.authorized && <NotificationsLocked />}
    {data?.authorized && <NotificationCenterView catalog={data.catalog} canAcknowledge={scopes.includes('alerts:ack')}
      onRead={(id) => void act(api.markNotificationRead, id)} onAcknowledge={(id) => void act(api.acknowledgeNotification, id)} onMute={(id) => void act((value) => api.muteNotification(value, 60), id)} focusId={focusId} now={now}
      canSnooze={scopes.includes('alerts:snooze')} onSnooze={(id, hours) => void act(async (value) => { await api.snoozeNotification(value, hours); return {} as AlertRecord }, id)}
      onUnsnooze={(id) => void act(async (value) => { await api.unsnoozeNotification(value); return {} as AlertRecord }, id)} />}
  </section>
}

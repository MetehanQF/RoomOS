import type { CSSProperties, ReactNode } from 'react'
import { Icon, type IconName } from './Icons'
import { formatValue } from '../ui'
import type { CommandFeedback, RealtimeStatus, TelemetryFreshness } from '../types'

export function PageHeader({ eyebrow, title, subtitle, children }: { eyebrow: string; title: string; subtitle?: string; children?: ReactNode }) {
  return <header className="page-header"><div className="page-heading"><p className="eyebrow">{eyebrow}</p><h1>{title}</h1>{subtitle && <p className="page-subtitle">{subtitle}</p>}</div>{children}</header>
}

export function StatusPill({ status, label }: { status: 'healthy' | 'degraded' | 'online' | 'offline' | 'unknown' | 'stale' | 'locked'; label?: string }) {
  const fallback = status === 'healthy' ? 'Healthy' : status === 'degraded' ? 'Degraded' : status === 'online' ? 'Online' : status === 'offline' ? 'Offline' : status === 'stale' ? 'Stale' : status === 'locked' ? 'Locked' : 'Unknown'
  return <span className={`status-pill ${status}`}><span className="status-indicator" />{label ?? fallback}</span>
}

export function State({ value, stale = false }: { value: boolean | null; stale?: boolean }) {
  if (stale) return <StatusPill status="stale" />
  return <StatusPill status={value === null ? 'unknown' : value ? 'online' : 'offline'} />
}

export function RealtimePill({ status }: { status: RealtimeStatus }) {
  return <StatusPill status={status === 'connected' ? 'online' : status === 'offline' ? 'offline' : 'unknown'} label={status === 'connected' ? 'Live' : status === 'reconnecting' ? 'Reconnecting' : 'Offline'} />
}

export function ErrorPanel({ message }: { message: string }) {
  return <div className="notice error" role="alert"><span className="notice-icon">!</span><div><strong>Live data unavailable</strong><p>{message}</p></div></div>
}

export function CommandNotice({ feedback, onDismiss }: { feedback: CommandFeedback | null; onDismiss?: () => void }) {
  if (!feedback) return null
  return <div className={`notice command ${feedback.tone}`} role={feedback.tone === 'error' || feedback.tone === 'partial' ? 'alert' : 'status'}><span className="notice-icon">{feedback.tone === 'pending' ? '…' : feedback.tone === 'success' ? '✓' : feedback.tone === 'partial' ? '!' : '×'}</span><div><strong>{feedback.title}</strong>{feedback.detail && <p>{feedback.detail}</p>}</div>{onDismiss && feedback.tone !== 'pending' && <button className="notice-dismiss" onClick={onDismiss} aria-label="Dismiss">×</button>}</div>
}

export function Metric({ label, value, unit, freshness = 'unknown', icon, progress, updatedAt }: { label: string; value: unknown; unit?: string | null; freshness?: TelemetryFreshness; icon?: IconName; progress?: number | null; updatedAt?: string | null }) {
  const safeProgress = progress === null || progress === undefined ? null : Math.max(0, Math.min(100, progress))
  const freshnessLabel = freshness === 'fresh' ? 'Fresh' : freshness === 'stale' ? 'Stale' : freshness === 'unavailable' ? 'Unavailable' : 'Unknown'
  return <div className={`metric-tile is-${freshness}`} style={safeProgress === null ? undefined : { '--metric-progress': `${safeProgress}%` } as CSSProperties}>
    <div className="metric-label">{icon && <Icon name={icon} />}<span>{label}</span><span className={`freshness-tag is-${freshness}`}>{freshnessLabel}</span></div>
    <strong>{formatValue(value, unit)}</strong>
    {safeProgress !== null && <div className="metric-bar"><span /></div>}
    {updatedAt && <small className="metric-updated">State changed {new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(updatedAt))}</small>}
  </div>
}

export function LoadingPanel({ label = 'Connecting to RoomOS…' }: { label?: string }) {
  return <div className="loading-panel" role="status"><span className="loader-orb" /><div><strong>{label}</strong><span>Syncing live state</span></div></div>
}

export function SectionHeading({ icon, title, detail, children }: { icon?: IconName; title: string; detail?: string; children?: ReactNode }) {
  return <div className="section-heading"><div className="section-title-wrap">{icon && <span className="section-icon"><Icon name={icon} /></span>}<div><h2>{title}</h2>{detail && <p>{detail}</p>}</div></div>{children}</div>
}

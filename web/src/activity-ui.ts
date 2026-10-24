import type { ActivityRecord, AlertSeverity } from './types'

export type ActivityFilter = 'all' | 'alerts' | 'scenes'

export function severityLabel(severity: AlertSeverity | null) {
  if (severity === 'critical') return 'Critical'
  if (severity === 'warning') return 'Warning'
  return severity === 'info' ? 'Info' : 'Activity'
}

export function activityKindLabel(item: ActivityRecord) {
  if (item.type === 'alert_started') return `${severityLabel(item.severity)} started`
  if (item.type === 'alert_resolved') return 'Resolved'
  return 'Scene'
}

export function relativeActivityTime(value: string, now = new Date()) {
  const elapsed = Math.max(0, now.getTime() - new Date(value).getTime())
  const minutes = Math.floor(elapsed / 60_000)
  if (minutes < 1) return 'Just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} hr ago`
  const days = Math.floor(hours / 24)
  return `${days} day${days === 1 ? '' : 's'} ago`
}

const reasonLabels: Record<string, string> = {
  unavailable: 'Unavailable', ha_unavailable: 'State unavailable', capability_rejected: 'Unsupported capability',
  power_on_not_confirmed: 'Power on not confirmed', ha_error: 'Home Assistant error', policy_rejected: 'Policy rejected',
  invalid_action: 'Invalid action', not_configured: 'Not configured', duplicate: 'Duplicate suppressed',
  control_disabled: 'Control disabled', internal_error: 'Internal error', interrupted: 'Interrupted',
}
export function executionReason(reason: string | null) { return reason ? reasonLabels[reason] ?? 'Unknown result' : '' }
export function executionDuration(ms: number | null) { return ms === null ? 'In progress' : `${(ms / 1000).toFixed(1)} s` }
export function executionSummary(execution: NonNullable<ActivityRecord['execution']>) {
  const unavailable = execution.targets.filter(target => target.reason === 'unavailable').length
  if (unavailable) return `${unavailable} device${unavailable === 1 ? '' : 's'} unavailable`
  const failed = execution.targets.filter(target => target.result === 'failed').length
  if (failed) return `${failed} device${failed === 1 ? '' : 's'} failed`
  return executionReason(execution.reason) || (execution.result === 'success' ? 'All targets applied' : 'Execution details')
}

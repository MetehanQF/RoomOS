import type { HealthComponent, HealthReport, HealthStatus } from './types'

const IDS = ['home_assistant', 'control', 'persistent_state', 'automations', 'execution_history', 'notifications', 'telemetry_history', 'backups', 'background_services']
const STATUSES: HealthStatus[] = ['healthy', 'degraded', 'unhealthy', 'unknown']

export class HealthContractError extends Error {
  constructor() {
    super('RoomOS Core returned an incompatible health response.')
    this.name = 'HealthContractError'
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const isText = (value: unknown, max: number) => typeof value === 'string' && value.length > 0 && value.length <= max
const isTime = (value: unknown) => typeof value === 'string' && !Number.isNaN(Date.parse(value))

/** Strict contract check: bounded, server-owned fields only. */
export function parseHealth(payload: unknown): HealthReport {
  if (!isRecord(payload) || Object.keys(payload).some((key) => !['overall', 'checked_at', 'components'].includes(key))
    || !['healthy', 'degraded', 'unhealthy'].includes(payload.overall as string) || !isTime(payload.checked_at)
    || !Array.isArray(payload.components) || payload.components.length > IDS.length) throw new HealthContractError()
  for (const item of payload.components) {
    if (!isRecord(item) || !IDS.includes(item.id as string) || !STATUSES.includes(item.status as HealthStatus)
      || !isText(item.name, 40) || !isText(item.summary, 120) || typeof item.critical !== 'boolean' || !isTime(item.checked_at)
      || !Array.isArray(item.details) || item.details.length > 8 || !item.details.every((detail) => isText(detail, 120))) throw new HealthContractError()
  }
  return payload as HealthReport
}

export const HEALTH_LABEL: Record<HealthStatus, string> = { healthy: 'Healthy', degraded: 'Degraded', unhealthy: 'Unhealthy', unknown: 'Not monitored' }

export function overallHeadline(report: HealthReport) {
  const attention = report.components.filter((item) => item.status === 'degraded' || item.status === 'unhealthy')
  if (!attention.length) return 'RoomOS is operating normally'
  if (report.overall === 'unhealthy') return `${attention.length} subsystem${attention.length === 1 ? '' : 's'} need${attention.length === 1 ? 's' : ''} attention · RoomOS impaired`
  return `${attention.length} subsystem${attention.length === 1 ? '' : 's'} need${attention.length === 1 ? 's' : ''} attention`
}

/** Problems first, then healthy, then not-monitored; stable within each group. */
export function orderComponents(components: HealthComponent[]) {
  const rank: Record<HealthStatus, number> = { unhealthy: 0, degraded: 1, healthy: 2, unknown: 3 }
  return components.map((item, index) => ({ item, index })).sort((a, b) => rank[a.item.status] - rank[b.item.status] || a.index - b.index).map(({ item }) => item)
}

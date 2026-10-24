import type { Health, Light, Metric, ServiceStatus, SystemData, SystemNode } from './types'

export type DashboardStatus = 'healthy' | 'degraded' | 'offline' | 'unknown' | 'stale'

export type DashboardService = {
  id: 'home_assistant' | 'frigate' | 'dns' | 'nextcloud'
  name: string
  status: DashboardStatus
}

const nodeMetricNames = ['cpu', 'ram', 'temperature'] as const

export function nodeMetric(node: SystemNode, name: typeof nodeMetricNames[number]) {
  return node.metrics[`${node.id}_${name}`]
}

export function nodeDashboardStatus(node: SystemNode): DashboardStatus {
  if (node.online === false) return 'offline'
  if (node.online === null) return 'unknown'
  const onlineMetric = node.metrics[`${node.id}_online`]
  const metrics = nodeMetricNames.map((name) => nodeMetric(node, name))
  if (onlineMetric?.freshness === 'stale' || metrics.some((metric) => metric?.freshness === 'stale')) return 'stale'
  if (metrics.some((metric) => !metric?.available || metric.value === null)) return 'degraded'
  return 'healthy'
}

export function metricDashboardStatus(metric: Metric | undefined): DashboardStatus {
  if (!metric || metric.freshness === 'unknown' || metric.freshness === 'unavailable') return 'unknown'
  return metric.freshness === 'stale' ? 'stale' : 'healthy'
}

export function serviceDashboardStatus(service: ServiceStatus | undefined): DashboardStatus {
  if (!service || service.status === 'unknown') return 'unknown'
  if (service.status === 'offline') return 'offline'
  if (service.health === 'degraded') return 'degraded'
  if (service.health === 'unknown') return 'unknown'
  if (service.freshness === 'stale') return 'stale'
  return 'healthy'
}

export function systemServices(system: SystemData): DashboardService[] {
  const byId = new Map(system.services.map((service) => [service.id, service]))
  return [
    { id: 'home_assistant', name: 'Home Assistant', status: serviceDashboardStatus(byId.get('home_assistant')) },
    { id: 'frigate', name: 'Frigate', status: serviceDashboardStatus(byId.get('frigate')) },
    { id: 'dns', name: 'DNS', status: serviceDashboardStatus(byId.get('dns')) },
    { id: 'nextcloud', name: 'Nextcloud', status: serviceDashboardStatus(byId.get('nextcloud')) },
  ]
}

export function homeServices(system: SystemData, health: Health): DashboardService[] {
  const haStatus: DashboardStatus = health.home_assistant === 'connected'
    ? 'healthy'
    : health.home_assistant === 'disconnected'
      ? 'offline'
      : 'unknown'
  return systemServices(system).map((service) => service.id === 'home_assistant' ? { ...service, status: haStatus } : service)
}

export function overallDashboardStatus(
  health: Health,
  lights: Light[],
  system: SystemData,
): DashboardStatus {
  if (health.alerts.status === 'critical') return 'offline'
  if (health.alerts.status === 'attention') return 'degraded'
  if (health.alerts.status === 'operational') return 'healthy'
  if (health.home_assistant === 'disconnected') return 'offline'
  const statuses: DashboardStatus[] = [
    nodeDashboardStatus(system.primary),
    nodeDashboardStatus(system.secondary),
    ...homeServices(system, health).map((service) => service.status),
  ]
  if (statuses.includes('offline') || lights.some((light) => !light.available)) return 'degraded'
  if (statuses.includes('stale')) return 'stale'
  if (statuses.includes('unknown')) return 'unknown'
  if (statuses.includes('degraded')) return 'degraded'
  return 'healthy'
}

export function dashboardStatusLabel(status: DashboardStatus) {
  if (status === 'healthy') return 'Healthy'
  if (status === 'degraded') return 'Degraded'
  if (status === 'offline') return 'Offline'
  if (status === 'stale') return 'Stale'
  return 'Unknown'
}

export function overallStatusLabel(status: DashboardStatus) {
  if (status === 'healthy') return 'All systems operational'
  return status === 'offline' ? 'Critical issue' : 'Attention needed'
}

export function formatLocalClock(date: Date) {
  return {
    time: new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(date),
    date: new Intl.DateTimeFormat(undefined, { weekday: 'long', day: 'numeric', month: 'long' }).format(date),
  }
}

import { useCallback } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { SYSTEM_PAGE_REFRESH, useApi, useRoomOSEvents } from '../hooks'
import { ErrorPanel, LoadingPanel, Metric, PageHeader, RealtimePill, SectionHeading, StatusPill } from '../components/Common'
import { Icon } from '../components/Icons'
import { TrendsSection } from '../components/Trends'
import { SystemOverview } from '../components/SystemOverview'
import { SystemEvents } from '../components/SystemEvents'
import { HealthSection } from '../components/Health'
import { dashboardStatusLabel, nodeDashboardStatus, systemServices } from '../home-dashboard'
import { releaseMatches, shortRevision, WEB_RELEASE_INFO, type WebReleaseInfo } from '../release'
import type { IconName } from '../components/Icons'
import type { DnsClusterStatus, DnsResolverStatus, Metric as TelemetryMetric, ReleaseInfo, SystemNode, TelemetryFreshness } from '../types'

type MetricDefinition = { key: string; label: string; icon: IconName; progress?: boolean; booleanKind?: 'service' | 'problem' }

const commonMetrics: MetricDefinition[] = [
  { key: 'cpu', label: 'CPU', icon: 'cpu', progress: true },
  { key: 'ram', label: 'RAM', icon: 'memory', progress: true },
  { key: 'disk', label: 'Disk', icon: 'disk', progress: true },
  { key: 'temperature', label: 'Temperature', icon: 'thermometer' },
  { key: 'uptime', label: 'Uptime', icon: 'pulse' },
]

const nodeMetrics: Record<SystemNode['id'], MetricDefinition[]> = {
  primary: [...commonMetrics, { key: 'docker', label: 'Docker', icon: 'server', booleanKind: 'service' }, { key: 'tailscale', label: 'Tailscale', icon: 'wifi', booleanKind: 'service' }, { key: 'throttled', label: 'Throttling', icon: 'pulse', booleanKind: 'problem' }],
  secondary: [...commonMetrics, { key: 'docker', label: 'Docker', icon: 'server', booleanKind: 'service' }, { key: 'smart', label: 'SMART', icon: 'disk', booleanKind: 'problem' }, { key: 'portainer', label: 'Portainer', icon: 'server', booleanKind: 'service' }],
}

function displayMetric(metric: TelemetryMetric | undefined, definition: MetricDefinition, online: boolean | null) {
  if (!metric) return { value: null, unit: null, progress: null, freshness: 'unknown' as TelemetryFreshness, updatedAt: null }
  const freshness: TelemetryFreshness = online === false && metric.available ? 'stale' : metric.freshness
  if (online === false || !metric.available) return { value: null, unit: null, progress: null, freshness, updatedAt: metric.last_updated }
  if (definition.booleanKind === 'problem') return { value: metric.value === true ? (definition.key === 'throttled' ? 'Throttled' : 'Problem') : 'Normal', unit: null, progress: null, freshness, updatedAt: metric.last_updated }
  if (definition.booleanKind === 'service') return { value: metric.value === true ? 'Online' : 'Offline', unit: null, progress: null, freshness, updatedAt: metric.last_updated }
  return { value: metric.value, unit: metric.unit, progress: definition.progress && typeof metric.value === 'number' ? metric.value : null, freshness, updatedAt: metric.last_updated }
}

export function NodePanel({ node, label }: { node: SystemNode; label: string }) {
  const get = (name: string) => node.metrics[`${node.id}_${name}`]
  const definitions = nodeMetrics[node.id]
  const known = definitions.filter(({ key }) => get(key)?.available).length
  const status = nodeDashboardStatus(node)
  const summary = node.online === false ? 'No response' : node.online === null ? 'State unknown' : status === 'stale' ? 'Telemetry stale' : status === 'degraded' ? 'Some metrics unavailable' : 'Reporting normally'
  const statusLabel = node.online === false ? 'Offline' : node.online === null ? 'Unknown' : status === 'healthy' ? 'Online' : `Online · ${dashboardStatusLabel(status)}`
  return <article className="node-panel surface"><div className="node-panel-head"><div className="node-name"><span><Icon name="server" /></span><div><p>{label}</p><h2>{node.name}</h2></div></div><StatusPill status={status} label={statusLabel} /></div><div className="node-summary"><span><Icon name="pulse" />{known}/{definitions.length} metrics available</span><span>{summary}</span></div><div className="node-metrics">{definitions.map((definition) => { const metric = displayMetric(get(definition.key), definition, node.online); return <Metric key={definition.key} label={definition.label} value={metric.value} unit={metric.unit} freshness={metric.freshness} icon={definition.icon} progress={metric.progress} updatedAt={metric.updatedAt} /> })}</div></article>
}

function checkedLabel(value: string | null, now = Date.now()) {
  if (!value) return 'Not checked yet'
  const seconds = Math.max(0, Math.round((now - new Date(value).getTime()) / 1000))
  if (seconds < 60) return `Checked ${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  return `Checked ${minutes}m ago`
}

function ResolverRow({ resolver, label }: { resolver: DnsResolverStatus; label: string }) {
  return <div className="dns-resolver-row"><div><strong>{label}</strong><small>{resolver.response_time_ms === null ? 'Response unavailable' : `Response ${resolver.response_time_ms.toFixed(1)} ms`}</small></div><StatusPill status={resolver.status} /></div>
}

export function ReleaseIdentityPanel({ core, web = WEB_RELEASE_INFO }: { core: ReleaseInfo | null; web?: WebReleaseInfo }) {
  const matches = core !== null && releaseMatches(core, web)
  return <article className={`release-identity surface ${matches ? 'is-matched' : 'is-mismatch'}`} data-release-match={matches}>
    <div><p className="eyebrow">Release identity</p><h3>RoomOS {web.release}</h3><code>{shortRevision(web.revision)}</code></div>
    {core === null
      ? <p role="alert">Core release identity unavailable.</p>
      : matches
        ? <p>Core and Web revisions match.</p>
        : <p role="alert">Core/Web release mismatch: Core {core.release} {shortRevision(core.revision)}, Web {web.release} {shortRevision(web.revision)}.</p>}
  </article>
}

export function DnsClusterPanel({ cluster }: { cluster: DnsClusterStatus }) {
  return <article className="dns-cluster-card surface" aria-label="DNS Cluster health"><div className="dns-cluster-head"><div><span className="service-card-icon"><Icon name="wifi" /></span><div><p className="eyebrow">Functional probes</p><h3>DNS Cluster</h3></div></div><StatusPill status={cluster.status} /></div><div className="dns-resolver-list"><ResolverRow resolver={cluster.primary} label="Primary" /><ResolverRow resolver={cluster.secondary} label="Secondary" /></div><p className="dns-checked">{checkedLabel(cluster.last_checked)}</p></article>
}

export function SystemPage() {
  const [params] = useSearchParams()
  const sections=['Overview','Services','Network & DNS','Events']
  const tab=sections.includes(params.get('tab')??'')?params.get('tab'):'Overview'
  const loader = useCallback(() => api.system(), [])
  const { data, error, loading, refresh } = useApi(loader)
  const releaseLoader = useCallback(() => api.version(), [])
  const { data: coreRelease, refresh: refreshRelease } = useApi(releaseLoader)
  const refreshAll = useCallback(async () => {
    await Promise.all([refresh(), refreshRelease()])
  }, [refresh, refreshRelease])
  const realtime = useRoomOSEvents(useCallback(() => undefined, []), refreshAll, { refreshOnEvent: SYSTEM_PAGE_REFRESH })
  return <section className="page system-page">
    <PageHeader eyebrow="Infrastructure" title="System" subtitle="Quiet confidence in the systems behind your room."><RealtimePill status={realtime} /></PageHeader>
    <nav className="monitor-tabs" aria-label="System sections">{sections.map(name=><Link key={name} to={`?tab=${encodeURIComponent(name)}`} aria-current={tab===name?'page':undefined}>{name}</Link>)}</nav>
    {error && <ErrorPanel message={error} />}{loading && <LoadingPanel label="Reading system health…" />}
    {data && <>
      {tab==='Overview'&&<SystemOverview data={data}/>}
      {tab==='Services'&&<>
      <SectionHeading icon="pulse" title="Services" detail="Canonical RoomOS health model" />
      <div className="services-grid">{systemServices(data).filter((service) => service.id !== 'dns').map((service) => <article className="service-card surface" key={service.id}><span className="service-card-icon"><Icon name={service.id === 'home_assistant' ? 'home' : service.id === 'frigate' ? 'camera' : 'cloud'} /></span><div><h3>{service.name}</h3><p>{data.services.find((item) => item.id === service.id)?.source ?? 'No approved source'}</p></div><StatusPill status={service.status} /></article>)}</div>
      <details className="surface monitor-notice"><summary>Host diagnostics · Docker, SMART, throttling and connectivity</summary><div className="system-grid"><NodePanel node={data.primary} label="Primary host" /><NodePanel node={data.secondary} label="Companion machine" /></div></details>
      <TrendsSection />
      <ReleaseIdentityPanel core={coreRelease} />
      </>}
      {tab==='Network & DNS'&&<><DnsClusterPanel cluster={data.dns_cluster}/><article className="surface monitor-notice"><h3>Evidence, not assumptions</h3><p>Functional probe results measure DNS resolution. They do not prove individual upstream reachability or explain a historical host outage.</p><p>Container state and host telemetry are separate signals. Historical Secondary journal gaps cannot establish a root cause.</p></article></>}
      {tab==='Events'&&<SystemEvents/>}
    </>}
    {tab==='Overview'&&<HealthSection />}
  </section>
}

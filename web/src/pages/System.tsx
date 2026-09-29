import { useCallback } from 'react'
import { api } from '../api'
import { useApi, useRoomOSEvents } from '../hooks'
import { ErrorPanel, LoadingPanel, Metric, PageHeader, RealtimePill, SectionHeading, State, StatusPill } from '../components/Common'
import { Icon } from '../components/Icons'
import { serviceName } from '../ui'
import type { SystemNode } from '../types'

const metricDefinitions = [
  { key: 'cpu', label: 'CPU load', icon: 'cpu' as const, progress: true },
  { key: 'ram', label: 'Memory', icon: 'memory' as const, progress: true },
  { key: 'disk', label: 'Disk', icon: 'disk' as const, progress: true },
  { key: 'temperature', label: 'Temperature', icon: 'thermometer' as const, progress: false },
  { key: 'uptime', label: 'Uptime', icon: 'pulse' as const, progress: false },
]

function NodePanel({ node }: { node: SystemNode }) {
  const get = (name: string) => node.metrics[`${node.id}_${name}`]
  const known = metricDefinitions.filter(({ key }) => get(key)?.available).length
  return <article className="node-panel surface"><div className="node-panel-head"><div className="node-name"><span><Icon name="server" /></span><div><p>Node</p><h2>{node.name}</h2></div></div><State value={node.online} /></div><div className="node-summary"><span><Icon name="pulse" />{known}/{metricDefinitions.length} metrics available</span><span>{node.online === null ? 'State unknown' : node.online ? 'Reporting normally' : 'No response'}</span></div><div className="node-metrics">{metricDefinitions.map(({ key, label: metricLabel, icon, progress }) => { const metric = get(key); const numeric = typeof metric?.value === 'number' ? metric.value : null; return <Metric key={key} label={metricLabel} value={metric?.value} unit={metric?.unit} stale={metric?.stale} icon={icon} progress={progress ? numeric : null} /> })}</div></article>
}

export function SystemPage() {
  const loader = useCallback(() => api.system(), [])
  const { data, error, loading, refresh } = useApi(loader)
  const realtime = useRoomOSEvents(useCallback(() => undefined, []), refresh)
  return <section className="page system-page">
    <PageHeader eyebrow="Infrastructure" title="System" subtitle="Quiet confidence in the systems behind your room."><RealtimePill status={realtime} /></PageHeader>
    {error && <ErrorPanel message={error} />}{loading && <LoadingPanel label="Reading system health…" />}
    {data && <>
      <div className="system-overview"><div><span className="overview-icon"><Icon name="pulse" /></span><div><p className="eyebrow">Live monitoring</p><h2>{Object.values(data.nodes).filter((node) => node.online).length} of {Object.keys(data.nodes).length} nodes online</h2></div></div><p>Current values · stale and unknown data stays visible</p></div>
      <div className="system-grid">{Object.values(data.nodes).map((node) => <NodePanel key={node.id} node={node} />)}</div>
      <SectionHeading icon="pulse" title="Service health" />
      <div className="services-grid">{data.services.map((service) => <article className="service-card surface" key={service.id}><span className="service-card-icon"><Icon name={service.id === 'home_assistant' ? 'home' : 'server'} /></span><div><h3>{serviceName(service.id)}</h3><p>{service.status === 'unknown' ? 'Unknown' : service.source}</p></div><StatusPill status={service.status} /></article>)}</div>
    </>}
  </section>
}

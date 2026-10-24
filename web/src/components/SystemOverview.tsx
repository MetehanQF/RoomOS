import { Link } from 'react-router-dom'
import { useMonitoringAlerts } from '../monitoring-alerts'
import { StatusPill } from './Common'
import { dashboardStatusLabel, nodeDashboardStatus, systemServices } from '../home-dashboard'
import type { SystemData } from '../types'

export function SystemOverview({data}: {data:SystemData}) {
  const alerts=useMonitoringAlerts()
  const active=alerts.data?.notifications.filter(a=>a.status==='active')??[]
  return <>
    <div className="infrastructure-summary surface"><div><small>Infrastructure</small><h2>{[data.primary.online,data.secondary.online].filter(Boolean).length} of 2 nodes online</h2></div><div><Link to="/notifications">{active.filter(a=>a.severity==='critical').length} critical · {active.filter(a=>a.severity==='warning').length} warning →</Link>{(!alerts.allowed||alerts.error)&&<small>Alarm status unavailable</small>}</div></div>
    <div className="infrastructure-nodes">{[data.primary,data.secondary].map(node=><Link className="surface infrastructure-node" to={`/devices/${node.id}`} key={node.id}><div><h3>{node.name}</h3><span>Device details →</span></div><StatusPill status={nodeDashboardStatus(node)} label={dashboardStatusLabel(nodeDashboardStatus(node))}/></Link>)}</div>
    <div className="infrastructure-signals">{systemServices(data).map(service=><div className="surface infrastructure-node" key={service.id}><h3>{service.name}</h3><StatusPill status={service.status}/></div>)}<div className="surface infrastructure-node"><div><h3>MQTT</h3><small>No direct broker probe</small></div><StatusPill status="unknown"/></div></div>
    {active.slice(0,3).map(a=><Link className={`surface infrastructure-alert is-${a.severity}`} to={`/notifications?focus=${encodeURIComponent(a.id)}`} key={a.id}><strong>{a.severity} · {a.title}</strong><span>{a.message} →</span></Link>)}
  </>
}

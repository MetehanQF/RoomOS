import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useAuthStatus } from '../auth-context'
import { DeviceCard, SensorTile } from '../components/DeviceCard'
import { ErrorPanel, LoadingPanel, PageHeader, RealtimePill } from '../components/Common'
import { MetricHistory } from '../components/MetricHistory'
import { DiskForecast } from '../components/DiskForecast'
import { DevicePermissionPanel } from './Devices'
import { useDevicesSnapshot, useNow } from '../devices-hooks'
import { sensorMeta } from '../devices-state'
import { DEVICE_TABS as tabs, sensorsForTab, type DeviceTab } from '../device-tabs'

function Detail() {
  const {deviceId}=useParams()
  const [search,setSearch]=useSearchParams()
  const selected=search.get('tab')??'Overview'
  const tab:DeviceTab=tabs.includes(selected as DeviceTab)?selected as DeviceTab:'Overview'
  const {snapshot,retry}=useDevicesSnapshot()
  const now=useNow(15000)
  const device=snapshot.devices.find(d=>d.id===deviceId)
  if(snapshot.status==='loading'&&!device) return <LoadingPanel label="Reading device…"/>
  if(!device) return <><Link className="monitor-back" to="/devices">← Devices</Link><ErrorPanel message={snapshot.status==='error'?'Device data unavailable.':'This device is unknown or unavailable to this session.'}/><button onClick={retry}>Retry</button></>
  const inTab=sensorsForTab(device,tab)
  const shown=inTab.filter(s=>s.support!=='unsupported')
  const unsupported=inTab.filter(s=>s.support==='unsupported')
  return <>
    <Link className="monitor-back" to="/devices">← Devices</Link>
    <PageHeader eyebrow="Device detail" title={device.name} subtitle={`${device.platform} · ${device.id}`}><RealtimePill status={snapshot.connection}/></PageHeader>
    <nav className="monitor-tabs" aria-label="Device sections">{tabs.map(t=><Link key={t} to={`?tab=${encodeURIComponent(t)}`} aria-current={tab===t?'page':undefined} onClick={e=>{e.preventDefault();setSearch({tab:t})}}>{t}</Link>)}</nav>
    {snapshot.status==='error'&&<ErrorPanel message="Live data unavailable; retained values are not current."/>}
    {tab==='Overview'&&<DeviceCard device={{...device,sensors:inTab}} trusted={snapshot.trusted} now={now}/>}
    {(tab==='Performance'||tab==='Storage')&&<>
      {!shown.length&&<p className="monitor-notice surface">This device does not report these measurements.</p>}
      {unsupported.length>0&&<details className="device-unsupported surface monitor-notice"><summary>Not reported by this device · {unsupported.length}</summary><ul>{unsupported.map(s=><li key={s.id}>{sensorMeta(s,device).label}</li>)}</ul></details>}
      <div className="monitor-detail-grid">{shown.map(s=><SensorTile key={s.id} sensor={s} device={device} trusted={snapshot.trusted} now={now}/>)}</div>
      {tab==='Storage'&&shown.some(s=>s.id==='disk_usage'&&s.support==='supported')&&<DiskForecast deviceId={device.id}/>}
      {shown.filter(s=>s.support==='supported'&&s.kind==='number').map(s=><MetricHistory key={s.id} deviceId={device.id} metric={s.id} label={sensorMeta(s,device).label}/>)}
    </>}
    {tab==='Network'&&<><DeviceCard device={{...device,sensors:inTab}} trusted={snapshot.trusted} now={now}/><details className="surface monitor-notice"><summary>Source capabilities</summary>{device.sources.map(s=><p key={s.kind}>{s.kind} · {s.configured?'Configured':'Not configured'} · {s.connection}</p>)}<p>Unreported sensor values are not inferred.</p></details></>}
    {tab==='Alerts & Events'&&<div className="surface monitor-notice"><p>Alarm lifecycle, snooze and profile controls remain in Notification Center.</p><Link className="monitor-back" to="/notifications">Open Notification Center →</Link></div>}
  </>
}

export function DeviceDetailPage() {
  const allowed=useAuthStatus()?.device?.scopes.some(s=>s==='devices:read'||s==='system:read')??false
  return <section className="page device-detail-page">{allowed?<Detail/>:<DevicePermissionPanel/>}</section>
}

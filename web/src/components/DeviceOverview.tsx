import { Link } from 'react-router-dom'
import { StatusPill } from './Common'
import { Icon } from './Icons'
import { connectionLabel, effectiveConnection, effectiveFreshness, relativeTime, sensorMeta, sensorText } from '../devices-state'
import type { AlertRecord, DeviceDetail } from '../types'

export function DeviceOverviewRow({device, trusted, now, alerts = []}: {device: DeviceDetail; trusted: boolean; now: number; alerts?: AlertRecord[]}) {
  const connection = effectiveConnection(device, trusted, now)
  const active = alerts.filter(a => a.source === device.id && a.status === 'active')
  const severity = active.some(a => a.severity === 'critical') ? 'critical' : active.some(a => a.severity === 'warning') ? 'warning' : null
  const metrics = device.sensors.filter(s => ['cpu_usage','ram_usage','battery_percent'].includes(s.id) && s.support !== 'unsupported').slice(0,3)
  return <Link to={`/devices/${device.id}`} className={`device-overview-row surface ${severity ? `is-${severity}` : ''}`} data-device={device.id}>
    <span className="device-row-icon"><Icon name={device.type === 'windows' ? 'laptop' : device.type === 'linux' ? 'server' : 'phone'} /></span>
    <div className="device-row-main"><h2>{device.name}</h2><small>{device.platform} · {connection === 'online' ? `Last report ${relativeTime(device.last_seen_at, now)}` : 'Values not current'}</small>
      <div className="device-row-metrics">{metrics.map(s => <span key={s.id} data-sensor={s.id}>{sensorMeta(s,device).label} <strong>{sensorText(s,effectiveFreshness(s,device,{now,trusted}),device).text}</strong></span>)}</div>
    </div>
    <div className="device-row-status"><StatusPill status={connection} label={connectionLabel(connection)} />
      {severity ? <span className={`alert-count is-${severity}`}>{active.length} {severity}</span> : <small>{connection === 'online' ? device.health : 'Health unknown'}</small>}
      <span className="device-row-detail">Details →</span>
    </div>
  </Link>
}

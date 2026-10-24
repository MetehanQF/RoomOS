import { memo, type CSSProperties } from 'react'
import { Icon, type IconName } from './Icons'
import { StatusPill } from './Common'
import {
  connectionDetail, connectionLabel, diskWarning, effectiveConnection, effectiveFreshness, partitionSensors,
  lastMeasured, relativeTime, sensorMeta, sensorText, telemetryConnected,
} from '../devices-state'
import type { DeviceDetail, DeviceFreshness, DeviceSensor, PairedClient } from '../types'

/* Device Center building blocks (7D.4). Reusable for 7D.3 Android telemetry: everything is
   driven by the registry's sensor list, never by device-specific assumptions about values. */

const PRIMARY = ['cpu_usage', 'ram_usage', 'gpu_usage', 'disk_usage']
const POWER = ['battery_percent', 'ac_connected', 'charging', 'power_source']

const deviceIcon = (device: Pick<DeviceDetail, 'type'>): IconName =>
  device.type === 'windows' ? 'laptop' : device.type === 'linux' ? 'server' : 'phone'

const pillStatus = (connection: string) => (connection === 'online' ? 'online' : connection === 'offline' ? 'offline' : 'unknown')
const FRESHNESS_LABEL: Record<DeviceFreshness, string> = { fresh: 'Live', stale: 'Stale', unknown: 'Not current' }

export function SensorTile({ sensor, device, trusted, now }: { sensor: DeviceSensor; device: DeviceDetail; trusted: boolean; now: number }) {
  const meta = sensorMeta(sensor, device)
  const freshness = effectiveFreshness(sensor, device, { now, trusted })
  const shown = sensorText(sensor, freshness, device)
  const percent = meta.progress && sensor.availability === 'available' && typeof sensor.value === 'number' ? Math.max(0, Math.min(100, sensor.value)) : null
  const tag = sensor.availability !== 'available' ? 'No value' : FRESHNESS_LABEL[freshness]
  return <div className={`metric-tile device-metric is-${sensor.availability === 'available' ? freshness : 'unavailable'}`} data-sensor={sensor.id}
    style={percent === null ? undefined : { '--metric-progress': `${percent}%` } as CSSProperties}>
    <div className="metric-label"><Icon name={meta.icon} /><span>{meta.label}</span><span className={`freshness-tag is-${sensor.availability === 'available' ? freshness : 'unavailable'}`}>{tag}</span></div>
    <strong>{shown.text}</strong>
    {percent !== null && <div className="metric-bar"><span /></div>}
    {(shown.note || sensor.support === 'unverified') && <small className="device-metric-note">{[shown.note, sensor.support === 'unverified' ? 'Unverified source' : null].filter(Boolean).join(' · ')}</small>}
  </div>
}

export const DeviceCard = memo(function DeviceCard({ device, trusted, now, featured = false }: { device: DeviceDetail; trusted: boolean; now: number; featured?: boolean }) {
  const connected = telemetryConnected(device)
  const connection = effectiveConnection(device, trusted, now)
  const { reportable, unsupported } = partitionSensors(device)
  const primary = reportable.filter((sensor) => PRIMARY.includes(sensor.id))
  const power = reportable.filter((sensor) => POWER.includes(sensor.id))
  const other = reportable.filter((sensor) => !PRIMARY.includes(sensor.id) && !POWER.includes(sensor.id))
  const warning = connected ? diskWarning(device) : null
  const disk = device.sensors.find((sensor) => sensor.id === 'disk_usage')
  const diskCurrent = warning !== null && disk !== undefined && effectiveFreshness(disk, device, { now, trusted }) === 'fresh'
  const label = connected ? connectionLabel(connection) : 'Not connected'
  const measured = device.type === 'android' ? lastMeasured(device) : null
  return <article className={`device-card surface ${featured ? 'is-featured' : ''} is-${connected ? connection : 'unconnected'}`} data-device={device.id} aria-labelledby={`device-${device.id}`}>
    <header className="device-card-head">
      <span className="device-card-icon"><Icon name={deviceIcon(device)} /></span>
      <div className="device-card-title"><p>{device.platform}</p><h2 id={`device-${device.id}`}>{device.name}</h2></div>
      <StatusPill status={connected ? pillStatus(connection) : 'unknown'} label={label} />
    </header>
    <div className="device-card-meta">
      <span><Icon name="pulse" />{connectionDetail(connection, device)}</span>
      {connected && <span><Icon name="clock" />Last report {relativeTime(device.last_seen_at, now)}</span>}
      {connected && measured && <span><Icon name="pulse" />Measured {relativeTime(measured, now)} · device clock</span>}
      {connected && !trusted && <span className="is-paused">Live updates paused · values not current</span>}
    </div>
    {warning && <div className="device-warning" role="status"><Icon name="disk" /><div><strong>{warning.label} is {warning.percent}% full</strong><span>{diskCurrent ? warning.advice : 'Last reported value — current fill level unknown.'}</span></div></div>}
    {connected
      ? <>
        {primary.length > 0 && <div className="device-metrics">{primary.map((sensor) => <SensorTile key={sensor.id} sensor={sensor} device={device} trusted={trusted} now={now} />)}</div>}
        {power.length > 0 && <div className="device-power-row" aria-label="Power">{power.map((sensor) => <SensorTile key={sensor.id} sensor={sensor} device={device} trusted={trusted} now={now} />)}</div>}
        {other.length > 0 && <div className="device-metrics is-secondary">{other.map((sensor) => <SensorTile key={sensor.id} sensor={sensor} device={device} trusted={trusted} now={now} />)}</div>}
        {device.type === 'windows' && <p className="device-footnote">Times show when RoomOS received each value; the Windows agent does not report measurement times.</p>}
        {device.type === 'android' && <p className="device-footnote">Android limits background work: the app reports every minute while open and roughly every 15 minutes otherwise, so this phone can show as Unknown while it sleeps.</p>}
      </>
      : <div className="device-unconnected"><Icon name="wifi" /><p>No telemetry source is connected for this device yet{device.metadata.planned_phase ? ` (planned: ${device.metadata.planned_phase})` : ''}. RoomOS shows no CPU, RAM or battery values until it reports.</p></div>}
    {unsupported.length > 0 && <details className="device-unsupported"><summary><span>Not reported by this device</span><small>{unsupported.length}</small></summary><ul>{unsupported.map((sensor) => <li key={sensor.id}>{sensorMeta(sensor, device).label}</li>)}</ul></details>}
  </article>
})

const APP_LABEL: Record<PairedClient['app'], string> = { web: 'Web app', android: 'Android app', unknown: 'App type unknown' }

export function ClientRow({ client, now, resolutionSeconds }: { client: PairedClient; now: number; resolutionSeconds: number | null }) {
  const minutes = resolutionSeconds ? Math.round(resolutionSeconds / 60) : null
  return <li className={`client-row ${client.current ? 'is-current' : ''}`}>
    <span className="client-icon"><Icon name={client.app === 'android' ? 'phone' : 'devices'} /></span>
    <div className="client-main">
      <strong>{client.name}{client.current && <em>This device</em>}</strong>
      <small>{APP_LABEL[client.app]} · paired {new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(client.paired_at))}</small>
    </div>
    <div className="client-activity">
      {client.active_session
        ? <><span>Active {relativeTime(client.last_seen_at, now)}</span>{minutes && <small>updated every {minutes} min</small>}</>
        : <span className="is-inactive">No active session</span>}
    </div>
  </li>
}

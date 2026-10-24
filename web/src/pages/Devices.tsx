import { useAuthStatus } from '../auth-context'
import type { DevicesSnapshot } from '../devices-store'
import { useDevicesSnapshot, useNow } from '../devices-hooks'
import { effectiveConnection, groupDevices, telemetryConnected } from '../devices-state'
import { ErrorPanel, LoadingPanel, PageHeader, RealtimePill, SectionHeading } from '../components/Common'
import { ClientRow } from '../components/DeviceCard'
import { DeviceOverviewRow } from '../components/DeviceOverview'
import type { AlertRecord } from '../types'
import { useMonitoringAlerts } from '../monitoring-alerts'
import { Icon } from '../components/Icons'

const DEVICES_SCOPE = 'devices:read'

export function DevicePermissionPanel() {
  return <div className="device-state-panel surface" role="status">
    <span className="device-state-icon"><Icon name="devices" /></span>
    <div><h2>Device access not enabled</h2><p>This app is paired, but it does not have permission to view device telemetry. The RoomOS operator can grant device access; you will not need to pair again.</p></div>
  </div>
}

export function DeviceCenterView({ snapshot, now, onRetry, alerts = [] }: { snapshot: DevicesSnapshot; now: number; onRetry: () => void; alerts?: AlertRecord[] }) {
  const { status, devices, trusted } = snapshot
  if (status === 'forbidden') return <DevicePermissionPanel />
  if (status === 'loading' && devices.length === 0) return <LoadingPanel label="Reading devices…" />
  const groups = groupDevices(devices)
  const connected = devices.filter(telemetryConnected)
  const online = connected.filter((device) => effectiveConnection(device, trusted, now) === 'online').length
  const offline = connected.filter((device) => effectiveConnection(device, trusted, now) === 'offline').length
  return <>
    {status === 'error' && <div className="device-error"><ErrorPanel message="RoomOS could not read the device list. Values below, if any, are not current." /><button className="device-retry" onClick={onRetry}>Retry</button></div>}
    {status === 'ready' && devices.length === 0 && <div className="device-state-panel surface" role="status"><span className="device-state-icon"><Icon name="devices" /></span><div><h2>No devices to show</h2><p>RoomOS has no devices visible to this app.</p></div></div>}
    {devices.length > 0 && <div className="device-overview" aria-label="Device summary">
      <div><strong>{online}</strong><span>Online</span></div>
      <div><strong>{offline}</strong><span>Offline</span></div>
      <div><strong>{connected.length - online - offline}</strong><span>Unknown</span></div>
      <div><strong>{devices.length - connected.length}</strong><span>Not connected</span></div>
    </div>}
    {groups.map((group) => <section key={group.id} className="device-group" aria-labelledby={`group-${group.id}`}>
      <SectionHeading icon={group.id === 'computers' ? 'laptop' : 'phone'} title={group.title} detail={group.id === 'computers' ? 'Live telemetry from RoomOS agents' : 'Reported by the RoomOS app on each phone or tablet'} />
      <div className="device-overview-list">{group.devices.map((device) => <DeviceOverviewRow key={device.id} device={device} trusted={trusted} now={now} alerts={alerts} />)}</div>
    </section>)}
    <details className="paired-apps-fold surface"><summary>Paired RoomOS apps · {snapshot.clients.length}</summary><ClientSection snapshot={snapshot} now={now} /></details>
  </>
}

function ClientSection({ snapshot, now }: { snapshot: DevicesSnapshot; now: number }) {
  const { clients, clientsStatus, clientResolutionSeconds } = snapshot
  return <section className="device-group" aria-labelledby="group-clients">
    <SectionHeading icon="devices" title="Paired RoomOS apps" detail="Apps that can open RoomOS. Activity, not device telemetry." />
    {clientsStatus === 'loading' && clients.length === 0 && <LoadingPanel label="Reading paired apps…" />}
    {clientsStatus === 'error' && <ErrorPanel message="Paired apps are unavailable right now." />}
    {clientsStatus === 'forbidden' && <DevicePermissionPanel />}
    {clientsStatus === 'ready' && clients.length === 0 && <p className="device-empty-line">No paired apps.</p>}
    {clients.length > 0 && <ul className="client-list surface">{clients.map((client) => <ClientRow key={client.id} client={client} now={now} resolutionSeconds={clientResolutionSeconds} />)}</ul>}
  </section>
}

function DeviceCenter() {
  const {snapshot, retry} = useDevicesSnapshot()
  const alerts = useMonitoringAlerts()
  const now = useNow(15_000)
  return <>
    <PageHeader eyebrow="Device Center" title="Devices" subtitle="Computers and phones around your room, with live telemetry where RoomOS has it."><RealtimePill status={snapshot.connection} /></PageHeader>
    {alerts.error && <ErrorPanel message="Alarm status unavailable; device telemetry is shown separately." />}
    <DeviceCenterView snapshot={snapshot} now={now} onRetry={retry} alerts={alerts.data?.notifications} />
  </>
}

export function DevicesPage() {
  const auth = useAuthStatus()
  const allowed = auth?.device?.scopes.includes(DEVICES_SCOPE) ?? false
  return <section className="page devices-page">
    {allowed
      ? <DeviceCenter />
      : <><PageHeader eyebrow="Device Center" title="Devices" /><DevicePermissionPanel /></>}
  </section>
}

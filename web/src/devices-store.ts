import type { RoomOSLifecycle } from './lifecycle'
import { parseDeviceDetail } from './devices-state'
import type { ConnectionStatus, DeviceDetail, DeviceList, PairedClient, PairedClientList, RoomOSEvent } from './types'

/* Device Center store (7D.1 foundation, mounted by 7D.4). GET-only, event-driven. */

export interface DevicesApi {
  devices: () => Promise<DeviceList>
  /** Paired RoomOS apps; optional so the registry works without it. */
  clients?: () => Promise<PairedClientList>
}

export type LoadStatus = 'loading' | 'ready' | 'error' | 'forbidden'

export type DevicesSnapshot = {
  status: LoadStatus
  devices: DeviceDetail[]
  connection: ConnectionStatus
  /** False until a REST load completes on the current connection epoch. */
  trusted: boolean
  loadedAt: number | null
  clients: PairedClient[]
  clientsStatus: LoadStatus
  /** Server-side granularity of client last_seen_at (seconds). */
  clientResolutionSeconds: number | null
}

const statusOf = (error: unknown): LoadStatus => ((error as { status?: number }).status === 403 ? 'forbidden' : 'error')
const unauthorized = (error: unknown) => [401, 403].includes((error as { status?: number }).status ?? 0)

export class DevicesStore {
  private snapshot: DevicesSnapshot
  private listeners = new Set<() => void>()
  private epoch = 0
  private pendingEvents = new Map<string, DeviceDetail>()
  private loading = false

  constructor(private api: DevicesApi, connection: ConnectionStatus = 'reconnecting', private now = () => Date.now()) {
    this.snapshot = { status: 'loading', devices: [], connection, trusted: false, loadedAt: null, clients: [], clientsStatus: 'loading', clientResolutionSeconds: null }
  }

  getSnapshot = () => this.snapshot
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  private update(change: Partial<DevicesSnapshot>) {
    this.snapshot = { ...this.snapshot, ...change }
    this.listeners.forEach((listener) => listener())
  }

  async load() {
    const epoch = ++this.epoch
    this.loading = true
    this.pendingEvents.clear()
    await Promise.all([this.loadDevices(epoch), this.loadClients(epoch)])
    if (epoch === this.epoch) { this.loading = false; this.pendingEvents.clear() }
  }

  private async loadDevices(epoch: number) {
    try {
      const list = await this.api.devices()
      if (epoch !== this.epoch) return
      // Events received while the GET was in flight are newer than its snapshot.
      this.update({ status: 'ready', devices: list.devices.map((device) => this.pendingEvents.get(device.id) ?? device), trusted: this.snapshot.connection === 'connected', loadedAt: this.now() })
    } catch (error) {
      if (epoch !== this.epoch) return
      this.update({ status: statusOf(error), trusted: false, ...(unauthorized(error) ? { devices: [], loadedAt: null } : {}) })
    }
  }

  private async loadClients(epoch: number) {
    if (!this.api.clients) return
    try {
      const list = await this.api.clients()
      if (epoch !== this.epoch) return
      if (this.snapshot.clientsStatus === 'ready' && JSON.stringify(list.clients) === JSON.stringify(this.snapshot.clients)) return
      this.update({ clients: list.clients, clientsStatus: 'ready', clientResolutionSeconds: list.last_seen_resolution_seconds })
    } catch (error) {
      if (epoch !== this.epoch) return
      this.update({ clientsStatus: statusOf(error), ...(unauthorized(error) ? { clients: [] } : {}) })
    }
  }

  /** Applies scope-filtered ``device.updated`` events; identical payloads cause no update. */
  handleEvent(event: RoomOSEvent) {
    if (event.type !== 'device.updated' || !event.data.device) return
    let next: DeviceDetail
    try { next = parseDeviceDetail(event.data.device) } catch { return }
    if (this.loading && this.pendingEvents.size < 256) this.pendingEvents.set(next.id, next)
    if (this.snapshot.status !== 'ready') return
    const index = this.snapshot.devices.findIndex((device) => device.id === next.id)
    if (index === -1) return // Unknown to this client's last authoritative list; the next load decides.
    if (JSON.stringify(this.snapshot.devices[index]) === JSON.stringify(next)) return
    const devices = this.snapshot.devices.slice()
    devices[index] = next // other device objects keep their identity, so their cards do not re-render
    this.update({ devices })
  }

  invalidate() {
    this.epoch += 1
    this.loading = false
    this.pendingEvents.clear()
    this.update({ trusted: false })
  }

  /** Any connection change invalidates live state until the caller reloads (reconnect sync). */
  setConnection(connection: ConnectionStatus) {
    if (connection === this.snapshot.connection) return
    this.epoch += 1
    this.loading = false
    this.pendingEvents.clear()
    this.update({ connection, trusted: false })
  }
}


/**
 * Mounts the store on the shared lifecycle: no new socket, no polling. Device events never
 * trigger a refetch (refreshOnEvent false); only connect, reconnect and foreground do.
 */
export function bindDevicesLifecycle(store: DevicesStore, lifecycle: Pick<RoomOSLifecycle, 'subscribeStatus' | 'subscribeEvents'>) {
  // While the socket is still opening, its onopen refresh performs the first load; loading
  // here too would fetch everything twice at app start. Connected or offline: load now.
  let loaded = false
  const loadOnce = () => { if (!loaded) { loaded = true; void store.load() } }
  const offEvents = lifecycle.subscribeEvents((event) => store.handleEvent(event), () => { loaded = true; return store.load() }, { refreshOnEvent: false })
  const offStatus = lifecycle.subscribeStatus((status) => {
    store.setConnection(status)
    if (status === 'offline') loadOnce()
  })
  if (store.getSnapshot().connection === 'connected') loadOnce()
  return () => {
    offEvents()
    offStatus()
    store.invalidate()
  }
}

import { parseSystemData } from './api'
import type { ConnectionStatus, RoomOSEvent, SystemData } from './types'

/**
 * 7L: event-driven System snapshot for the Dashboard (services, DNS cluster). One bounded GET
 * /system per connection epoch, then `system.snapshot_updated` carries the whole snapshot and
 * `system.service_updated` (DNS probe) triggers one re-read only when the cluster state changes.
 * No polling.
 */
export type SystemSnapshot = {
  data: SystemData | null
  status: 'loading' | 'ready' | 'error' | 'forbidden'
  connection: ConnectionStatus
  /** True only after a load on the current connection epoch: data is live, not last known. */
  trusted: boolean
}

export class SystemStore {
  private snapshot: SystemSnapshot
  private listeners = new Set<() => void>()
  private epoch = 0
  private inflight: Promise<void> | null = null

  constructor(private load$: () => Promise<SystemData>, allowed: boolean, connection: ConnectionStatus = 'reconnecting') {
    this.snapshot = { data: null, status: allowed ? 'loading' : 'forbidden', connection, trusted: false }
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  getSnapshot = () => this.snapshot

  private update(change: Partial<SystemSnapshot>) {
    this.snapshot = { ...this.snapshot, ...change }
    this.listeners.forEach((listener) => listener())
  }

  load(): Promise<void> {
    if (this.snapshot.status === 'forbidden') return Promise.resolve()
    if (this.inflight) return this.inflight
    const epoch = this.epoch
    const run = bounded(this.load$()).then((data) => {
      if (epoch !== this.epoch) return
      this.update({ data, status: 'ready', trusted: this.snapshot.connection === 'connected' })
    }, (cause: unknown) => {
      if (epoch !== this.epoch) return
      const status = (cause as { status?: number }).status
      this.update(status === 403 ? { data: null, status: 'forbidden', trusted: false } : { status: 'error', trusted: false })
    })
    this.inflight = run.finally(() => { this.inflight = null })
    return this.inflight
  }

  setConnection(connection: ConnectionStatus) {
    if (connection === this.snapshot.connection) return
    this.epoch += 1
    this.update({ connection, trusted: false })
  }

  handleEvent(event: RoomOSEvent) {
    if (this.snapshot.status === 'forbidden') return
    if (event.type === 'system.snapshot_updated') {
      try {
        const data = parseSystemData(event.data.system)
        this.update({ data, status: 'ready' })
      } catch { /* malformed payload: keep the last valid snapshot */ }
      return
    }
    if (event.type === 'system.service_updated' && event.data.service === 'dns' && this.snapshot.data) {
      const status = event.data.status
      if (status !== 'healthy' && status !== 'degraded' && status !== 'offline' && status !== 'unknown') return
      // A DNS state change is rare; its event carries only the cluster status, so re-read once to
      // keep resolver rows consistent with it. Repeated probes with an unchanged state cost nothing.
      if (this.snapshot.data.dns_cluster.status !== status) void this.load()
    }
  }
}

function bounded<T>(promise: Promise<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Request timed out')), 15_000)
    promise.then(resolve, reject).finally(() => clearTimeout(timer))
  })
}

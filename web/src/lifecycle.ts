import { setConnectionStatus } from './connection-state'
import type { ConnectionStatus, RoomOSEvent } from './types'

type SocketLike = {
  readyState: number
  close: () => void
  onopen: ((event: Event) => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
  onclose: ((event: { code: number }) => void) | null
  onerror: (() => void) | null
}
type Timer = ReturnType<typeof setTimeout>
type StatusListener = (status: ConnectionStatus) => void
type EventListener = (event: RoomOSEvent) => void
type RefreshListener = () => Promise<void>
/** Legacy refresh-on-event policy: every event, none, or only events the page depends on. */
export type RefreshOnEvent = boolean | ((event: RoomOSEvent) => boolean)

export type LifecycleEnvironment = {
  createSocket: () => SocketLike
  online: () => boolean
  visible: () => boolean
  random: () => number
  setTimer: (work: () => void, delay: number) => Timer
  clearTimer: (timer: Timer) => void
  addWindowListener: (name: 'online' | 'offline', listener: () => void) => void
  removeWindowListener: (name: 'online' | 'offline', listener: () => void) => void
  addVisibilityListener: (listener: () => void) => void
  removeVisibilityListener: (listener: () => void) => void
  dispatch: (name: 'roomos:auth-required' | 'roomos:permission-denied' | 'roomos:scopes-changed') => void
  verify: () => Promise<'ok' | 'auth' | 'permission' | 'offline'>
}

const socketOpen = 1
const socketConnecting = 0

export function reconnectDelay(attempt: number, random = Math.random) {
  const bounded = Math.min(1_000 * 2 ** Math.max(0, attempt), 30_000)
  return Math.round(bounded * (.8 + random() * .4))
}

export class RoomOSLifecycle {
  private status: ConnectionStatus = 'reconnecting'
  private socket: SocketLike | null = null
  private retry: Timer | null = null
  private attempt = 0
  private started = false
  private statusListeners = new Set<StatusListener>()
  private eventListeners = new Set<EventListener>()
  // value = which ordinary events also refresh this listener (legacy pages): true = every event,
  // a filter = only matching event types. Event-driven pages opt out (false) and only receive
  // authoritative refreshes on connect, reconnect and return to foreground.
  private refreshListeners = new Map<RefreshListener, RefreshOnEvent>()
  private eventRefreshDue = new Set<RefreshListener>()
  private refreshTimer: Timer | null = null
  private refreshing: Promise<void> | null = null
  private foreground: Promise<void> | null = null
  private refreshVersion = 0
  private statusEpoch = 0
  private refreshEpoch = -1

  constructor(private env: LifecycleEnvironment) {}

  get snapshot() { return this.status }
  get activeSocketCount() { return this.socket && (this.socket.readyState === socketOpen || this.socket.readyState === socketConnecting) ? 1 : 0 }

  subscribeStatus(listener: StatusListener) {
    this.statusListeners.add(listener)
    listener(this.status)
    this.ensureStarted()
    return () => { this.statusListeners.delete(listener) }
  }

  subscribeEvents(listener: EventListener, refresh?: RefreshListener, options: { refreshOnEvent?: RefreshOnEvent } = {}) {
    this.eventListeners.add(listener)
    if (refresh) this.refreshListeners.set(refresh, options.refreshOnEvent ?? true)
    this.ensureStarted()
    return () => {
      this.eventListeners.delete(listener)
      if (refresh) { this.refreshListeners.delete(refresh); this.eventRefreshDue.delete(refresh) }
    }
  }

  ensureStarted() {
    if (this.started) return
    this.started = true
    this.env.addWindowListener('online', this.handleOnline)
    this.env.addWindowListener('offline', this.handleOffline)
    this.env.addVisibilityListener(this.handleVisibility)
    this.connect()
  }

  stop() {
    this.started = false
    this.clearRetry()
    if (this.refreshTimer) this.env.clearTimer(this.refreshTimer)
    this.refreshTimer = null
    const socket = this.socket
    this.socket = null
    socket?.close()
    this.env.removeWindowListener('online', this.handleOnline)
    this.env.removeWindowListener('offline', this.handleOffline)
    this.env.removeVisibilityListener(this.handleVisibility)
  }

  private setStatus(status: ConnectionStatus) {
    if (this.status === status) return
    this.status = status
    this.statusEpoch += 1
    setConnectionStatus(status)
    this.statusListeners.forEach((listener) => listener(status))
  }

  private clearRetry() {
    if (this.retry) this.env.clearTimer(this.retry)
    this.retry = null
  }

  private connect() {
    if (!this.started) return
    if (!this.env.online()) { this.setStatus('offline'); return }
    if (this.socket && (this.socket.readyState === socketOpen || this.socket.readyState === socketConnecting)) return
    this.clearRetry()
    this.setStatus('reconnecting')
    const socket = this.env.createSocket()
    this.socket = socket
    socket.onopen = () => {
      if (this.socket !== socket) return
      this.attempt = 0
      this.setStatus('connected')
      void this.refreshAll()
    }
    socket.onmessage = (message) => {
      if (this.socket !== socket) return
      try {
        const event = JSON.parse(String(message.data)) as RoomOSEvent
        if (!event.type) return
        this.eventListeners.forEach((listener) => listener(event))
        // Only listeners interested in this event type refresh; a page's own GET must not be
        // able to trigger itself again through unrelated events (7D.1 System/device loop).
        for (const [refresh, onEvent] of this.refreshListeners) {
          if (onEvent === true || (typeof onEvent === 'function' && onEvent(event))) this.eventRefreshDue.add(refresh)
        }
        if (this.eventRefreshDue.size) this.scheduleRefresh()
      } catch { /* malformed frames never break reconnect lifecycle */ }
    }
    socket.onclose = (event) => {
      if (this.socket !== socket) return
      this.socket = null
      if (!this.started) return
      if (event.code === 4401) {
        this.setStatus('offline'); this.stop(); this.env.dispatch('roomos:auth-required'); return
      }
      if (event.code === 4403) {
        this.setStatus('offline'); this.stop(); this.env.dispatch('roomos:permission-denied'); return
      }
      // 4409: still paired, permissions changed. Re-read them (no pairing screen), then reconnect.
      if (event.code === 4409) this.env.dispatch('roomos:scopes-changed')
      this.attempt += 1
      this.setStatus(!this.env.online() || this.attempt >= 2 ? 'offline' : 'reconnecting')
      const delay = reconnectDelay(this.attempt - 1, this.env.random)
      this.retry = this.env.setTimer(() => { this.retry = null; this.connect() }, delay)
    }
    socket.onerror = () => undefined
  }

  private scheduleRefresh() {
    if (this.refreshTimer) this.env.clearTimer(this.refreshTimer)
    this.refreshTimer = this.env.setTimer(() => { this.refreshTimer = null; void this.refreshAll(true) }, 180)
  }

  /** Single-flight: concurrent requests share one run. eventOnly limits to legacy listeners. */
  private refreshAll(eventOnly = false): Promise<void> {
    if (!eventOnly && this.refreshing) return this.refreshEpoch === this.statusEpoch ? this.refreshing : this.refreshing.then(() => this.refreshAll())
    if (!eventOnly) this.refreshEpoch = this.statusEpoch
    if (!eventOnly) this.refreshVersion += 1
    const listeners = eventOnly
      ? [...this.eventRefreshDue].filter((refresh) => this.refreshListeners.has(refresh))
      : [...this.refreshListeners.keys()]
    this.eventRefreshDue.clear()
    const run = Promise.allSettled(listeners.map((refresh) => refresh())).then(() => undefined)
    if (eventOnly) return run
    this.refreshing = run.finally(() => { if (this.refreshing === pending) this.refreshing = null })
    const pending = this.refreshing
    return pending
  }

  private handleOnline = () => {
    void this.reconcileForeground()
  }
  private handleOffline = () => {
    this.clearRetry()
    this.setStatus('offline')
  }
  private handleVisibility = () => {
    if (!this.env.visible()) return
    void this.reconcileForeground()
  }

  private reconcileForeground(): Promise<void> {
    if (this.foreground) return this.foreground
    const version = this.refreshVersion
    const run = this.verifyForeground(version).finally(() => { this.foreground = null })
    this.foreground = run
    return run
  }

  private async verifyForeground(version: number) {
    if (!this.env.online()) { this.setStatus('offline'); return }
    const result = await this.env.verify()
    if (result === 'auth') { this.setStatus('offline'); this.stop(); this.env.dispatch('roomos:auth-required'); return }
    if (result === 'permission') { this.setStatus('offline'); this.stop(); this.env.dispatch('roomos:permission-denied'); return }
    if (result === 'offline') { this.setStatus('offline'); this.socket?.close(); return }
    this.attempt = 0
    if (this.socket?.readyState === socketOpen) {
      this.setStatus('connected')
      if (version === this.refreshVersion) await this.refreshAll()
    } else {
      // The socket's onopen performs the authoritative refresh; refreshing here as well
      // would fetch everything twice.
      this.connect()
    }
  }
}

export function browserLifecycleEnvironment(): LifecycleEnvironment {
  return {
    createSocket: () => new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/v1/events`) as unknown as SocketLike,
    online: () => navigator.onLine,
    visible: () => document.visibilityState === 'visible',
    random: Math.random,
    setTimer: (work, delay) => window.setTimeout(work, delay),
    clearTimer: (timer) => window.clearTimeout(timer),
    addWindowListener: (name, listener) => window.addEventListener(name, listener),
    removeWindowListener: (name, listener) => window.removeEventListener(name, listener),
    addVisibilityListener: (listener) => document.addEventListener('visibilitychange', listener),
    removeVisibilityListener: (listener) => document.removeEventListener('visibilitychange', listener),
    dispatch: (name) => window.dispatchEvent(new Event(name)),
    verify: async () => {
      try {
        const response = await fetch('/api/v1/health', { credentials: 'same-origin', signal: AbortSignal.timeout(5_000) })
        if (response.status === 401) return 'auth'
        if (response.status === 403) return 'permission'
        return response.ok ? 'ok' : 'offline'
      } catch { return 'offline' }
    },
  }
}

export const roomosLifecycle = typeof window === 'undefined' ? null : new RoomOSLifecycle(browserLifecycleEnvironment())

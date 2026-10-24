import type { HealthReport } from './types'

export const HEALTH_POLL_MS = 60_000
export const HEALTH_STALE_MS = 120_000
export const HEALTH_TIMEOUT_MS = 15_000
export type HealthSnapshot = {
  report: HealthReport | null
  freshness: 'unavailable' | 'fresh' | 'stale'
  refreshing: boolean
  lastSuccessAt: number | null
  lastAttemptAt: number | null
  lastAttemptResult: 'none' | 'pending' | 'success' | 'error' | 'cancelled'
  error: string | null
  ageMs: number | null
}

export interface HealthEnvironment {
  now: () => number
  monotonic: () => number
  visible: () => boolean
  listen: (listener: () => void) => () => void
}

export const browserHealthEnvironment: HealthEnvironment = {
  now: () => Date.now(),
  monotonic: () => performance.now(),
  visible: () => document.visibilityState === 'visible',
  listen: (listener) => {
    document.addEventListener('visibilitychange', listener)
    return () => document.removeEventListener('visibilitychange', listener)
  },
}

/** Health-only, in-memory single-flight GET lifecycle. Never issues device commands. */
export class HealthStore {
  private snapshot: HealthSnapshot = {
    report: null, freshness: 'unavailable', refreshing: false, lastSuccessAt: null,
    lastAttemptAt: null, lastAttemptResult: 'none', error: null, ageMs: null,
  }
  private listeners = new Set<() => void>()
  private active = false
  private visible = false
  private controller: AbortController | null = null
  private timeout: ReturnType<typeof setTimeout> | undefined
  private poll: ReturnType<typeof setTimeout> | undefined
  private clock: ReturnType<typeof setInterval> | undefined
  private unlisten: (() => void) | undefined
  private failures = 0
  private receivedMono = 0
  private sourceAge = 0
  private sourceTimeValid = false

  constructor(private load: (signal: AbortSignal) => Promise<HealthReport>, private env = browserHealthEnvironment) {}

  getSnapshot = () => this.snapshot
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  private update(change: Partial<HealthSnapshot>) {
    this.snapshot = { ...this.snapshot, ...change }
    this.listeners.forEach((listener) => listener())
  }

  start = () => {
    if (this.active) return
    this.active = true
    this.unlisten = this.env.listen(this.visibilityChanged)
    this.visibilityChanged()
  }

  stop = () => {
    this.active = false
    this.visible = false
    this.unlisten?.()
    this.unlisten = undefined
    clearTimeout(this.poll)
    clearInterval(this.clock)
    this.cancel()
  }

  private cancel() {
    clearTimeout(this.timeout)
    const controller = this.controller
    this.controller = null // invalidate even transports that ignore AbortSignal
    controller?.abort()
    if (controller) {
      this.sourceTimeValid = false
      this.update({ refreshing: false, lastAttemptResult: 'cancelled', freshness: this.snapshot.report ? 'stale' : 'unavailable' })
    }
  }

  private visibilityChanged = () => {
    const visible = this.env.visible()
    if (!this.active || this.visible === visible) return
    this.visible = visible
    clearTimeout(this.poll)
    clearInterval(this.clock)
    if (!visible) { this.cancel(); return }
    this.tick()
    this.clock = setInterval(this.tick, 1_000)
    this.refresh()
  }

  private tick = () => {
    if (!this.snapshot.report || this.snapshot.lastSuccessAt === null) return
    const elapsed = this.env.monotonic() - this.receivedMono
    const wallElapsed = this.env.now() - this.snapshot.lastSuccessAt
    const ageMs = Math.max(this.snapshot.ageMs ?? 0, this.sourceAge + Math.max(0, elapsed, wallElapsed))
    const clockInvalid = elapsed < 0 || wallElapsed < 0
    if (clockInvalid) this.sourceTimeValid = false
    const failed = ['error', 'cancelled'].includes(this.snapshot.lastAttemptResult) || this.snapshot.error !== null
    this.update({ ageMs, freshness: !clockInvalid && this.sourceTimeValid && !failed && ageMs < HEALTH_STALE_MS ? 'fresh' : 'stale' })
  }

  refresh = () => {
    if (!this.active || !this.visible || this.controller) return
    clearTimeout(this.poll)
    const controller = new AbortController()
    this.controller = controller
    this.update({ refreshing: true, lastAttemptAt: this.env.now(), lastAttemptResult: 'pending' })
    const current = () => this.active && this.controller === controller
    const finish = (error: string | null, report?: HealthReport) => {
      if (!current()) return
      clearTimeout(this.timeout)
      this.controller = null
      if (report) {
        const now = this.env.now()
        const checked = Date.parse(report.checked_at)
        this.receivedMono = this.env.monotonic()
        // Receipt time is not source measurement time. A stale response stays stale.
        this.sourceTimeValid = Number.isFinite(checked) && checked <= now + 5_000
        this.sourceAge = Number.isFinite(checked) ? Math.max(0, now - checked) : 0
        this.failures = 0
        this.update({ report, lastSuccessAt: now, refreshing: false, error: null, lastAttemptResult: 'success', ageMs: this.sourceAge })
        this.tick()
      } else {
        this.failures += 1
        this.update({ refreshing: false, error, lastAttemptResult: 'error', freshness: this.snapshot.report ? 'stale' : 'unavailable' })
        this.tick()
      }
      if (this.active && this.visible) {
        const delay = error ? Math.min(HEALTH_POLL_MS, 15_000 * 2 ** Math.min(this.failures - 1, 2)) : HEALTH_POLL_MS
        this.poll = setTimeout(this.refresh, delay)
      }
    }
    this.timeout = setTimeout(() => {
      finish('Health request timed out. Current health is unknown.')
      controller.abort()
    }, HEALTH_TIMEOUT_MS)
    // Promise boundary also handles a synchronous loader failure.
    void Promise.resolve().then(() => {
      if (!current()) return
      return this.load(controller.signal)
    }).then((report) => {
      if (report) finish(null, report)
    }, () => {
      // Never echo arbitrary transport errors, response bodies, or credentials.
      finish('Health API or connection error. Current health is unknown.')
    })
  }
}

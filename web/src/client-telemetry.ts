import { useEffect, useRef } from 'react'

/* Phase 7D.3: the RoomOS PWA reports what Chrome for Android can actually tell it.
   Only while the app is visible and only with telemetry:write:self; the server binds the
   report to this paired app's device. No polling while hidden, no storage quota posing as
   device storage, no RAM/CPU/temperature guesses. Values the browser cannot give are null. */

export const TELEMETRY_SCOPE = 'telemetry:write:self'
export const PWA_INTERVAL_SECONDS = 60
/** The server accepts one report per 15 s per app; keep a margin. */
export const MIN_GAP_MS = 16_000
const SEQUENCE_KEY = 'roomos.telemetry.sequence'
// Earliest time the next attempt may start. Persisted so a reload (e.g. a service-worker
// takeover right after opening) or a reconnect does not send again within the server's window.
const NOT_BEFORE_KEY = 'roomos.telemetry.notBefore'
// Last accepted report: a reconnect or reload within a minute resumes the regular cadence
// instead of reporting again early (the previous values are still fresh on the server).
const LAST_SENT_KEY = 'roomos.telemetry.lastSent'
// One attempt at a time per page, even if the reporter is re-created (remount).
let inFlight = false

type BatteryLike = { level: number; charging: boolean }
type ConnectionLike = { type?: string }
type UaDataLike = { getHighEntropyValues?: (hints: string[]) => Promise<{ model?: string; platformVersion?: string; platform?: string }> }
export type BrowserLike = {
  onLine: boolean
  getBattery?: () => Promise<BatteryLike>
  connection?: ConnectionLike
  userAgentData?: UaDataLike
}

export type PwaSensors = {
  battery_percent: number | null
  charging: boolean | null
  network_type: string | null
  network_connected: boolean
  android_version: string | null
  device_model: string | null
}

const NETWORK_TYPES: Record<string, string> = { wifi: 'wifi', cellular: 'cellular', ethernet: 'ethernet', bluetooth: 'bluetooth', none: 'none', other: 'other', mixed: 'other', wimax: 'other' }

export async function collectPwaSensors(browser: BrowserLike): Promise<PwaSensors> {
  const battery: BatteryLike | null = browser.getBattery ? await browser.getBattery().catch(() => null) : null
  type Hints = { model?: string; platformVersion?: string; platform?: string }
  const hints: Hints = (await Promise.resolve(browser.userAgentData?.getHighEntropyValues?.(['model', 'platformVersion'])).catch(() => undefined)) ?? {}
  const level = battery && Number.isFinite(battery.level) && battery.level >= 0 && battery.level <= 1 ? Math.round(battery.level * 1000) / 10 : null
  const version = hints.platform === 'Android' && typeof hints.platformVersion === 'string' && /^[0-9]{1,2}(\.[0-9]{1,3}){0,2}$/.test(hints.platformVersion) ? hints.platformVersion : null
  const model = typeof hints.model === 'string' && /^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,39}$/.test(hints.model) ? hints.model : null
  return {
    battery_percent: level,
    charging: battery && typeof battery.charging === 'boolean' ? battery.charging : null,
    network_type: NETWORK_TYPES[browser.connection?.type ?? ''] ?? null,
    network_connected: browser.onLine,
    android_version: version,
    device_model: model,
  }
}

export function nextSequence(now: number, storage: Pick<Storage, 'getItem' | 'setItem'> | null): number {
  const previous = Number(storage?.getItem(SEQUENCE_KEY) ?? 0)
  const value = Math.max(Number.isSafeInteger(previous) ? previous + 1 : 1, Math.floor(now))
  storage?.setItem(SEQUENCE_KEY, String(value))
  return value
}

export function pwaPayload(sensors: PwaSensors, sequence: number, measuredAt: Date) {
  return { schema_version: 1, sequence, measured_at: measuredAt.toISOString(), next_report_seconds: PWA_INTERVAL_SECONDS, sensors }
}

export type TelemetrySend = (body: string) => Promise<void>
export type TelemetryDeps = {
  browser: BrowserLike
  send: TelemetrySend
  storage: Pick<Storage, 'getItem' | 'setItem'> | null
  now: () => number
  setTimer: (callback: () => void, ms: number) => number
  clearTimer: (id: number) => void
}

/** One report per minute while visible; stops for good on 401/403 until scopes change.
    Starting, reconnecting, becoming visible or reloading never sends sooner than the
    persisted not-before time (last attempt + 16 s, or the server's Retry-After). */
export class PwaTelemetryReporter {
  private timer: number | null = null
  private running = false
  private blocked = false

  constructor(private readonly deps: TelemetryDeps) {}

  setActive(active: boolean) {
    if (active && !this.running && !this.blocked) {
      this.running = true
      const resumeAt = Math.max(this.notBefore(), this.stored(LAST_SENT_KEY) + PWA_INTERVAL_SECONDS * 1000)
      this.schedule(Math.max(0, resumeAt - this.deps.now()))
    } else if (!active && this.running) {
      this.running = false
      if (this.timer !== null) this.deps.clearTimer(this.timer)
      this.timer = null
    }
  }

  /** Scopes were re-read (4409): a previously refused app may try again. */
  reset() { this.blocked = false }

  private stored(key: string): number {
    const value = Number(this.deps.storage?.getItem(key) ?? 0)
    // A future value beyond the longest legitimate wait means a changed clock: ignore it.
    return Number.isFinite(value) && value <= this.deps.now() + 10 * 60_000 ? value : 0
  }

  private notBefore(): number { return this.stored(NOT_BEFORE_KEY) }

  private store(key: string, value: number) {
    try { this.deps.storage?.setItem(key, String(Math.round(value))) } catch { /* storage full or blocked */ }
  }

  private setNotBefore(value: number) { this.store(NOT_BEFORE_KEY, value) }

  private schedule(ms: number) {
    if (this.timer !== null) this.deps.clearTimer(this.timer)
    this.timer = this.deps.setTimer(() => { this.timer = null; void this.tick() }, ms)
  }

  private async tick() {
    if (!this.running) return
    const wait = this.notBefore() - this.deps.now()
    if (inFlight || wait > 0) {
      this.schedule(Math.max(wait, 1000)) // another attempt is running or too recent
      return
    }
    inFlight = true
    this.setNotBefore(this.deps.now() + MIN_GAP_MS) // before sending: a reload mid-request waits too
    try {
      const sensors = await collectPwaSensors(this.deps.browser)
      const measuredAt = new Date(this.deps.now())
      await this.deps.send(JSON.stringify(pwaPayload(sensors, nextSequence(this.deps.now(), this.deps.storage), measuredAt)))
      this.store(LAST_SENT_KEY, this.deps.now())
    } catch (error) {
      const { status, retryAfter } = error as { status?: number; retryAfter?: number | null }
      if (status === 401 || status === 403) {
        this.blocked = true
        this.setActive(false)
        return
      }
      if (status === 429 && typeof retryAfter === 'number' && retryAfter > 0) {
        this.setNotBefore(Math.max(this.notBefore(), this.deps.now() + Math.min(retryAfter, 600) * 1000))
      }
      // offline/429/5xx: drop this measurement; the next tick measures again (no replay)
    } finally {
      inFlight = false
    }
    if (this.running) this.schedule(Math.max(PWA_INTERVAL_SECONDS * 1000, this.notBefore() - this.deps.now()))
  }
}

/** Mount once in the app shell. Inert unless the session holds telemetry:write:self. Reports
    only while the page is visible and the live connection is up (writes need it). */
export function useClientTelemetry(scopes: readonly string[], connected: boolean, send: TelemetrySend) {
  const permitted = scopes.includes(TELEMETRY_SCOPE)
  const reporterRef = useRef<PwaTelemetryReporter | null>(null)
  const connectedRef = useRef(connected)
  useEffect(() => {
    connectedRef.current = connected
    reporterRef.current?.setActive(connected && document.visibilityState === 'visible')
  }, [connected])
  useEffect(() => {
    if (!permitted || typeof window === 'undefined') return undefined
    const reporter = new PwaTelemetryReporter({
      browser: navigator as unknown as BrowserLike,
      send,
      storage: (() => { try { return window.localStorage } catch { return null } })(),
      now: () => Date.now(),
      setTimer: (callback, ms) => window.setTimeout(callback, ms),
      clearTimer: (id) => window.clearTimeout(id),
    })
    reporterRef.current = reporter
    const visibility = () => reporter.setActive(connectedRef.current && document.visibilityState === 'visible')
    const scopesChanged = () => reporter.reset()
    visibility()
    document.addEventListener('visibilitychange', visibility)
    window.addEventListener('roomos:scopes-changed', scopesChanged)
    return () => {
      reporterRef.current = null
      reporter.setActive(false)
      document.removeEventListener('visibilitychange', visibility)
      window.removeEventListener('roomos:scopes-changed', scopesChanged)
    }
  }, [permitted, send])
}

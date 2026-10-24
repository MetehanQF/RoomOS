export type WakeLockStatus = 'unsupported' | 'off' | 'requesting' | 'active' | 'released' | 'denied'

export type WakeLockSentinelLike = {
  released: boolean
  release: () => Promise<void>
  addEventListener: (name: 'release', listener: () => void) => void
}

export type WakeLockEnvironment = {
  visibilityState: () => DocumentVisibilityState
  addVisibilityListener: (listener: () => void) => void
  removeVisibilityListener: (listener: () => void) => void
  request?: () => Promise<WakeLockSentinelLike>
}

export const PANEL_NIGHT_KEY = 'roomos:panel-night:v1'
export const PANEL_WAKE_KEY = 'roomos:panel-wake:v1'

export function readPanelPreference(storage: Pick<Storage, 'getItem'> | null, key: string, fallback = false) {
  try {
    const value = storage?.getItem(key)
    return value === null || value === undefined ? fallback : value === '1'
  } catch { return fallback }
}

export function writePanelPreference(storage: Pick<Storage, 'setItem'> | null, key: string, value: boolean) {
  try { storage?.setItem(key, value ? '1' : '0') } catch { /* Private/blocked storage must not break controls. */ }
}

/** One opt-in screen wake lock. It never attempts a CPU/system lock. */
export class PanelWakeLockController {
  private enabled: boolean
  private sentinel: WakeLockSentinelLike | null = null
  private generation = 0
  private started = false
  private readonly visibility = () => { if (this.enabled && this.env.visibilityState() === 'visible') void this.acquire() }

  constructor(private env: WakeLockEnvironment, enabled: boolean, private changed: (status: WakeLockStatus) => void) {
    this.enabled = enabled
    this.changed(env.request ? 'off' : 'unsupported')
  }

  start() {
    if (this.started) return
    this.started = true
    this.env.addVisibilityListener(this.visibility)
    if (this.enabled && this.env.visibilityState() === 'visible') void this.acquire()
  }

  setEnabled(enabled: boolean) {
    this.enabled = enabled
    this.generation += 1
    if (!enabled) {
      const sentinel = this.sentinel
      this.sentinel = null
      this.changed(this.env.request ? 'off' : 'unsupported')
      if (sentinel && !sentinel.released) void sentinel.release().catch(() => undefined)
      return
    }
    if (this.env.visibilityState() === 'visible') void this.acquire()
  }

  private async acquire() {
    if (!this.env.request || !this.enabled || this.sentinel && !this.sentinel.released) return
    const generation = ++this.generation
    this.changed('requesting')
    try {
      const sentinel = await this.env.request()
      if (!this.enabled || generation !== this.generation) {
        if (!sentinel.released) await sentinel.release().catch(() => undefined)
        return
      }
      this.sentinel = sentinel
      sentinel.addEventListener('release', () => {
        if (this.sentinel === sentinel) this.sentinel = null
        this.changed(this.enabled ? 'released' : 'off')
      })
      this.changed('active')
    } catch {
      if (generation === this.generation) this.changed('denied')
    }
  }

  stop() {
    if (!this.started) return
    this.started = false
    this.generation += 1
    this.env.removeVisibilityListener(this.visibility)
    const sentinel = this.sentinel
    this.sentinel = null
    if (sentinel && !sentinel.released) void sentinel.release().catch(() => undefined)
  }
}

export function browserWakeLockEnvironment(): WakeLockEnvironment {
  type NavigatorWithWakeLock = Navigator & { wakeLock?: { request: (kind: 'screen') => Promise<WakeLockSentinelLike> } }
  const wakeLock = (navigator as NavigatorWithWakeLock).wakeLock
  return {
    visibilityState: () => document.visibilityState,
    addVisibilityListener: (listener) => document.addEventListener('visibilitychange', listener),
    removeVisibilityListener: (listener) => document.removeEventListener('visibilitychange', listener),
    request: wakeLock ? () => wakeLock.request('screen') : undefined,
  }
}

/* ----- 7E panel settings (per browser profile = per device; never sent to the server) ----- */

export type PanelTheme = 'day' | 'night' | 'auto'
export type PanelOrientation = 'auto' | 'portrait' | 'landscape'
export type PanelSettings = { theme: PanelTheme; wake: boolean; launch: boolean; orientation: PanelOrientation; saverMinutes: 0 | 2 | 5 | 10 }

export const PANEL_SETTINGS_KEY = 'roomos:panel-settings:v2'
export const PANEL_SAVER_CHOICES = [0, 2, 5, 10] as const
export const DEFAULT_PANEL_SETTINGS: PanelSettings = { theme: 'auto', wake: false, launch: false, orientation: 'auto', saverMinutes: 5 }

/** Reads v2 settings; migrates the v1 night/wake flags; any corrupt value falls back to a default. */
export function readPanelSettings(storage: Pick<Storage, 'getItem'> | null): PanelSettings {
  const raw: unknown = (() => { try { return JSON.parse(storage?.getItem(PANEL_SETTINGS_KEY) ?? 'null') } catch { return null } })()
  const value = (raw && typeof raw === 'object' ? raw : {}) as Partial<Record<keyof PanelSettings, unknown>>
  const legacyNight = readPanelPreference(storage, PANEL_NIGHT_KEY, false)
  const legacyWake = readPanelPreference(storage, PANEL_WAKE_KEY, false)
  return {
    theme: value.theme === 'day' || value.theme === 'night' || value.theme === 'auto' ? value.theme : legacyNight ? 'night' : DEFAULT_PANEL_SETTINGS.theme,
    wake: typeof value.wake === 'boolean' ? value.wake : legacyWake,
    launch: typeof value.launch === 'boolean' ? value.launch : DEFAULT_PANEL_SETTINGS.launch,
    orientation: value.orientation === 'portrait' || value.orientation === 'landscape' || value.orientation === 'auto' ? value.orientation : 'auto',
    saverMinutes: (PANEL_SAVER_CHOICES as readonly unknown[]).includes(value.saverMinutes) ? value.saverMinutes as PanelSettings['saverMinutes'] : DEFAULT_PANEL_SETTINGS.saverMinutes,
  }
}

export function writePanelSettings(storage: Pick<Storage, 'setItem'> | null, settings: PanelSettings) {
  try { storage?.setItem(PANEL_SETTINGS_KEY, JSON.stringify(settings)) } catch { /* blocked storage keeps working in memory */ }
}

/** Auto theme: night between 21:00 and 07:00 local time (a visual theme; Android brightness is untouched). */
export function isNightAt(date: Date) {
  const hour = date.getHours()
  return hour >= 21 || hour < 7
}

export function resolveNight(theme: PanelTheme, date: Date) {
  return theme === 'night' || (theme === 'auto' && isNightAt(date))
}

/** Only a fresh app start on the default pages opens the panel; later navigation never redirects. */
export function shouldOpenPanel(settings: PanelSettings, path: string) {
  return settings.launch && (path === '/' || path === '/home')
}

export type FullscreenResult = { fullscreen: boolean; orientationLocked: boolean }
type OrientationLock = { lock?: (orientation: 'portrait' | 'landscape') => Promise<void>; unlock?: () => void }

/** Fullscreen and orientation lock need a user gesture and browser support; failures are reported, never thrown. */
export async function enterPanelFullscreen(orientation: PanelOrientation, doc: Document = document): Promise<FullscreenResult> {
  let fullscreen = Boolean(doc.fullscreenElement)
  try {
    if (!fullscreen && doc.documentElement.requestFullscreen) { await doc.documentElement.requestFullscreen({ navigationUI: 'hide' }); fullscreen = true }
  } catch { fullscreen = false }
  let orientationLocked = false
  const screenOrientation = (typeof screen !== 'undefined' ? screen.orientation : undefined) as unknown as OrientationLock | undefined
  if (fullscreen && orientation !== 'auto' && screenOrientation?.lock) {
    try { await screenOrientation.lock(orientation); orientationLocked = true } catch { orientationLocked = false }
  }
  return { fullscreen, orientationLocked }
}

export async function exitPanelFullscreen(doc: Document = document) {
  try { ((typeof screen !== 'undefined' ? screen.orientation : undefined) as unknown as OrientationLock | undefined)?.unlock?.() } catch { /* unsupported */ }
  try { if (doc.fullscreenElement) await doc.exitFullscreen() } catch { /* already exited */ }
}

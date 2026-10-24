import { describe, expect, it } from 'vitest'
import { DEFAULT_PANEL_SETTINGS, PANEL_NIGHT_KEY, PANEL_SETTINGS_KEY, PANEL_WAKE_KEY, PanelWakeLockController, enterPanelFullscreen, isNightAt, readPanelPreference, readPanelSettings, resolveNight, shouldOpenPanel, writePanelPreference, writePanelSettings, type WakeLockEnvironment, type WakeLockSentinelLike, type WakeLockStatus } from './panel-mode'

const settle = async () => { await Promise.resolve(); await Promise.resolve() }

function wakeFixture() {
  let visibility: DocumentVisibilityState = 'visible'
  let listener: (() => void) | null = null
  const sentinels: Array<WakeLockSentinelLike & { fire: () => void }> = []
  const env: WakeLockEnvironment = {
    visibilityState: () => visibility,
    addVisibilityListener: (next) => { listener = next },
    removeVisibilityListener: () => { listener = null },
    request: async () => {
      let releaseListener: () => void = () => undefined
      const sentinel = {
        released: false,
        release: async () => { sentinel.released = true; releaseListener() },
        addEventListener: (_name: 'release', next: () => void) => { releaseListener = next },
        fire: () => { sentinel.released = true; releaseListener() },
      }
      sentinels.push(sentinel)
      return sentinel
    },
  }
  return { env, sentinels, setVisibility: (next: DocumentVisibilityState) => { visibility = next; listener?.() } }
}

describe('Phone 2 panel preferences and wake lock', () => {
  it('persists compact boolean preferences without depending on available storage', () => {
    const values = new Map<string, string>()
    const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value) } }
    expect(readPanelPreference(storage, 'night')).toBe(false)
    writePanelPreference(storage, 'night', true)
    expect(readPanelPreference(storage, 'night')).toBe(true)
    expect(readPanelPreference({ getItem: () => { throw new Error('blocked') } }, 'night', true)).toBe(true)
    expect(() => writePanelPreference({ setItem: () => { throw new Error('blocked') } }, 'night', true)).not.toThrow()
  })

  it('is opt-in, releases on disable and reacquires only after the panel is visible again', async () => {
    const fixture = wakeFixture()
    const statuses: WakeLockStatus[] = []
    const controller = new PanelWakeLockController(fixture.env, false, (status) => statuses.push(status))
    controller.start()
    expect(fixture.sentinels).toHaveLength(0)
    controller.setEnabled(true)
    await settle()
    expect(fixture.sentinels).toHaveLength(1)
    expect(statuses.at(-1)).toBe('active')
    fixture.setVisibility('hidden')
    fixture.sentinels[0].fire()
    expect(statuses.at(-1)).toBe('released')
    fixture.setVisibility('visible')
    await settle()
    expect(fixture.sentinels).toHaveLength(2)
    controller.setEnabled(false)
    await settle()
    expect(fixture.sentinels[1].released).toBe(true)
    expect(statuses.at(-1)).toBe('off')
    controller.stop()
  })

  it('labels browsers without Screen Wake Lock as unsupported', () => {
    const statuses: WakeLockStatus[] = []
    const controller = new PanelWakeLockController({
      visibilityState: () => 'visible', addVisibilityListener: () => undefined, removeVisibilityListener: () => undefined,
    }, true, (status) => statuses.push(status))
    controller.start()
    expect(statuses.at(-1)).toBe('unsupported')
  })
})

describe('panel settings (7E)', () => {
  const store = (entries: Record<string, string> = {}) => {
    const map = new Map(Object.entries(entries))
    return { getItem: (k: string) => map.get(k) ?? null, setItem: (k: string, v: string) => void map.set(k, v), map }
  }

  it('defaults safely, migrates v1 flags and rejects corrupt values', () => {
    expect(readPanelSettings(null)).toEqual(DEFAULT_PANEL_SETTINGS)
    expect(readPanelSettings(store({ [PANEL_NIGHT_KEY]: '1', [PANEL_WAKE_KEY]: '1' }))).toMatchObject({ theme: 'night', wake: true })
    expect(readPanelSettings(store({ [PANEL_SETTINGS_KEY]: '{not json' }))).toEqual(DEFAULT_PANEL_SETTINGS)
    expect(readPanelSettings(store({ [PANEL_SETTINGS_KEY]: JSON.stringify({ theme: 'disco', saverMinutes: 3, orientation: 'upside', launch: 'yes' }) }))).toEqual(DEFAULT_PANEL_SETTINGS)
  })

  it('round-trips settings on this device only', () => {
    const s = store()
    const next = { theme: 'day', wake: true, launch: true, orientation: 'landscape', saverMinutes: 10 } as const
    writePanelSettings(s, next)
    expect(readPanelSettings(s)).toEqual(next)
    expect(() => writePanelSettings({ setItem: () => { throw new Error('quota') } }, next)).not.toThrow()
  })

  it('auto theme switches at 21:00 and 07:00 local time', () => {
    expect(isNightAt(new Date(2026, 9, 15, 20, 59))).toBe(false)
    expect(isNightAt(new Date(2026, 9, 15, 21, 0))).toBe(true)
    expect(isNightAt(new Date(2026, 9, 16, 6, 59))).toBe(true)
    expect(isNightAt(new Date(2026, 9, 16, 7, 0))).toBe(false)
    expect(resolveNight('day', new Date(2026, 9, 15, 23, 0))).toBe(false)
    expect(resolveNight('night', new Date(2026, 9, 15, 12, 0))).toBe(true)
  })

  it('opens the panel only on a fresh start of the default pages', () => {
    const on = { ...DEFAULT_PANEL_SETTINGS, launch: true }
    expect(shouldOpenPanel(on, '/home')).toBe(true)
    expect(shouldOpenPanel(on, '/')).toBe(true)
    expect(shouldOpenPanel(on, '/lights')).toBe(false)
    expect(shouldOpenPanel(DEFAULT_PANEL_SETTINGS, '/home')).toBe(false)
  })

  it('reports fullscreen and orientation-lock failures instead of throwing', async () => {
    const refused = { fullscreenElement: null, documentElement: { requestFullscreen: () => Promise.reject(new Error('gesture')) } } as unknown as Document
    expect(await enterPanelFullscreen('portrait', refused)).toEqual({ fullscreen: false, orientationLocked: false })
    const missing = { fullscreenElement: null, documentElement: {} } as unknown as Document
    expect(await enterPanelFullscreen('auto', missing)).toEqual({ fullscreen: false, orientationLocked: false })
    const granted = { fullscreenElement: null, documentElement: { requestFullscreen: () => Promise.resolve() } } as unknown as Document
    expect((await enterPanelFullscreen('auto', granted)).fullscreen).toBe(true)
  })
})

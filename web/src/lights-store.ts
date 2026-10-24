import { describeGroupResult, type LightAction } from './ui'
import { canAct, canAllOff, canAllOn, displayName, effectLabel, isNewer, observationTime, pageControl, type LightsControl } from './lights-state'
import { MAIN_GROUP_ID, MAIN_LIGHT_IDS, summarizeMainLights } from './main-lights'
import type { ConnectionStatus, GroupCommandResult, Health, Light, RoomOSEvent, SceneActivationResult } from './types'

/**
 * Event-driven Lights data: one bounded bootstrap of /lights and /health, then WebSocket
 * light.state_changed events update local state. Authoritative refresh happens only on
 * (re)connect and return to foreground via the lifecycle — never after ordinary events.
 * Commands are never optimistic: the light changes only when authoritative state arrives.
 */
export interface LightsApi {
  lights: () => Promise<Light[]>
  health: () => Promise<Health>
  power: (id: string, on: boolean) => Promise<unknown>
  brightness: (id: string, brightness: number) => Promise<unknown>
  color: (id: string, rgb: [number, number, number]) => Promise<unknown>
  temperature: (id: string, kelvin: number) => Promise<unknown>
  effect: (id: string, effect: string) => Promise<unknown>
  allPower: (on: boolean) => Promise<GroupCommandResult>
  /** 7E.1 main_room_lights group: one request controls the four L530 room lights. */
  mainPower: (on: boolean) => Promise<GroupCommandResult>
  mainBrightness: (brightness: number) => Promise<GroupCommandResult>
  mainTemperature: (kelvin: number) => Promise<GroupCommandResult>
  activateScene: (id: 'all_off') => Promise<SceneActivationResult>
}

export type Feedback = { tone: 'pending' | 'success' | 'partial' | 'error'; title: string; detail?: string }
export type MainAction = 'power' | 'brightness' | 'temperature'
export type Pending = { lightId: string; action: LightAction; startedAt: number; acknowledged: boolean }
  | { lightId: null; action: 'all_on' | 'all_off' | 'main_power' | 'main_brightness' | 'main_temperature'; startedAt: number; acknowledged: boolean }

export type LightsSnapshot = {
  lights: Light[] | null
  lightsStatus: 'loading' | 'ready' | 'error'
  control: Health | null
  controlStatus: 'loading' | 'ready' | 'error'
  connection: ConnectionStatus
  trusted: boolean
  refreshing: boolean
  pending: Pending | null
  feedback: { scope: string; value: Feedback } | null
  announcement: { id: number; text: string } | null
}

export const CONFIRM_TIMEOUT_MS = 10_000
const REQUEST_TIMEOUT_MS = 15_000

export class LightsStore {
  private snapshot: LightsSnapshot
  private listeners = new Set<() => void>()
  private inflight: Promise<void> | null = null
  private epoch = 0
  private loadEpoch = -1
  private queued: RoomOSEvent[] = []
  private announcements = 0
  private expectation: { lightId: string; baseline: Light; matches: (light: Light) => boolean } | null = null
  /** Group command: every succeeded member must report a newer matching observation. */
  private groupExpectation: { waiting: Map<string, Light>; matches: (light: Light) => boolean } | null = null
  private confirmTimer: ReturnType<typeof setTimeout> | null = null

  constructor(private api: LightsApi, private scopes: readonly string[], private authResolved: boolean, connection: ConnectionStatus = 'reconnecting', private now: () => number = Date.now) {
    this.snapshot = { lights: null, lightsStatus: 'loading', control: null, controlStatus: 'loading', connection, trusted: false, refreshing: false, pending: null, feedback: null, announcement: null }
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  getSnapshot = () => this.snapshot

  private update(change: Partial<LightsSnapshot>) {
    this.snapshot = { ...this.snapshot, ...change }
    this.listeners.forEach((listener) => listener())
  }

  /** Single-flight authoritative load; a load started before a disconnect never re-trusts state. */
  load(): Promise<void> {
    if (this.inflight) return this.loadEpoch === this.epoch ? this.inflight : this.inflight.then(() => this.load())
    const epoch = this.epoch
    this.loadEpoch = epoch
    this.update({ refreshing: true, trusted: false, lightsStatus: 'loading', controlStatus: 'loading' })
    const run = Promise.allSettled([
      bounded(this.api.lights()).then((lights) => { if (epoch === this.epoch) this.update({ lights: lights.map((next) => {
        const previous = this.snapshot.lights?.find((item) => item.id === next.id)
        const a = observationTime(previous?.last_updated ?? null), b = observationTime(next.last_updated)
        return previous && a !== null && b !== null && a > b ? previous : next
      }), lightsStatus: 'ready' }) },
        () => { if (epoch === this.epoch) this.update({ lightsStatus: 'error' }) }),
      bounded(this.api.health()).then((control) => { if (epoch === this.epoch) this.update({ control, controlStatus: 'ready' }) },
        () => { if (epoch === this.epoch) this.update({ controlStatus: 'error' }) }),
    ]).then(() => {
      this.inflight = null // A queued HA reconnect may start a fresh authoritative load.
      const events = this.queued.splice(0)
      const ok = this.snapshot.lightsStatus === 'ready' && this.snapshot.controlStatus === 'ready'
      this.update({ refreshing: false, trusted: epoch === this.epoch && ok && this.snapshot.connection === 'connected' })
      if (epoch === this.epoch) {
        events.forEach((event) => this.handleEvent(event))
        this.snapshot.lights?.forEach((light) => this.confirm(light))
      }
    })
    this.inflight = run
    return this.inflight
  }

  setConnection(connection: ConnectionStatus) {
    if (connection === this.snapshot.connection) return
    this.epoch += 1
    this.update({ connection, trusted: false })
  }

  page(): LightsControl {
    return pageControl({ authResolved: this.authResolved, scopes: this.scopes, connection: this.snapshot.connection, control: this.snapshot.control, trusted: this.snapshot.trusted && !this.snapshot.refreshing })
  }

  can(light: Light, action: LightAction) {
    const current = this.snapshot.lights?.find((item) => item.id === light.id)
    return Boolean(current && canAct(this.page(), this.snapshot.control, current, action, this.snapshot.pending !== null) && canAct(this.page(), this.snapshot.control, light, action, false))
  }

  canAllOn() { return canAllOn(this.page(), this.snapshot.control, this.snapshot.pending !== null) && Boolean(this.snapshot.lights?.some((light) => light.available && light.capabilities.power)) }
  canAllOff() { return canAllOff(this.page(), this.snapshot.control, this.snapshot.lights, this.snapshot.pending !== null) && Boolean(this.snapshot.lights?.some((light) => light.available && light.capabilities.power)) }

  handleEvent(event: RoomOSEvent) {
    if (this.snapshot.refreshing) { this.queued.push(event); return }
    if (event.type === 'light.state_changed') {
      const next = event.data.light
      if (!next || !this.snapshot.lights) return
      const current = this.snapshot.lights.find((light) => light.id === next.id)
      if (!current || !isNewer(next, current)) return // unknown device or stale observation
      this.update({ lights: this.snapshot.lights.map((light) => light.id === next.id ? next : light) })
      this.confirm(next)
      this.confirmGroup(next)
      return
    }
    if (event.type === 'roomos.connection') {
      const ha = event.data.home_assistant
      const control = this.snapshot.control
      if (!control || (ha !== 'connected' && ha !== 'disconnected') || control.home_assistant === ha) return
      // HA link changed: the displayed light state may be stale; re-read authoritatively.
      this.update({ control: { ...control, home_assistant: ha } })
      if (ha === 'connected') void this.load()
      else this.update({ trusted: false })
    }
    // Other event types (metrics, alerts, scenes…) are not Lights state.
  }

  /** A newer authoritative observation of the commanded light confirms an acknowledged command. */
  private confirm(light: Light) {
    const pending = this.snapshot.pending
    if (!pending || !pending.acknowledged) return
    // Acknowledgement and a newer matching observation are both required, in either arrival order.
    // A conflicting third-party change is not confirmation; no client/server clock comparison.
    const expected = this.expectation
    if (pending.lightId === null || pending.lightId !== light.id || !expected || light === expected.baseline || !expected.matches(light) || !this.page().ok) return
    this.finish(pending.lightId, { tone: 'success', title: `${displayName(light)} updated` })
  }

  private confirmGroup(light: Light) {
    const pending = this.snapshot.pending
    const expected = this.groupExpectation
    if (!pending || pending.lightId !== null || !pending.action.startsWith('main_') || !pending.acknowledged || !expected) return
    const baseline = expected.waiting.get(light.id)
    if (!baseline || light === baseline || !expected.matches(light)) return
    expected.waiting.delete(light.id)
    if (!expected.waiting.size) this.finish('main', { tone: 'success', title: 'Ana Oda Işıkları güncellendi' })
  }

  private finish(scope: string, value: Feedback) {
    if (this.confirmTimer) clearTimeout(this.confirmTimer)
    this.confirmTimer = null
    this.expectation = null
    this.groupExpectation = null
    this.announcements += 1
    this.update({ pending: null, feedback: { scope, value }, announcement: { id: this.announcements, text: value.title + (value.detail ? `. ${value.detail}` : '') + '.' } })
  }

  private async command(light: Light, action: LightAction, label: string, send: () => Promise<unknown>, matches: (state: Light) => boolean) {
    const current = this.snapshot.lights?.find((item) => item.id === light.id)
    if (!current || this.snapshot.pending || !this.can(current, action)) return false
    this.expectation = { lightId: light.id, baseline: current, matches }
    const startedAt = this.now()
    this.update({ pending: { lightId: light.id, action, startedAt, acknowledged: false }, feedback: { scope: light.id, value: { tone: 'pending', title: `${label}…` } } })
    try {
      await bounded(send())
    } catch (cause) {
      if (cause instanceof Error && cause.message === 'Request timed out') this.update({ trusted: false })
      this.finish(light.id, { tone: 'error', title: `${displayName(light)}: ${label.toLowerCase()} failed`, detail: safeReason(cause) })
      return true
    }
    if (!this.snapshot.pending) return true
    // Accepted by RoomOS; wait for the light's authoritative state rather than assuming success.
    this.update({ pending: { lightId: light.id, action, startedAt, acknowledged: true }, feedback: { scope: light.id, value: { tone: 'pending', title: 'Sent · waiting for the light…' } } })
    const observed = this.snapshot.lights?.find((item) => item.id === light.id)
    if (observed) this.confirm(observed) // WS often arrives before the HTTP acknowledgement.
    if (!this.snapshot.pending) return true
    this.confirmTimer = setTimeout(() => {
      if (this.snapshot.pending?.lightId === light.id) {
        this.update({ trusted: false })
        this.finish(light.id, { tone: 'partial', title: `${displayName(light)}: no confirmation yet`, detail: 'Refresh status to check the light before another command.' })
      }
    }, CONFIRM_TIMEOUT_MS)
    return true
  }

  power(light: Light, on: boolean) { return this.command(light, 'power', on ? 'Turning on' : 'Turning off', () => this.api.power(light.id, on), (state) => state.available && state.on === on) }
  brightness(light: Light, value: number) {
    if (!Number.isFinite(value)) return Promise.resolve(false)
    const bounded255 = Math.max(1, Math.min(255, Math.round(value)))
    return this.command(light, 'brightness', 'Setting brightness', () => this.api.brightness(light.id, bounded255), (state) => state.available && state.on === true && state.brightness !== null && Math.abs(state.brightness - bounded255) <= 1)
  }
  color(light: Light, rgb: [number, number, number]) {
    if (!rgb.every((channel) => Number.isInteger(channel) && channel >= 0 && channel <= 255)) return Promise.resolve(false)
    return this.command(light, 'color', 'Setting colour', () => this.api.color(light.id, rgb), (state) => state.available && state.on === true && ['hs', 'rgb', 'rgbw', 'rgbww', 'xy'].includes(state.color_mode ?? '') && Boolean(state.rgb?.every((v, i) => Math.abs(v - rgb[i]) <= 4)))
  }
  temperature(light: Light, kelvin: number) {
    const current = this.snapshot.lights?.find((item) => item.id === light.id)
    if (!current || !Number.isFinite(kelvin) || kelvin < current.capabilities.min_kelvin || kelvin > current.capabilities.max_kelvin) return Promise.resolve(false)
    return this.command(light, 'temperature', 'Setting white temperature', () => this.api.temperature(light.id, Math.round(kelvin)), (state) => state.available && state.on === true && state.color_mode === 'color_temp' && state.color_temperature_kelvin !== null && Math.abs(state.color_temperature_kelvin - kelvin) <= 50)
  }
  effect(light: Light, effect: string) {
    const current = this.snapshot.lights?.find((item) => item.id === light.id)
    if (!current || !current.effects.includes(effect) || current.effect === effect) return Promise.resolve(false)
    return this.command(light, 'effect', `Setting ${effectLabel(effect)}`, () => this.api.effect(light.id, effect), (state) => state.available && state.on === true && state.effect === effect)
  }

  async allOn() {
    if (!this.canAllOn()) return false
    this.update({ pending: { lightId: null, action: 'all_on', startedAt: this.now(), acknowledged: false }, feedback: { scope: 'group', value: { tone: 'pending', title: 'Turning every light on…' } } })
    try {
      const described = describeGroupResult(await bounded(this.api.allPower(true)))
      this.finish('group', { tone: described.tone, title: described.tone === 'success' ? 'All on sent' : described.title, detail: described.detail })
    } catch (cause) {
      this.finish('group', { tone: 'error', title: 'All on failed', detail: safeReason(cause) })
    }
    return true
  }

  /** Same semantics as Home: the all_off scene (execution history, Activity correlation). */
  async allOff() {
    if (!this.canAllOff()) return false
    this.update({ pending: { lightId: null, action: 'all_off', startedAt: this.now(), acknowledged: false }, feedback: { scope: 'group', value: { tone: 'pending', title: 'Turning every light off…' } } })
    try {
      const result = await bounded(this.api.activateScene('all_off'))
      if (result.status === 'success') this.finish('group', { tone: 'success', title: 'All off applied' })
      else if (result.status === 'partial') this.finish('group', { tone: 'partial', title: 'All off partially applied', detail: [result.skipped.length ? `${result.skipped.length} unavailable` : '', result.failed.length ? `${result.failed.length} failed` : ''].filter(Boolean).join(' · ') })
      else this.finish('group', { tone: 'error', title: 'All off could not be applied' })
    } catch (cause) {
      this.finish('group', { tone: 'error', title: 'All off failed', detail: safeReason(cause) })
    }
    return true
  }

  /** Group writes: same page preconditions plus the main_room_lights policy. Attribute changes need
   *  every available member on, because the group endpoints use turn_on and would power lights on. */
  canMain(action: MainAction) {
    const summary = summarizeMainLights(this.snapshot.lights)
    if (!summary || !this.page().ok || this.snapshot.pending !== null || summary.available === 0) return false
    if (!this.snapshot.control?.writable_permissions?.[MAIN_GROUP_ID]?.includes(action)) return false
    const members = summary.members.filter((light) => light.available)
    if (action === 'power') return members.every((light) => light.capabilities.power)
    if (summary.power !== 'on') return false
    return members.every((light) => light.capabilities[action])
  }

  mainPower(on: boolean) {
    return this.groupCommand('power', on ? 'Ana Oda Işıkları açılıyor' : 'Ana Oda Işıkları kapanıyor', () => this.api.mainPower(on), (state) => state.available && state.on === on)
  }

  mainBrightness(percent: number) {
    if (!Number.isFinite(percent)) return Promise.resolve(false)
    const value = Math.max(1, Math.min(255, Math.round((Math.max(1, Math.min(100, percent)) / 100) * 255)))
    return this.groupCommand('brightness', 'Parlaklık ayarlanıyor', () => this.api.mainBrightness(value), (state) => state.available && state.on === true && state.brightness !== null && Math.abs(state.brightness - value) <= 1)
  }

  mainTemperature(kelvin: number) {
    const summary = summarizeMainLights(this.snapshot.lights)
    if (!summary || !Number.isFinite(kelvin) || kelvin < summary.minKelvin || kelvin > summary.maxKelvin) return Promise.resolve(false)
    const value = Math.round(kelvin)
    return this.groupCommand('temperature', 'Renk sıcaklığı ayarlanıyor', () => this.api.mainTemperature(value), (state) => state.available && state.on === true && state.color_mode === 'color_temp' && state.color_temperature_kelvin !== null && Math.abs(state.color_temperature_kelvin - value) <= 50)
  }

  /** Never optimistic, never retried. Anything short of full confirmation re-reads the real state once. */
  private async groupCommand(action: MainAction, label: string, send: () => Promise<GroupCommandResult>, matches: (light: Light) => boolean) {
    if (!this.canMain(action)) return false
    const kind = `main_${action}` as const
    const startedAt = this.now()
    const baseline = new Map((this.snapshot.lights ?? []).filter((light) => (MAIN_LIGHT_IDS as readonly string[]).includes(light.id)).map((light) => [light.id, light]))
    this.update({ pending: { lightId: null, action: kind, startedAt, acknowledged: false }, feedback: { scope: 'main', value: { tone: 'pending', title: `${label}…` } } })
    let result: GroupCommandResult
    try {
      result = await bounded(send())
    } catch (cause) {
      if (cause instanceof Error && cause.message === 'Request timed out') this.update({ trusted: false })
      this.finish('main', { tone: 'error', title: 'Ana Oda Işıkları: komut başarısız', detail: safeReason(cause) })
      void this.load()
      return true
    }
    if (result.failed.length) {
      const names = (reason: string) => result.failed.filter((item) => item.reason === reason).map((item) => displayName({ id: item.device_id, name: item.device_id }))
      const unavailable = names('unavailable'), failed = names('service_failed')
      const detail = [failed.length ? `${failed.length} ışık yanıt vermedi: ${failed.join(', ')}` : '', unavailable.length ? `${unavailable.length} erişilemiyor: ${unavailable.join(', ')}` : ''].filter(Boolean).join(' · ')
      // Nothing succeeded: that is a failure, not a partial result (7L: was reported as "kısmen").
      this.finish('main', result.succeeded.length
        ? { tone: 'partial', title: 'Ana Oda Işıkları: kısmen uygulandı', detail }
        : { tone: 'error', title: 'Ana Oda Işıkları: uygulanamadı', detail })
      void this.load()
      return true
    }
    const waiting = new Map(result.succeeded.filter((id) => baseline.has(id)).map((id) => [id, baseline.get(id)!]))
    this.groupExpectation = { waiting, matches }
    this.update({ pending: { lightId: null, action: kind, startedAt, acknowledged: true }, feedback: { scope: 'main', value: { tone: 'pending', title: 'Gönderildi · ışıklar bekleniyor…' } } })
    // Events may have arrived before the acknowledgement.
    for (const light of this.snapshot.lights ?? []) this.confirmGroup(light)
    if (!this.snapshot.pending) return true
    this.confirmTimer = setTimeout(() => {
      if (this.snapshot.pending?.action === kind) {
        this.finish('main', { tone: 'partial', title: 'Ana Oda Işıkları: onay gelmedi', detail: 'Gerçek durum yeniden okunuyor.' })
        void this.load()
      }
    }, CONFIRM_TIMEOUT_MS)
    return true
  }

  dismiss() { this.update({ feedback: null }) }
}

function safeReason(cause: unknown) {
  if (cause instanceof Error && (cause.name === 'ApiError' || cause.message.startsWith('RoomOS is offline') || cause.message === 'Request timed out')) return cause.message
  return 'Please try again.'
}

/** Bound waiting; never retry or replay a control command after a timeout. */
function bounded<T>(promise: Promise<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Request timed out')), REQUEST_TIMEOUT_MS)
    promise.then(resolve, reject).finally(() => clearTimeout(timer))
  })
}

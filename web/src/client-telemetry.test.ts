import { describe, expect, it, vi } from 'vitest'
import { collectPwaSensors, nextSequence, PwaTelemetryReporter, pwaPayload, type BrowserLike, type TelemetryDeps } from './client-telemetry'

const android = (change: Partial<BrowserLike> = {}): BrowserLike => ({
  onLine: true,
  getBattery: async () => ({ level: 0.76, charging: true }),
  connection: { type: 'wifi' },
  userAgentData: { getHighEntropyValues: async () => ({ platform: 'Android', platformVersion: '14.0.0', model: 'Pixel Tablet' }) },
  ...change,
})

describe('PWA sensors (Chrome for Android)', () => {
  it('reads battery, network and UA client hints', async () => {
    expect(await collectPwaSensors(android())).toEqual({
      battery_percent: 76, charging: true, network_type: 'wifi', network_connected: true, android_version: '14.0.0', device_model: 'Pixel Tablet',
    })
  })

  it('reports null, never 0, when the browser lacks an API', async () => {
    const sensors = await collectPwaSensors({ onLine: false })
    expect(sensors).toEqual({ battery_percent: null, charging: null, network_type: null, network_connected: false, android_version: null, device_model: null })
    const failing = await collectPwaSensors(android({ getBattery: () => Promise.reject(new Error('blocked')), connection: { type: 'unknown' } }))
    expect(failing.battery_percent).toBeNull()
    expect(failing.network_type).toBeNull()
  })

  it('rejects odd hint values and non-Android platforms', async () => {
    const hints = (values: Record<string, string>) => android({ userAgentData: { getHighEntropyValues: async () => values } })
    expect((await collectPwaSensors(hints({ platform: 'Windows', platformVersion: '15.0.0', model: '' }))).android_version).toBeNull()
    expect((await collectPwaSensors(hints({ platform: 'Android', platformVersion: '14', model: '<b>x</b>' }))).device_model).toBeNull()
  })

  it('builds a payload without any device identifier', () => {
    const payload = pwaPayload({ battery_percent: 1, charging: false, network_type: null, network_connected: true, android_version: null, device_model: null }, 5, new Date('2026-10-15T09:00:00Z'))
    expect(payload).toEqual(expect.objectContaining({ schema_version: 1, sequence: 5, measured_at: '2026-10-15T09:00:00.000Z', next_report_seconds: 60 }))
    expect(JSON.stringify(payload)).not.toMatch(/device_id|token|scope/)
  })

  it('keeps the sequence strictly increasing even if the clock goes back', () => {
    const store = new Map<string, string>()
    const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) }
    expect(nextSequence(1000, storage)).toBe(1000)
    expect(nextSequence(10, storage)).toBe(1001)
    expect(nextSequence(5000.7, null)).toBe(5000)
  })
})

describe('PwaTelemetryReporter', () => {
  type Timer = { id: number; callback: () => void; at: number }
  function setup(send: TelemetryDeps['send'], shared = new Map<string, string>()) {
    const clock = { now: Date.parse('2026-10-15T09:00:00Z') }
    const timers: Timer[] = []
    let next = 1
    const storage = { getItem: (k: string) => shared.get(k) ?? null, setItem: (k: string, v: string) => void shared.set(k, v) }
    const deps: TelemetryDeps = {
      browser: android(), send, storage, now: () => clock.now,
      setTimer: (callback, ms) => { timers.push({ id: next, callback, at: clock.now + ms }); return next++ },
      clearTimer: (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1) },
    }
    /** Advance the fake clock, firing due timers in order. */
    const advance = async (ms: number) => {
      const end = clock.now + ms
      for (;;) {
        timers.sort((a, b) => a.at - b.at)
        const due = timers[0]
        if (!due || due.at > end) break
        timers.shift(); clock.now = due.at; due.callback(); await flush()
      }
      clock.now = end
    }
    return { reporter: new PwaTelemetryReporter(deps), timers, advance, clock, shared, deps }
  }
  const flush = async () => { for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0)) }

  it('reports at once, then once a minute, only while visible', async () => {
    const send = vi.fn(async () => undefined)
    const { reporter, timers, advance } = setup(send)
    reporter.setActive(true)
    reporter.setActive(true) // repeated visibility events do not stack timers
    await advance(0)
    expect(send).toHaveBeenCalledTimes(1)
    await advance(60_000)
    expect(send).toHaveBeenCalledTimes(2)
    reporter.setActive(false) // hidden: no background polling
    expect(timers).toHaveLength(0)
  })

  it('a reconnect or visibility flap right after a report does not send again (Phone 2 12:09:29)', async () => {
    const send = vi.fn(async () => undefined)
    const { reporter, advance } = setup(send)
    reporter.setActive(true); await advance(0)
    await advance(3_000)
    reporter.setActive(false); reporter.setActive(true) // WebSocket dropped and came back
    await advance(56_000)
    expect(send).toHaveBeenCalledTimes(1) // the last report is still fresh: keep the cadence
    await advance(1_001)
    expect(send).toHaveBeenCalledTimes(2) // one minute after the last accepted report
  })

  it('a page reload (service-worker takeover) respects the persisted gap (Phone 2 12:09:32)', async () => {
    const send = vi.fn(async () => undefined)
    const first = setup(send)
    first.reporter.setActive(true); await first.advance(0)
    // New page, same origin storage, 6 s later.
    const reloaded = setup(send, first.shared)
    reloaded.clock.now = first.clock.now + 6_000
    reloaded.reporter.setActive(true)
    await reloaded.advance(53_000)
    expect(send).toHaveBeenCalledTimes(1)
    await reloaded.advance(1_001)
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('two reporters on one page never send concurrently (remount)', async () => {
    let release: () => void = () => undefined
    const send = vi.fn(() => new Promise<void>((resolve) => { release = resolve }))
    const shared = new Map<string, string>()
    const a = setup(send, shared); const b = setup(send, shared)
    b.clock.now = a.clock.now
    a.reporter.setActive(true); b.reporter.setActive(true)
    await a.advance(0); await b.advance(0)
    expect(send).toHaveBeenCalledTimes(1)
    release(); await flush()
    a.reporter.setActive(false); b.reporter.setActive(false)
  })

  it('honours Retry-After on 429', async () => {
    const send = vi.fn<() => Promise<void>>(async () => { throw Object.assign(new Error('slow down'), { status: 429, retryAfter: 90 }) })
    const { reporter, advance } = setup(send)
    reporter.setActive(true); await advance(0)
    send.mockImplementation(async () => undefined)
    await advance(89_000)
    expect(send).toHaveBeenCalledTimes(1)
    await advance(1_001)
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('a failed attempt still enforces the 16 s gap on reload, then reports', async () => {
    const send = vi.fn<() => Promise<void>>(async () => { throw new Error('offline') })
    const first = setup(send)
    first.reporter.setActive(true); await first.advance(0)
    const reloaded = setup(send, first.shared)
    reloaded.clock.now = first.clock.now + 4_000
    send.mockImplementation(async () => undefined)
    reloaded.reporter.setActive(true)
    await reloaded.advance(11_000)
    expect(send).toHaveBeenCalledTimes(1)
    await reloaded.advance(1_001)
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('returning after a long hidden period reports at once', async () => {
    const send = vi.fn(async () => undefined)
    const { reporter, advance } = setup(send)
    reporter.setActive(true); await advance(0)
    reporter.setActive(false); await advance(300_000)
    reporter.setActive(true); await advance(0)
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('ignores a corrupt or far-future stored time', async () => {
    const send = vi.fn(async () => undefined)
    const shared = new Map([['roomos.telemetry.notBefore', String(Date.parse('2030-01-01T00:00:00Z'))]])
    const { reporter, advance } = setup(send, shared)
    reporter.setActive(true); await advance(0)
    expect(send).toHaveBeenCalledTimes(1)
  })

  it('stops on 403 until scopes change, keeps going after network errors', async () => {
    const send = vi.fn<() => Promise<void>>(async () => { throw Object.assign(new Error('denied'), { status: 403 }) })
    const { reporter, timers, advance } = setup(send)
    reporter.setActive(true); await advance(0)
    expect(timers).toHaveLength(0)
    reporter.setActive(true); await advance(60_000)
    expect(send).toHaveBeenCalledTimes(1)
    reporter.reset()
    send.mockImplementation(async () => { throw new Error('offline') })
    reporter.setActive(true); await advance(16_000)
    expect(send).toHaveBeenCalledTimes(2)
    expect(timers).toHaveLength(1) // dropped, next tick measures again
  })
})

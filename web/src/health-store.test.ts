import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { HealthStore, HEALTH_POLL_MS, HEALTH_STALE_MS, HEALTH_TIMEOUT_MS } from './health-store'
import type { HealthReport } from './types'

const epoch = Date.parse('2026-10-13T12:00:00Z')
const report = (at = Date.now()): HealthReport => ({ overall: 'healthy', checked_at: new Date(at).toISOString(), components: [] })
const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve() }
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function fixture(initiallyVisible = true) {
  let visible = initiallyVisible
  let listener: (() => void) | undefined
  const load = vi.fn<(signal: AbortSignal) => Promise<HealthReport>>().mockImplementation(async () => report())
  const unlisten = vi.fn(() => { listener = undefined })
  const env = {
    now: () => Date.now(), monotonic: () => performance.now(), visible: () => visible,
    listen: (fn: () => void) => { listener = fn; return unlisten },
  }
  const store = new HealthStore(load, env)
  const visibility = (value: boolean) => { visible = value; listener?.() }
  return { store, load, visibility, unlisten }
}

describe('Health reliability lifecycle', () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(epoch) })
  afterEach(() => { vi.clearAllTimers(); vi.useRealTimers() })

  it('loads successfully and records independent attempt/success times', async () => {
    const { store, load } = fixture()
    const pending = deferred<HealthReport>(); load.mockReturnValueOnce(pending.promise)
    store.start(); await flush()
    expect(store.getSnapshot()).toMatchObject({ refreshing: true, lastAttemptAt: epoch, lastSuccessAt: null })
    await vi.advanceTimersByTimeAsync(500)
    pending.resolve(report()); await flush()
    expect(store.getSnapshot()).toMatchObject({ freshness: 'fresh', lastAttemptAt: epoch, lastSuccessAt: epoch + 500, lastAttemptResult: 'success', ageMs: 0 })
    store.stop()
  })

  it('reports first-load failure without data and redacts arbitrary error text', async () => {
    const { store, load } = fixture(); load.mockRejectedValueOnce(new Error('private bearer secret'))
    store.start(); await flush()
    expect(store.getSnapshot()).toMatchObject({ report: null, freshness: 'unavailable', lastSuccessAt: null, lastAttemptResult: 'error' })
    expect(store.getSnapshot().error).not.toContain('secret')
    store.stop()
  })

  it('keeps failed refresh data historical and recovers on automatic retry', async () => {
    const { store, load } = fixture(); store.start(); await flush()
    load.mockRejectedValueOnce(new Error('offline'))
    await vi.advanceTimersByTimeAsync(HEALTH_POLL_MS)
    expect(store.getSnapshot()).toMatchObject({ freshness: 'stale', lastSuccessAt: epoch, lastAttemptResult: 'error' })
    const waiting = deferred<HealthReport>(); load.mockReturnValueOnce(waiting.promise)
    await vi.advanceTimersByTimeAsync(15_000)
    expect(store.getSnapshot()).toMatchObject({ freshness: 'stale', refreshing: true })
    await vi.advanceTimersByTimeAsync(1_000)
    expect(store.getSnapshot().freshness).toBe('stale')
    waiting.resolve(report()); await flush()
    expect(store.getSnapshot()).toMatchObject({ freshness: 'fresh', error: null, lastAttemptResult: 'success' })
    store.stop()
  })

  it('manual Retry immediately cancels scheduled backoff and is single-flight', async () => {
    const { store, load } = fixture(); load.mockRejectedValueOnce(new Error('offline'))
    store.start(); await flush()
    const pending = deferred<HealthReport>(); load.mockReturnValueOnce(pending.promise)
    store.refresh(); store.refresh(); store.refresh(); await flush()
    expect(load).toHaveBeenCalledTimes(2)
    pending.resolve(report()); await flush()
    await vi.advanceTimersByTimeAsync(15_000)
    expect(load).toHaveBeenCalledTimes(2)
    expect(store.getSnapshot().freshness).toBe('fresh')
    store.stop()
  })

  it('pauses hidden polling, ages retained data, and refreshes once on return', async () => {
    const { store, load, visibility } = fixture(); store.start(); await flush()
    visibility(false); await vi.advanceTimersByTimeAsync(HEALTH_STALE_MS * 3)
    expect(load).toHaveBeenCalledTimes(1)
    const pending = deferred<HealthReport>(); load.mockReturnValueOnce(pending.promise)
    visibility(true); visibility(true); await flush()
    expect(load).toHaveBeenCalledTimes(2)
    expect(store.getSnapshot().freshness).toBe('stale')
    pending.resolve(report()); await flush()
    expect(store.getSnapshot().freshness).toBe('fresh')
    store.stop()
  })

  it('does not fetch when initially hidden; first visibility starts one request', async () => {
    const { store, load, visibility } = fixture(false); store.start(); await flush()
    await vi.advanceTimersByTimeAsync(HEALTH_STALE_MS)
    expect(load).not.toHaveBeenCalled()
    expect(store.getSnapshot().lastAttemptResult).toBe('none')
    visibility(true); await flush(); expect(load).toHaveBeenCalledTimes(1)
    store.stop()
  })

  it('aborts in-flight work on hiding and ignores late old results', async () => {
    const { store, load, visibility } = fixture()
    const old = deferred<HealthReport>(); load.mockReturnValueOnce(old.promise)
    store.start(); await flush(); const signal = load.mock.calls[0][0]
    visibility(false); expect(signal.aborted).toBe(true)
    visibility(true); await flush(); const latest = store.getSnapshot().report
    old.resolve({ ...report(epoch - 999_999), overall: 'unhealthy' }); await flush()
    expect(store.getSnapshot().report).toBe(latest)
    expect(store.getSnapshot().freshness).toBe('fresh')
    store.stop()
  })

  it('timeouts abort the fetch, release the single-flight lock and ignore late completion', async () => {
    const { store, load } = fixture(); const old = deferred<HealthReport>()
    load.mockReturnValueOnce(old.promise); store.start(); await flush()
    await vi.advanceTimersByTimeAsync(HEALTH_TIMEOUT_MS)
    expect(load.mock.calls[0][0].aborted).toBe(true)
    expect(store.getSnapshot()).toMatchObject({ refreshing: false, freshness: 'unavailable', lastAttemptResult: 'error' })
    expect(store.getSnapshot().error).toContain('timed out')
    store.refresh(); await flush(); const current = store.getSnapshot().report
    old.resolve(report(epoch - 999_999)); await flush()
    expect(store.getSnapshot().report).toBe(current)
    store.stop()
  })

  it('ages a successful report without needing a subsequent API result', async () => {
    const { store, load } = fixture()
    load.mockResolvedValueOnce(report(epoch - HEALTH_STALE_MS + 2_000))
    store.start(); await flush(); expect(store.getSnapshot().freshness).toBe('fresh')
    await vi.advanceTimersByTimeAsync(2_000)
    expect(store.getSnapshot().freshness).toBe('stale')
    expect(load).toHaveBeenCalledTimes(1)
    store.stop()
  })

  it.each([epoch - HEALTH_STALE_MS, epoch + 60_000])('does not trust old or future source timestamp %s', async (at) => {
    const { store, load } = fixture(); load.mockResolvedValueOnce(report(at))
    store.start(); await flush()
    expect(store.getSnapshot()).toMatchObject({ freshness: 'stale', lastAttemptResult: 'success' })
    store.stop()
  })

  it('invalidates freshness on backwards wall-clock change', async () => {
    const { store } = fixture(); store.start(); await flush()
    vi.setSystemTime(epoch - 60_000); await vi.advanceTimersByTimeAsync(1_000)
    expect(store.getSnapshot().freshness).toBe('stale')
    vi.setSystemTime(epoch + 10_000); await vi.advanceTimersByTimeAsync(1_000)
    expect(store.getSnapshot().freshness).toBe('stale')
    store.stop()
  })

  it('backs off failures at 15/30/60 seconds and resets after recovery', async () => {
    const { store, load } = fixture(); load.mockRejectedValue(new Error('offline'))
    store.start(); await flush()
    for (const [delay, count] of [[15_000, 2], [30_000, 3], [60_000, 4], [60_000, 5]]) {
      await vi.advanceTimersByTimeAsync(delay - 1); expect(load).toHaveBeenCalledTimes(count - 1)
      await vi.advanceTimersByTimeAsync(1); expect(load).toHaveBeenCalledTimes(count)
    }
    load.mockImplementation(async () => report()); store.refresh(); await flush()
    load.mockRejectedValueOnce(new Error('again'))
    await vi.advanceTimersByTimeAsync(HEALTH_POLL_MS)
    const count = load.mock.calls.length
    await vi.advanceTimersByTimeAsync(15_000)
    expect(load).toHaveBeenCalledTimes(count + 1)
    expect(store.getSnapshot().freshness).toBe('fresh')
    store.stop()
  })

  it('cleanup removes timers/listeners, aborts transport, and discards late errors', async () => {
    const { store, load, unlisten } = fixture(); const pending = deferred<HealthReport>()
    load.mockReturnValueOnce(pending.promise); store.start(); await flush()
    store.stop(); const stopped = store.getSnapshot()
    expect(load.mock.calls[0][0].aborted).toBe(true)
    expect(unlisten).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)
    pending.reject(new Error('late')); await flush()
    expect(store.getSnapshot()).toBe(stopped)
  })

  it('supports StrictMode start/stop/start without stale completion winning', async () => {
    const { store, load } = fixture(); store.start(); store.stop(); store.start(); await flush()
    expect(load).toHaveBeenCalledTimes(1)
    expect(store.getSnapshot().freshness).toBe('fresh')
    store.stop(); expect(vi.getTimerCount()).toBe(0)
  })

  it('handles synchronous loader errors and unsubscribes observers', async () => {
    const { store, load } = fixture(); load.mockImplementationOnce(() => { throw new Error('bad transport') })
    const subscriber = vi.fn(); const unsubscribe = store.subscribe(subscriber)
    store.start(); await flush(); expect(store.getSnapshot().lastAttemptResult).toBe('error')
    expect(subscriber).toHaveBeenCalled(); unsubscribe(); subscriber.mockClear()
    store.refresh(); await flush(); expect(subscriber).not.toHaveBeenCalled()
    store.stop()
  })
})

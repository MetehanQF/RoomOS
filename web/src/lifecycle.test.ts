import { afterEach, describe, expect, it, vi } from 'vitest'
import { reconnectDelay, RoomOSLifecycle, type LifecycleEnvironment } from './lifecycle'

type FakeSocket = {
  readyState: number
  close: () => void
  onopen: ((event: Event) => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
  onclose: ((event: { code: number }) => void) | null
  onerror: (() => void) | null
}

function harness() {
  const sockets: FakeSocket[] = []
  const windowListeners: Record<string, () => void> = {}
  let visibility: (() => void) | null = null
  let online = true
  let visible = true
  const dispatched: string[] = []
  const env: LifecycleEnvironment = {
    createSocket: () => {
      const socket: FakeSocket = { readyState: 0, close: () => undefined, onopen: null, onmessage: null, onclose: null, onerror: null }
      sockets.push(socket)
      return socket
    },
    online: () => online,
    visible: () => visible,
    random: () => .5,
    setTimer: (work, delay) => setTimeout(work, delay),
    clearTimer: (timer) => clearTimeout(timer),
    addWindowListener: (name, listener) => { windowListeners[name] = listener },
    removeWindowListener: (name) => { delete windowListeners[name] },
    addVisibilityListener: (listener) => { visibility = listener },
    removeVisibilityListener: () => { visibility = null },
    dispatch: (name) => { dispatched.push(name) },
    verify: async () => 'ok',
  }
  return { lifecycle: new RoomOSLifecycle(env), sockets, windowListeners, dispatched, setOnline: (value: boolean) => { online = value }, setVisible: (value: boolean) => { visible = value }, visibility: () => visibility }
}

afterEach(() => vi.useRealTimers())

describe('RoomOS connection lifecycle', () => {
  it('uses exactly one socket and transitions connected → reconnecting → offline → connected', () => {
    vi.useFakeTimers()
    const h = harness()
    const states: string[] = []
    h.lifecycle.subscribeStatus((state) => states.push(state))
    h.lifecycle.subscribeEvents(() => undefined)
    expect(h.sockets).toHaveLength(1)
    h.sockets[0].readyState = 1
    h.sockets[0].onopen?.(new Event('open'))
    expect(h.lifecycle.snapshot).toBe('connected')
    h.sockets[0].readyState = 3
    h.sockets[0].onclose?.({ code: 1006 })
    expect(h.lifecycle.snapshot).toBe('reconnecting')
    vi.advanceTimersByTime(1_000)
    expect(h.sockets).toHaveLength(2)
    h.sockets[1].readyState = 3
    h.sockets[1].onclose?.({ code: 1006 })
    expect(h.lifecycle.snapshot).toBe('offline')
    vi.advanceTimersByTime(2_000)
    h.sockets[2].readyState = 1
    h.sockets[2].onopen?.(new Event('open'))
    expect(h.lifecycle.snapshot).toBe('connected')
    expect(states).toEqual(expect.arrayContaining(['reconnecting', 'connected', 'offline']))
    h.lifecycle.stop()
  })

  it('reconciles once on foreground without creating a duplicate socket', async () => {
    const h = harness()
    const refresh = vi.fn(async () => undefined)
    h.lifecycle.subscribeEvents(() => undefined, refresh)
    h.sockets[0].readyState = 1
    h.sockets[0].onopen?.(new Event('open'))
    await Promise.resolve()
    await new Promise((resolve) => setTimeout(resolve, 0))
    refresh.mockClear()
    h.visibility()?.()
    await Promise.resolve()
    expect(h.sockets).toHaveLength(1)
    expect(refresh).toHaveBeenCalledTimes(1)
    h.lifecycle.stop()
  })

  it('restores Connected after browser offline/online when the backend verifies', async () => {
    const h = harness()
    h.lifecycle.subscribeStatus(() => undefined)
    h.sockets[0].readyState = 1
    h.sockets[0].onopen?.(new Event('open'))
    h.setOnline(false)
    h.windowListeners.offline()
    expect(h.lifecycle.snapshot).toBe('offline')
    h.setOnline(true)
    h.windowListeners.online()
    await Promise.resolve()
    await Promise.resolve()
    expect(h.lifecycle.snapshot).toBe('connected')
    expect(h.sockets).toHaveLength(1)
    h.lifecycle.stop()
  })

  it('stops reconnecting after revoked auth and distinguishes browser offline', () => {
    vi.useFakeTimers()
    const h = harness()
    h.lifecycle.subscribeStatus(() => undefined)
    h.sockets[0].readyState = 3
    h.sockets[0].onclose?.({ code: 4401 })
    expect(h.dispatched).toEqual(['roomos:auth-required'])
    vi.advanceTimersByTime(60_000)
    expect(h.sockets).toHaveLength(1)
    const next = harness()
    next.lifecycle.subscribeStatus(() => undefined)
    next.setOnline(false)
    next.windowListeners.offline()
    expect(next.lifecycle.snapshot).toBe('offline')
    next.lifecycle.stop()
  })

  it('reports permission denial without misclassifying it as a revoked session', () => {
    const h = harness()
    h.lifecycle.subscribeStatus(() => undefined)
    h.sockets[0].readyState = 3
    h.sockets[0].onclose?.({ code: 4403 })
    expect(h.dispatched).toEqual(['roomos:permission-denied'])
    expect(h.lifecycle.snapshot).toBe('offline')
  })

  it('uses bounded jittered exponential retry delays', () => {
    expect(reconnectDelay(0, () => .5)).toBe(1_000)
    expect(reconnectDelay(3, () => .5)).toBe(8_000)
    expect(reconnectDelay(99, () => .5)).toBe(30_000)
    expect(reconnectDelay(0, () => 0)).toBe(800)
    expect(reconnectDelay(0, () => 1)).toBe(1_200)
  })
})


describe('Home lifecycle opt-out', () => {
  it('does not refresh event-driven listeners after ordinary frames or idle time', async () => {
    vi.useFakeTimers()
    const h = harness()
    const refresh = vi.fn(async () => undefined)
    const events = vi.fn()
    h.lifecycle.subscribeEvents(events, refresh, { refreshOnEvent: false })
    h.sockets[0].readyState = 1; h.sockets[0].onopen?.(new Event('open'))
    await vi.advanceTimersByTimeAsync(0)
    h.sockets[0].onmessage?.({ data: JSON.stringify({ type: 'light.state_changed', data: {} }) })
    await vi.advanceTimersByTimeAsync(180_000)
    expect(events).toHaveBeenCalledTimes(1); expect(refresh).toHaveBeenCalledTimes(1)
    h.lifecycle.stop()
  })
  it('deduplicates foreground and reconnect in the same transition, then refreshes once on later foreground', async () => {
    vi.useFakeTimers()
    const h = harness()
    const refresh = vi.fn(async () => undefined)
    h.lifecycle.subscribeEvents(() => {}, refresh, { refreshOnEvent: false })
    h.sockets[0].readyState = 1; h.sockets[0].onopen?.(new Event('open'))
    await vi.advanceTimersByTimeAsync(0)
    h.sockets[0].readyState = 3; h.sockets[0].onclose?.({ code: 1006 })
    await vi.advanceTimersByTimeAsync(1000)
    h.visibility()?.()
    h.sockets[1].readyState = 1; h.sockets[1].onopen?.(new Event('open'))
    await vi.advanceTimersByTimeAsync(0)
    expect(refresh).toHaveBeenCalledTimes(2)
    h.visibility()?.(); h.visibility()?.()
    await vi.advanceTimersByTimeAsync(0)
    expect(refresh).toHaveBeenCalledTimes(3)
    h.lifecycle.stop()
  })
})

it('reruns an in-flight old bootstrap after a disconnect instead of trusting it forever', async () => {
  vi.useFakeTimers()
  const h = harness()
  let finish!: () => void
  const refresh = vi.fn().mockImplementationOnce(() => new Promise<void>((resolve) => { finish = resolve })).mockResolvedValue(undefined)
  h.lifecycle.subscribeEvents(() => {}, refresh, { refreshOnEvent: false })
  h.sockets[0].readyState = 1; h.sockets[0].onopen?.(new Event('open'))
  h.sockets[0].readyState = 3; h.sockets[0].onclose?.({ code: 1006 })
  await vi.advanceTimersByTimeAsync(1000)
  h.sockets[1].readyState = 1; h.sockets[1].onopen?.(new Event('open'))
  expect(refresh).toHaveBeenCalledTimes(1)
  finish(); await vi.advanceTimersByTimeAsync(0)
  expect(refresh).toHaveBeenCalledTimes(2)
  h.lifecycle.stop()
})

it.each([4409, 1013])('reconnects with fresh scopes after server close %i instead of forcing re-pairing', (code) => {
  vi.useFakeTimers()
  const h = harness()
  h.lifecycle.subscribeStatus(() => undefined)
  h.sockets[0].readyState = 3
  h.sockets[0].onclose?.({ code })
  // 4409 asks the app to re-read its scopes; neither code ever requests pairing.
  expect(h.dispatched).toEqual(code === 4409 ? ['roomos:scopes-changed'] : [])
  vi.advanceTimersByTime(60_000)
  expect(h.sockets.length).toBeGreaterThan(1)
  h.lifecycle.stop()
})

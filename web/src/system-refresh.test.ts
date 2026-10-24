import { afterEach, expect, it, vi } from 'vitest'
import { RoomOSLifecycle, type LifecycleEnvironment } from './lifecycle'
import { ACTIVITY_PAGE_REFRESH, SYSTEM_PAGE_REFRESH } from './hooks'

/*
 * 7D.1-S2: the System page refreshed on every WebSocket frame. Its GET /system fed the
 * Device Registry, which published device.updated, which refreshed the page again
 * (observed every 100–300 ms in production). Legacy refreshes must be event-type scoped.
 */

type FakeSocket = { readyState: number; close: () => void; onopen: ((event: Event) => void) | null; onmessage: ((event: { data: unknown }) => void) | null; onclose: ((event: { code: number }) => void) | null; onerror: (() => void) | null }

function harness() {
  const sockets: FakeSocket[] = []
  const env: LifecycleEnvironment = {
    createSocket: () => { const s: FakeSocket = { readyState: 0, close: () => undefined, onopen: null, onmessage: null, onclose: null, onerror: null }; sockets.push(s); return s },
    online: () => true, visible: () => true, random: () => .5,
    setTimer: (work, delay) => setTimeout(work, delay), clearTimer: (timer) => clearTimeout(timer),
    addWindowListener: () => undefined, removeWindowListener: () => undefined,
    addVisibilityListener: () => undefined, removeVisibilityListener: () => undefined,
    dispatch: () => undefined, verify: async () => 'ok',
  }
  return { lifecycle: new RoomOSLifecycle(env), sockets }
}

const frame = (s: FakeSocket, type: string) => s.onmessage?.({ data: JSON.stringify({ type, timestamp: '2026-10-14T12:00:00Z', device_id: null, data: {} }) })

afterEach(() => vi.useRealTimers())

it('System page refresh cannot be driven by the device events its own GET produces', async () => {
  vi.useFakeTimers()
  const h = harness()
  // Simulated server: every GET /system (refresh) causes a device.updated frame 50 ms later.
  const refresh = vi.fn(async () => { setTimeout(() => frame(h.sockets[0], 'device.updated'), 50) })
  const off = h.lifecycle.subscribeEvents(() => undefined, refresh, { refreshOnEvent: SYSTEM_PAGE_REFRESH })
  const socket = h.sockets[0]; socket.readyState = 1; socket.onopen?.(new Event('open'))
  await vi.advanceTimersByTimeAsync(0)
  expect(refresh).toHaveBeenCalledTimes(1) // initial authoritative sync on connect
  await vi.advanceTimersByTimeAsync(60_000)
  expect(refresh).toHaveBeenCalledTimes(1) // was ~250/min before the fix

  frame(socket, 'system.snapshot_updated')
  await vi.advanceTimersByTimeAsync(1000)
  expect(refresh).toHaveBeenCalledTimes(2) // real system events still refresh, once
  for (const type of ['routine.diagnostics', 'light.state_changed', 'camera.updated', 'activity.recorded']) frame(socket, type)
  socket.onmessage?.({ data: JSON.stringify({ type: 'roomos.connection', timestamp: '2026-10-14T12:00:00Z', device_id: null, data: { core: 'connected', home_assistant: 'connected' } }) })
  await vi.advanceTimersByTimeAsync(1000)
  expect(refresh).toHaveBeenCalledTimes(2) // per-socket greeting is covered by the reconnect sync
  socket.onmessage?.({ data: JSON.stringify({ type: 'roomos.connection', timestamp: '2026-10-14T12:00:00Z', device_id: null, data: { home_assistant: 'disconnected' } }) })
  await vi.advanceTimersByTimeAsync(1000)
  expect(refresh).toHaveBeenCalledTimes(3) // HA outage must not leave last data looking current
  socket.onmessage?.({ data: JSON.stringify({ type: 'roomos.connection', timestamp: '2026-10-14T12:00:00Z', device_id: null, data: { home_assistant: 'connected' } }) })
  await vi.advanceTimersByTimeAsync(1000)
  expect(refresh).toHaveBeenCalledTimes(4)

  socket.readyState = 3; socket.onclose?.({ code: 1006 })
  await vi.advanceTimersByTimeAsync(5000)
  h.sockets[1].readyState = 1; h.sockets[1].onopen?.(new Event('open'))
  await vi.advanceTimersByTimeAsync(0)
  expect(refresh).toHaveBeenCalledTimes(5) // reconnect still re-syncs everything

  off()
  frame(h.sockets[1], 'system.snapshot_updated')
  await vi.advanceTimersByTimeAsync(1000)
  expect(refresh).toHaveBeenCalledTimes(5) // unmounted pages no longer refresh
  h.lifecycle.stop()
})

it('two System clients stay bounded and Activity only follows alert/activity events', async () => {
  vi.useFakeTimers()
  const h = harness()
  const a = vi.fn(async () => { setTimeout(() => frame(h.sockets[0], 'device.updated'), 30) })
  const b = vi.fn(async () => { setTimeout(() => frame(h.sockets[0], 'device.updated'), 70) })
  const activity = vi.fn(async () => undefined)
  h.lifecycle.subscribeEvents(() => undefined, a, { refreshOnEvent: SYSTEM_PAGE_REFRESH })
  h.lifecycle.subscribeEvents(() => undefined, b, { refreshOnEvent: SYSTEM_PAGE_REFRESH })
  h.lifecycle.subscribeEvents(() => undefined, activity, { refreshOnEvent: ACTIVITY_PAGE_REFRESH })
  const socket = h.sockets[0]; socket.readyState = 1; socket.onopen?.(new Event('open'))
  await vi.advanceTimersByTimeAsync(60_000)
  expect([a.mock.calls.length, b.mock.calls.length, activity.mock.calls.length]).toEqual([1, 1, 1])
  frame(socket, 'alert.started')
  await vi.advanceTimersByTimeAsync(1000)
  expect([a.mock.calls.length, b.mock.calls.length, activity.mock.calls.length]).toEqual([1, 1, 2])
  frame(socket, 'system.metric_updated')
  await vi.advanceTimersByTimeAsync(1000)
  expect([a.mock.calls.length, b.mock.calls.length, activity.mock.calls.length]).toEqual([2, 2, 2])
  h.lifecycle.stop()
})

it('boolean refreshOnEvent keeps its previous meaning', async () => {
  vi.useFakeTimers()
  const h = harness()
  const legacy = vi.fn(async () => undefined)
  const optOut = vi.fn(async () => undefined)
  h.lifecycle.subscribeEvents(() => undefined, legacy, { refreshOnEvent: true })
  h.lifecycle.subscribeEvents(() => undefined, optOut, { refreshOnEvent: false })
  const s = h.sockets[0]; s.readyState = 1; s.onopen?.(new Event('open'))
  await vi.advanceTimersByTimeAsync(0)
  frame(s, 'light.state_changed')
  await vi.advanceTimersByTimeAsync(1000)
  expect([legacy.mock.calls.length, optOut.mock.calls.length]).toEqual([2, 1])
  h.lifecycle.stop()
})

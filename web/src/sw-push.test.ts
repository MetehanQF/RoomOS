import { beforeEach, describe, expect, it, vi } from 'vitest'
import workerText from '../public/sw.js?raw'

type Listener = (event: { waitUntil: (p: Promise<unknown>) => void; [key: string]: unknown }) => void

function loadWorker(windows: Array<Record<string, unknown>>) {
  const listeners: Record<string, Listener> = {}
  const shown: Array<[string, Record<string, unknown>]> = []
  const fetches: Array<[string, RequestInit]> = []
  const self = {
    addEventListener: (type: string, listener: Listener) => { listeners[type] = listener },
    clients: { claim: vi.fn(), matchAll: vi.fn(async () => windows), openWindow: vi.fn(async () => undefined) },
    registration: { showNotification: vi.fn(async (title: string, options: Record<string, unknown>) => { shown.push([title, options]) }),
      pushManager: { subscribe: vi.fn(async () => ({ toJSON: () => ({ endpoint: 'https://fcm.googleapis.com/new', keys: { p256dh: 'p', auth: 'a' } }) })) } },
    location: { origin: 'https://roomos.example.com' },
    skipWaiting: vi.fn(),
  }
  const fetchFn = vi.fn(async (url: string, init: RequestInit) => { fetches.push([url, init]); return new Response(null, { status: 204 }) })
  new Function('self', 'fetch', workerText)(self, fetchFn)
  const dispatch = async (type: string, extra: Record<string, unknown>) => {
    let pending: Promise<unknown> = Promise.resolve()
    listeners[type]({ ...extra, waitUntil: (p) => { pending = p } })
    await pending
  }
  return { dispatch, shown, fetches, self }
}

const payload = { id: 'psh_' + 'a'.repeat(32), severity: 'critical', transition: 'started', title: 'RoomOS — Kritik', body: 'Home Assistant bağlantısı kesildi.', path: '/notifications?alert=service.home_assistant.offline', tag: 'service.home_assistant.offline' }

describe('service worker push', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('shows a critical notification that stays until handled and records a displayed receipt', async () => {
    const worker = loadWorker([])
    await worker.dispatch('push', { data: { json: () => payload } })
    expect(worker.shown).toHaveLength(1)
    const [title, options] = worker.shown[0]
    expect(title).toBe('RoomOS — Kritik')
    expect(options).toMatchObject({ body: 'Home Assistant bağlantısı kesildi.', requireInteraction: true, renotify: true, tag: payload.tag })
    expect(worker.fetches).toEqual([['/api/v1/push/receipts', expect.objectContaining({ method: 'POST', credentials: 'same-origin', body: JSON.stringify({ message_id: payload.id, event: 'displayed' }) })]])
  })

  it('does not raise a system notification while RoomOS is focused and visible', async () => {
    const focused = { visibilityState: 'visible', focused: true, url: 'https://roomos.example.com/panel', postMessage: vi.fn() }
    const worker = loadWorker([focused])
    await worker.dispatch('push', { data: { json: () => payload } })
    expect(worker.shown).toHaveLength(0)
    expect(focused.postMessage).toHaveBeenCalledWith(expect.objectContaining({ type: 'roomos:push', id: payload.id }))
  })

  it('never follows an untrusted path and survives a malformed payload', async () => {
    const worker = loadWorker([])
    await worker.dispatch('push', { data: { json: () => ({ ...payload, path: 'https://evil.example/' }) } })
    expect((worker.shown[0][1].data as { path: string }).path).toBe('/notifications')
    await worker.dispatch('push', { data: { json: () => { throw new Error('bad') } } })
    expect(worker.shown[1][0]).toBe('RoomOS')
    expect(worker.fetches).toHaveLength(1) // no receipt for an id-less payload
  })

  it('routes a tap into the open app without reloading it, else opens the notification path', async () => {
    const open = { url: 'https://roomos.example.com/panel', focus: vi.fn(async () => undefined), postMessage: vi.fn() }
    const worker = loadWorker([open])
    const notification = { close: vi.fn(), data: { path: payload.path, id: payload.id } }
    await worker.dispatch('notificationclick', { notification })
    expect(open.postMessage).toHaveBeenCalledWith({ type: 'roomos:navigate', path: payload.path })
    expect(worker.fetches.at(-1)![1].body).toContain('"opened"')
    const empty = loadWorker([])
    await empty.dispatch('notificationclick', { notification })
    expect(empty.self.clients.openWindow).toHaveBeenCalledWith(payload.path)
  })

  it('rotates a changed subscription with proof of the old endpoint', async () => {
    const worker = loadWorker([])
    await worker.dispatch('pushsubscriptionchange', { oldSubscription: { endpoint: 'https://fcm.googleapis.com/old', options: { applicationServerKey: new Uint8Array(65) } } })
    const [url, init] = worker.fetches[0]
    expect(url).toBe('/api/v1/push/webpush/rotate')
    expect(JSON.parse(String(init.body))).toEqual({ old_endpoint: 'https://fcm.googleapis.com/old', subscription: { endpoint: 'https://fcm.googleapis.com/new', keys: { p256dh: 'p', auth: 'a' } } })
  })

  it('still caches nothing', () => {
    for (const forbidden of ['caches.open', 'cache.put', 'respondWith', 'localStorage', 'indexedDB']) expect(workerText).not.toContain(forbidden)
  })
})

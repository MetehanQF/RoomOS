/* RoomOS intentionally caches no API, auth, media, alert, activity, control, or application state. */
self.addEventListener('install', () => undefined)
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))
self.addEventListener('message', (event) => {
  if (event.data?.type === 'SKIP_WAITING') void self.skipWaiting()
})
self.addEventListener('fetch', () => {
  // Network-only by design: live controller state must never be mistaken for fresh state.
})

/* Phase 7G Web Push. The payload is the server's minimal, secret-free notification; nothing is stored. */
const ROOMOS_PATH = /^\/notifications(\?alert=[a-z0-9._-]{1,80})?$/
const ROOMOS_ID = /^psh_[a-f0-9]{32}$/
const FALLBACK_BODY = "Ayrıntılar için RoomOS'u açın."

function roomosText(value, fallback, limit) {
  return typeof value === 'string' && value.trim() ? value.slice(0, limit) : fallback
}

function roomosReceipt(id, event) {
  if (!ROOMOS_ID.test(String(id))) return Promise.resolve()
  // Same-origin POST: the browser attaches the session cookie and Origin; the Core binds the
  // receipt to this device's own route. Failures are ignored (best-effort evidence only).
  return fetch('/api/v1/push/receipts', {
    method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message_id: id, event }),
  }).catch(() => undefined)
}

self.addEventListener('push', (event) => {
  event.waitUntil((async () => {
    let data = {}
    try { data = event.data ? event.data.json() : {} } catch { data = {} }
    const path = ROOMOS_PATH.test(String(data.path)) ? data.path : '/notifications'
    const critical = data.severity === 'critical' && data.transition !== 'resolved'
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    const focused = windows.find((client) => client.visibilityState === 'visible' && client.focused)
    if (focused) {
      // RoomOS is in front of the user: the live WebSocket already shows it, so no system notification.
      focused.postMessage({ type: 'roomos:push', id: data.id, severity: data.severity, path })
      await roomosReceipt(data.id, 'displayed')
      return
    }
    await self.registration.showNotification(roomosText(data.title, 'RoomOS', 100), {
      body: roomosText(data.body, FALLBACK_BODY, 240),
      tag: roomosText(data.tag, 'roomos', 80),
      renotify: critical,
      requireInteraction: critical,
      icon: '/icons/electronic-m-v1-192.png',
      badge: '/icons/electronic-m-v1-favicon-64.png',
      data: { path, id: data.id },
    })
    await roomosReceipt(data.id, 'displayed')
  })())
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const path = ROOMOS_PATH.test(String(event.notification.data?.path)) ? event.notification.data.path : '/notifications'
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    const existing = windows.find((client) => new URL(client.url).origin === self.location.origin)
    if (existing) {
      // In-app routing: no reload, so no second bootstrap or WebSocket on the panel.
      existing.postMessage({ type: 'roomos:navigate', path })
      await existing.focus()
    } else {
      await self.clients.openWindow(path)
    }
    await roomosReceipt(event.notification.data?.id, 'opened')
  })())
})

self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil((async () => {
    const previous = event.oldSubscription
    let next = event.newSubscription
    if (!next && previous?.options?.applicationServerKey) {
      next = await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: previous.options.applicationServerKey })
    }
    if (!previous || !next) return
    // Proof of the old endpoint replaces CSRF here; the Core accepts it only for this device.
    await fetch('/api/v1/push/webpush/rotate', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_endpoint: previous.endpoint, subscription: next.toJSON() }),
    }).catch(() => undefined)
  })())
})

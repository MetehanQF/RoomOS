/* RoomOS intentionally caches no API responses or application state. */
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))
self.addEventListener('fetch', () => {
  // Network-only by design: live controller state must never be mistaken for fresh state.
})

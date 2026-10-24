import { useEffect, useState, useSyncExternalStore } from 'react'
import { api } from './api'
import { roomosLifecycle } from './lifecycle'
import { bindDevicesLifecycle, DevicesStore } from './devices-store'

/** Re-renders relative times only (no network). Paused while the page is hidden. */
export function useNow(intervalMs: number) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    let timer: number | null = null
    const start = () => { if (timer === null) timer = window.setInterval(() => setNow(Date.now()), intervalMs) }
    const stop = () => { if (timer !== null) { window.clearInterval(timer); timer = null } }
    const visibility = () => { if (document.visibilityState === 'visible') { setNow(Date.now()); start() } else stop() }
    visibility()
    document.addEventListener('visibilitychange', visibility)
    return () => { stop(); document.removeEventListener('visibilitychange', visibility) }
  }, [intervalMs])
  return now
}

export function useDevicesSnapshot() {
  const [store] = useState(() => new DevicesStore({ devices: api.devices, clients: api.clients }))
  useEffect(() => (roomosLifecycle ? bindDevicesLifecycle(store, roomosLifecycle) : undefined), [store])
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot)
  return { snapshot, retry: () => void store.load() }
}

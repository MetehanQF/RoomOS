import { useCallback, useEffect, useRef, useState } from 'react'
import { roomosLifecycle, type RefreshOnEvent } from './lifecycle'
import type { RealtimeStatus, RoomOSEvent } from './types'

export function roomosEventUrl(location: Pick<Location, 'protocol' | 'host'> = window.location) {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${location.host}/api/v1/events`
}

export function useApi<T>(loader: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const inFlight = useRef<Promise<T> | null>(null)

  // The mount load and the socket-open authoritative refresh can overlap.
  // Share their request so a page performs one initial fetch per endpoint.
  const load = useCallback(() => {
    if (inFlight.current) return inFlight.current
    const request = Promise.resolve().then(loader).finally(() => {
      if (inFlight.current === request) inFlight.current = null
    })
    inFlight.current = request
    return request
  }, [loader])

  const refresh = useCallback(async () => {
    try {
      setError(null)
      setData(await load())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [load])

  useEffect(() => {
    let active = true
    load()
      .then((result) => {
        if (active) setData(result)
      })
      .catch((cause: unknown) => {
        if (active) setError(cause instanceof Error ? cause.message : 'Unknown error')
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [load])

  return { data, error, loading, refresh, setData }
}

/**
 * System data changes with system.* events and with Home Assistant connectivity transitions
 * (broadcast roomos.connection without the per-socket ``core`` greeting). Device, routine,
 * light and camera events never refetch it: GET /system feeds the device registry (7D.1-S2).
 */
export const SYSTEM_PAGE_REFRESH: RefreshOnEvent = (event) =>
  event.type.startsWith('system.') || (event.type === 'roomos.connection' && !('core' in (event.data ?? {})))
/** Activity lists alerts and recorded activity only. */
export const ACTIVITY_PAGE_REFRESH: RefreshOnEvent = (event) => event.type.startsWith('alert.') || event.type === 'activity.recorded'
export const NOTIFICATION_PAGE_REFRESH: RefreshOnEvent = (event) => event.type.startsWith('alert.')

export function useRoomOSEvents(
  onEvent: (event: RoomOSEvent) => void,
  refresh?: () => Promise<void>,
  options: { refreshOnEvent?: RefreshOnEvent } = {},
) {
  const refreshOnEvent = options.refreshOnEvent ?? true
  const [status, setStatus] = useState<RealtimeStatus>(() => roomosLifecycle?.snapshot ?? 'reconnecting')
  const eventRef = useRef(onEvent)
  const refreshRef = useRef(refresh)

  useEffect(() => { eventRef.current = onEvent }, [onEvent])
  useEffect(() => { refreshRef.current = refresh }, [refresh])

  useEffect(() => roomosLifecycle?.subscribeStatus(setStatus), [])
  useEffect(() => roomosLifecycle?.subscribeEvents(
    (event) => eventRef.current(event),
    refreshRef.current ? () => refreshRef.current?.() ?? Promise.resolve() : undefined,
    { refreshOnEvent },
  ), [refreshOnEvent])

  return status
}

export function useConnectionStatus() {
  const [status, setStatus] = useState<RealtimeStatus>(() => roomosLifecycle?.snapshot ?? 'reconnecting')
  useEffect(() => roomosLifecycle?.subscribeStatus(setStatus), [])
  return status
}

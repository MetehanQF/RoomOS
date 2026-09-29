import { useCallback, useEffect, useRef, useState } from 'react'
import type { RealtimeStatus, RoomOSEvent } from './types'

export function roomosEventUrl(location: Pick<Location, 'protocol' | 'host'> = window.location) {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${location.host}/api/v1/events`
}

export function useApi<T>(loader: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      setError(null)
      setData(await loader())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [loader])

  useEffect(() => {
    let active = true
    loader()
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
  }, [loader])

  return { data, error, loading, refresh, setData }
}

export function useRoomOSEvents(
  onEvent: (event: RoomOSEvent) => void,
  refresh?: () => Promise<void>,
) {
  const [status, setStatus] = useState<RealtimeStatus>('connecting')
  const eventRef = useRef(onEvent)
  const refreshRef = useRef(refresh)

  useEffect(() => { eventRef.current = onEvent }, [onEvent])
  useEffect(() => { refreshRef.current = refresh }, [refresh])

  useEffect(() => {
    let socket: WebSocket | null = null
    let retryTimer: number | null = null
    let refreshTimer: number | null = null
    let stopped = false
    let backoff = 1000

    const connect = () => {
      setStatus('connecting')
      socket = new WebSocket(roomosEventUrl())
      socket.onopen = () => { backoff = 1000; setStatus('connected') }
      socket.onmessage = (message) => {
        const event = JSON.parse(message.data) as RoomOSEvent
        if (!event.type) return
        eventRef.current(event)
        if (!refreshRef.current) return
        if (refreshTimer !== null) window.clearTimeout(refreshTimer)
        refreshTimer = window.setTimeout(() => { void refreshRef.current?.() }, 180)
      }
      socket.onclose = (event) => {
        if (stopped) return
        if (event.code === 4401 || event.code === 4403) {
          window.dispatchEvent(new Event('roomos:auth-required'))
          setStatus('disconnected')
          return
        }
        setStatus('disconnected')
        retryTimer = window.setTimeout(connect, backoff)
        backoff = Math.min(backoff * 2, 30000)
      }
    }

    connect()
    return () => {
      stopped = true
      socket?.close()
      if (retryTimer !== null) window.clearTimeout(retryTimer)
      if (refreshTimer !== null) window.clearTimeout(refreshTimer)
    }
  }, [])

  return status
}

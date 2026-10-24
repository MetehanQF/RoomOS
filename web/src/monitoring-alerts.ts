import { useCallback } from 'react'
import { api } from './api'
import { useAuthStatus } from './auth-context'
import { NOTIFICATION_PAGE_REFRESH, useApi, useRoomOSEvents } from './hooks'

export function useMonitoringAlerts() {
  const allowed=useAuthStatus()?.device?.scopes.includes('activity:read')??false
  const load=useCallback(()=>allowed?api.notifications():Promise.resolve(null),[allowed])
  const result=useApi(load)
  useRoomOSEvents(useCallback(()=>undefined,[]),result.refresh,{refreshOnEvent:NOTIFICATION_PAGE_REFRESH})
  return {...result,allowed}
}

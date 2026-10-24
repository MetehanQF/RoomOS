import { ApiError, api } from './api'
import type { CameraAccess, CameraStatus } from './types'

export async function loadCameraAccess(scopes: readonly string[]): Promise<CameraAccess> {
  if (!scopes.includes('camera:read')) return { authorized: false, camera: null }
  try {
    const catalog = await api.cameras()
    return { authorized: true, camera: catalog.cameras[0] ?? null }
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 403) return { authorized: false, camera: null }
    throw cause
  }
}

export function personActivityLabel(camera: CameraStatus, now = new Date()) {
  if (!camera.last_person_event) return 'No recent person activity'
  const elapsed = Math.max(0, now.getTime() - new Date(camera.last_person_event).getTime())
  const minutes = Math.floor(elapsed / 60_000)
  const relative = minutes < 1 ? 'just now' : minutes < 60 ? `${minutes} min ago` : minutes < 1440 ? `${Math.floor(minutes / 60)} hr ago` : `${Math.floor(minutes / 1440)} days ago`
  return camera.last_person_event_freshness === 'stale' ? `Person detected ${relative} · Older activity` : `Person detected ${relative}`
}

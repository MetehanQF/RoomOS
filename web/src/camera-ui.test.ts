import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from './api'
import { loadCameraAccess } from './camera-ui'

describe('camera authorization gate', () => {
  afterEach(() => vi.restoreAllMocks())

  it('does not request sensitive camera metadata without camera:read', async () => {
    const request = vi.spyOn(api, 'cameras')
    await expect(loadCameraAccess(['lights:read', 'system:read'])).resolves.toEqual({ authorized: false, camera: null })
    expect(request).not.toHaveBeenCalled()
  })

  it('loads only the server catalog for an explicitly authorized device', async () => {
    const camera = { id: 'room_camera' as const, display_name: 'Room Camera', status: 'online' as const, detection_status: 'active' as const, frigate_health: 'healthy' as const, last_person_event: null, last_person_event_freshness: 'none' as const, snapshot_available: false, live_view_available: false, last_checked: null }
    vi.spyOn(api, 'cameras').mockResolvedValue({ cameras: [camera] })
    await expect(loadCameraAccess(['camera:read'])).resolves.toEqual({ authorized: true, camera })
  })

  it('fails closed if an apparently authorized session is denied by Core', async () => {
    vi.spyOn(api, 'cameras').mockRejectedValue(new ApiError(403, 'denied'))
    await expect(loadCameraAccess(['camera:read'])).resolves.toEqual({ authorized: false, camera: null })
  })
})

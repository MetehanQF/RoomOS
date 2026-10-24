import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, csrfToken, parseReleaseInfo, parseSystemData } from './api'
import { resetConnectionStatus, setConnectionStatus } from './connection-state'
import { DEFAULT_PANEL_LAYOUT } from './panel-layout'

const node = (id: 'primary' | 'secondary') => ({
  id,
  name: id === 'primary' ? 'Primary' : 'Secondary',
  online: true,
  metrics: {},
})

describe('system response contract', () => {
  it('accepts the current Core primary/secondary response', () => {
    const payload = { primary: node('primary'), secondary: node('secondary'), services: [], dns_cluster: { status: 'unknown', primary: {}, secondary: {} } }
    expect(parseSystemData(payload)).toBe(payload)
  })

  it('rejects the legacy nodes response before React render', () => {
    expect(() => parseSystemData({ nodes: { primary: node('primary'), secondary: node('secondary') }, services: [], dns_cluster: { status: 'unknown', primary: {}, secondary: {} } }))
      .toThrowError(new ApiError(502, 'RoomOS Core returned an incompatible system response.'))
  })
})

describe('release response contract', () => {
  it('accepts only the bounded Core release identity', () => {
    const release = { release: 'phase-6a.1', revision: 'a'.repeat(40), component: 'core' as const }
    expect(parseReleaseInfo(release)).toBe(release)
  })

  it('rejects extra internal deployment details', () => {
    expect(() => parseReleaseInfo({
      release: 'phase-6a.1',
      revision: 'a'.repeat(40),
      component: 'core',
      path: '/private/source',
    })).toThrowError(new ApiError(502, 'RoomOS Core returned an incompatible release response.'))
  })
})

describe('RoomOS session request helpers', () => {
  afterEach(() => { resetConnectionStatus(); vi.unstubAllGlobals() })
  it('reads only the CSRF cookie value used for unsafe requests', () => {
    expect(csrfToken('other=x; roomos_csrf=safe-token; theme=dark')).toBe('safe-token')
    expect(csrfToken('other=x')).toBeNull()
  })

  it('fails closed before fetch when a write is attempted without a live connection', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    setConnectionStatus('offline')
    await expect(api.power('desk_ambient', false)).rejects.toThrow(/Controls are unavailable/)
    expect(fetch).not.toHaveBeenCalled()
  })

  it('PWA telemetry POST carries CSRF and surfaces Retry-After on 429 (7D.3.1)', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{"detail":"Too many reports"}', { status: 429, headers: { 'Retry-After': '12' } }))
    vi.stubGlobal('fetch', fetch)
    setConnectionStatus('connected')
    await expect(api.sendTelemetry('{}')).rejects.toMatchObject({ status: 429, retryAfter: 12 })
    expect(fetch).toHaveBeenCalledWith('/api/v1/devices/self/telemetry', expect.objectContaining({ method: 'POST' }))
    fetch.mockResolvedValue(new Response('{}', { status: 500 }))
    await expect(api.sendTelemetry('{}')).rejects.toMatchObject({ status: 500, retryAfter: null })
  })

  it('Health passes cancellation and no-store without changing its GET contract', async () => {
    const payload = { overall: 'healthy', checked_at: '2026-10-13T12:00:00Z', components: [] }
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => payload })
    vi.stubGlobal('fetch', fetch)
    const controller = new AbortController()
    expect(await api.systemHealth(controller.signal)).toEqual(payload)
    expect(fetch).toHaveBeenCalledWith('/api/v1/system/health', expect.objectContaining({ signal: controller.signal, cache: 'no-store', credentials: 'same-origin' }))
    expect(fetch.mock.calls[0][1].method).toBeUndefined()
    expect(await api.systemHealth()).toEqual(payload) // existing callers remain compatible
  })

  it('uses one self-only endpoint for panel layout load, save, and reset', async () => {
    const response = { layout: DEFAULT_PANEL_LAYOUT, source: 'default', updated_at: null }
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      void url; void init
      return new Response(JSON.stringify(response), { status: 200 })
    })
    vi.stubGlobal('fetch', fetch)
    setConnectionStatus('connected')
    await expect(api.panelLayout()).resolves.toEqual(response)
    await api.savePanelLayout(DEFAULT_PANEL_LAYOUT)
    await api.resetPanelLayout()
    expect(fetch.mock.calls.map(([url]) => url)).toEqual(Array(3).fill('/api/v1/panel/layout'))
    expect(fetch.mock.calls[0][1]?.method).toBeUndefined()
    expect(fetch.mock.calls[1][1]).toEqual(expect.objectContaining({ method: 'PUT', body: JSON.stringify(DEFAULT_PANEL_LAYOUT) }))
    expect(fetch.mock.calls[2][1]).toEqual(expect.objectContaining({ method: 'DELETE' }))
  })

  it('uses the closed Smart Automations endpoints and carries CSRF on every unsafe request', async () => {
    const fetch = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetch)
    setConnectionStatus('connected')
    await api.automations()
    await api.automationHistory(25, '50')
    await api.simulateAutomation(`au_${'a'.repeat(32)}`)
    await api.enableAutomation(`au_${'a'.repeat(32)}`, true)
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/automations', '/api/v1/automations/history?limit=25&cursor=50',
      `/api/v1/automations/au_${'a'.repeat(32)}/simulate`, `/api/v1/automations/au_${'a'.repeat(32)}/enabled`,
    ])
    expect(fetch.mock.calls[0][1]?.method).toBeUndefined()
    expect(fetch.mock.calls[1][1]?.method).toBeUndefined()
    expect(fetch.mock.calls[2][1]).toEqual(expect.objectContaining({ method: 'POST' }))
    expect(fetch.mock.calls[3][1]).toEqual(expect.objectContaining({ method: 'POST', body: '{"enabled":true}' }))
  })
})

describe('device API client (7D.1)', () => {
  it('uses GET only, validates ids before requesting, and parses responses', async () => {
    const { api } = await import('./api')
    const calls: Array<[string, RequestInit | undefined]> = []
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      calls.push([url, init])
      return new Response(JSON.stringify({ devices: [], generated_at: '2026-10-14T12:00:00Z' }), { status: 200 })
    })
    vi.stubGlobal('fetch', fetchMock)
    await expect(api.devices()).resolves.toEqual({ devices: [], generated_at: '2026-10-14T12:00:00Z' })
    await expect(api.device('../primary')).rejects.toMatchObject({ status: 404 })
    await expect(api.deviceTelemetry('PRIMARY')).rejects.toMatchObject({ status: 404 })
    expect(calls).toHaveLength(1)
    expect(calls[0][0]).toBe('/api/v1/devices')
    expect(calls[0][1]?.method ?? 'GET').toBe('GET')
    vi.unstubAllGlobals()
  })
})

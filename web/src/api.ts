import type { AuthStatus, GroupCommandResult, Health, Light, SystemData } from './types'

const BASE = '/api/v1'

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
    this.name = 'ApiError'
  }
}

export function friendlyApiMessage(status: number) {
  if (status === 401) return 'This device needs to be paired with RoomOS.'
  if (status === 403) return 'Controls are currently locked.'
  if (status === 404) return 'That RoomOS device is not available.'
  if (status === 503) return 'RoomOS cannot reach Home Assistant right now.'
  if (status === 422) return 'That setting is not supported by this light.'
  if (status >= 500) return 'RoomOS Core is temporarily unavailable.'
  return 'The command could not be completed.'
}

export function csrfToken(cookie = typeof document === 'undefined' ? '' : document.cookie) {
  const value = cookie.split(';').map((item) => item.trim()).find((item) => item.startsWith('roomos_csrf='))
  return value ? decodeURIComponent(value.slice('roomos_csrf='.length)) : null
}

async function request<T>(path: string, init?: RequestInit, csrf = true): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase()
  const token = csrf && !['GET', 'HEAD', 'OPTIONS'].includes(method) ? csrfToken() : null
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(token ? { 'X-CSRF-Token': token } : {}), ...init?.headers },
  })
  if (!response.ok) {
    if (response.status === 401 && typeof window !== 'undefined') {
      window.dispatchEvent(new Event('roomos:auth-required'))
    }
    throw new ApiError(response.status, friendlyApiMessage(response.status))
  }
  return response.json() as Promise<T>
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function parseSystemData(payload: unknown): SystemData {
  // Node IDs come from Core's device registry, so the shape is validated
  // structurally rather than against a fixed set of keys.
  const incompatible = () =>
    new ApiError(502, 'RoomOS Core returned an incompatible system response.')
  if (!isRecord(payload) || !isRecord(payload.nodes) || !Array.isArray(payload.services)) {
    throw incompatible()
  }
  for (const node of Object.values(payload.nodes)) {
    if (!isRecord(node) || !isRecord(node.metrics)) throw incompatible()
  }
  return payload as unknown as SystemData
}

export const api = {
  authStatus: () => request<AuthStatus>('/auth/status'),
  pair: (code: string, friendly_name: string) =>
    request<AuthStatus>('/auth/pair', {
      method: 'POST',
      body: JSON.stringify({ code, friendly_name }),
    }, false),
  health: () => request<Health>('/health'),
  lights: () => request<Light[]>('/lights'),
  system: async () => parseSystemData(await request<unknown>('/system')),
  power: (id: string, on: boolean) =>
    request(`/lights/${id}/power`, { method: 'POST', body: JSON.stringify({ on }) }),
  allPower: (on: boolean) =>
    request<GroupCommandResult>('/lights/all/power', {
      method: 'POST',
      body: JSON.stringify({ on }),
    }),
  brightness: (id: string, brightness: number) =>
    request(`/lights/${id}/brightness`, {
      method: 'POST',
      body: JSON.stringify({ brightness }),
    }),
  color: (id: string, rgb: [number, number, number]) =>
    request(`/lights/${id}/color`, { method: 'POST', body: JSON.stringify({ rgb }) }),
  temperature: (id: string, kelvin: number) =>
    request(`/lights/${id}/temperature`, {
      method: 'POST',
      body: JSON.stringify({ kelvin }),
    }),
  effect: (id: string, effect: string) =>
    request(`/lights/${id}/effect`, {
      method: 'POST',
      body: JSON.stringify({ effect }),
    }),
}

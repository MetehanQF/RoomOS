import { describe, expect, it, vi } from 'vitest'
import { applicationServerKey, deliveryLabel, disableBrowserPush, enableBrowserPush, listenForWorkerNavigation, pushSupport, reconcileBrowserPush, subscriptionJson } from './push-client'
import type { PushSettings } from './types'

const SETTINGS: PushSettings = {
  device_id: 'dev', display_name: 'Tab', session_kind: 'web', can_manage: true, can_receive: true,
  preferences: { enabled: true, critical: true, warning: true, info: false, recovery: 'critical', lock_screen_details: false,
    quiet_hours: { enabled: false, start: '23:00', end: '08:00', allow_critical: true } },
  channels: [], recent: [],
  capabilities: { mode: 'smart', delivery_active: true, webpush_available: true, webpush_public_key: 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U', fcm_available: false, timezone: 'Europe/Istanbul' },
}
const sub = { endpoint: 'https://fcm.googleapis.com/fcm/send/x', expirationTime: null, keys: { p256dh: 'p', auth: 'a' } }

function env({ permission = 'default' as NotificationPermission, existing = null as unknown, secure = true, push = true } = {}) {
  const subscription = { toJSON: () => sub, unsubscribe: vi.fn(async () => true) }
  const pushManager = { getSubscription: vi.fn(async () => existing), subscribe: vi.fn(async () => subscription) }
  const requestPermission = vi.fn(async () => 'granted' as NotificationPermission)
  return {
    env: { isSecureContext: secure, serviceWorker: { ready: Promise.resolve({ pushManager } as unknown as ServiceWorkerRegistration) },
      hasPushManager: push, Notification: { permission, requestPermission }, standalone: true },
    pushManager, requestPermission, subscription,
  }
}

describe('browser push helpers', () => {
  it('reports unsupported browsers honestly', () => {
    expect(pushSupport(env({ secure: false }).env)).toEqual({ supported: false, reason: 'insecure_context' })
    expect(pushSupport(env({ push: false }).env)).toEqual({ supported: false, reason: 'no_push_manager' })
    expect(pushSupport({ ...env().env, serviceWorker: null })).toEqual({ supported: false, reason: 'no_service_worker' })
    expect(pushSupport(env({ permission: 'granted' }).env)).toEqual({ supported: true, permission: 'granted', standalone: true })
  })

  it('decodes the VAPID key and rejects incomplete subscriptions', () => {
    expect(applicationServerKey(SETTINGS.capabilities.webpush_public_key!).length).toBe(65)
    expect(() => subscriptionJson({ toJSON: () => ({ endpoint: 'x', keys: {} }) } as unknown as PushSubscription)).toThrow()
  })

  it('asks permission only inside enable, subscribes userVisibleOnly and saves once', async () => {
    const harness = env()
    const save = vi.fn(async () => SETTINGS)
    await enableBrowserPush(harness.env, SETTINGS.capabilities.webpush_public_key!, save)
    expect(harness.requestPermission).toHaveBeenCalledTimes(1)
    expect(harness.pushManager.subscribe).toHaveBeenCalledWith(expect.objectContaining({ userVisibleOnly: true }))
    expect(save).toHaveBeenCalledWith(sub)
  })

  it('stops when permission is denied and never subscribes', async () => {
    const harness = env({ permission: 'denied' })
    harness.requestPermission.mockResolvedValue('denied')
    await expect(enableBrowserPush(harness.env, 'AAAA', vi.fn())).rejects.toThrow('blocked')
    expect(harness.pushManager.subscribe).not.toHaveBeenCalled()
  })

  it('disables server-side first, then drops the browser subscription', async () => {
    const harness = env({ existing: null })
    const existing = { unsubscribe: vi.fn(async () => true) }
    harness.pushManager.getSubscription.mockResolvedValue(existing as never)
    const remove = vi.fn(async () => SETTINGS)
    await disableBrowserPush(harness.env, remove)
    expect(remove).toHaveBeenCalled()
    expect(existing.unsubscribe).toHaveBeenCalled()
  })

  it('repairs only a subscription the server lost, never an invalidated one', async () => {
    const harness = env({ permission: 'granted', existing: { toJSON: () => sub } })
    const save = vi.fn(async () => SETTINGS)
    expect(await reconcileBrowserPush(harness.env, SETTINGS, save)).toBe(SETTINGS)
    save.mockClear()
    const invalid = { ...SETTINGS, channels: [{ kind: 'webpush' as const, status: 'invalid' as const, since: 'x' }] }
    expect(await reconcileBrowserPush(harness.env, invalid, save)).toBeNull()
    expect(await reconcileBrowserPush(harness.env, { ...SETTINGS, can_manage: false }, save)).toBeNull()
    expect(save).not.toHaveBeenCalled()
  })

  it('labels delivery truthfully (accepted is not shown-on-device)', () => {
    expect(deliveryLabel('accepted')).toBe('Push service accepted')
    expect(deliveryLabel('displayed')).toBe('Shown on device')
  })

  it('routes worker taps in-app only for Notification Center paths', () => {
    let handler: ((event: MessageEvent) => void) | null = null
    const container = { addEventListener: vi.fn((_t, h) => { handler = h }), removeEventListener: vi.fn() }
    const navigate = vi.fn()
    const stop = listenForWorkerNavigation(container as never, navigate)
    handler!({ data: { type: 'roomos:navigate', path: '/notifications?alert=node.primary.offline' } } as MessageEvent)
    handler!({ data: { type: 'roomos:navigate', path: 'https://evil.example' } } as MessageEvent)
    handler!({ data: { type: 'roomos:navigate', path: '/lights' } } as MessageEvent)
    expect(navigate).toHaveBeenCalledTimes(1)
    stop()
    expect(container.removeEventListener).toHaveBeenCalled()
  })
})

/* Phase 7G browser push helpers. Everything is injectable so the logic is unit-testable and
   unsupported browsers are reported as unsupported — never pretended to work. */
import type { PushSettings, WebPushSubscriptionJSON } from './types'

export type PushSupport =
  | { supported: false; reason: 'no_service_worker' | 'no_push_manager' | 'no_notifications' | 'insecure_context' }
  | { supported: true; permission: NotificationPermission; standalone: boolean }

type Env = {
  isSecureContext: boolean
  serviceWorker?: Pick<ServiceWorkerContainer, 'ready'> | null
  hasPushManager: boolean
  Notification?: { permission: NotificationPermission; requestPermission: () => Promise<NotificationPermission> } | null
  standalone: boolean
}

export function browserEnv(win: Window = window): Env {
  const nav = win.navigator
  return {
    isSecureContext: win.isSecureContext,
    serviceWorker: 'serviceWorker' in nav ? nav.serviceWorker : null,
    hasPushManager: 'PushManager' in win,
    Notification: 'Notification' in win ? (win.Notification as unknown as Env['Notification']) : null,
    standalone: win.matchMedia?.('(display-mode: standalone)').matches ?? false,
  }
}

export function pushSupport(env: Env): PushSupport {
  if (!env.isSecureContext) return { supported: false, reason: 'insecure_context' }
  if (!env.serviceWorker) return { supported: false, reason: 'no_service_worker' }
  if (!env.hasPushManager) return { supported: false, reason: 'no_push_manager' }
  if (!env.Notification) return { supported: false, reason: 'no_notifications' }
  return { supported: true, permission: env.Notification.permission, standalone: env.standalone }
}

export function applicationServerKey(base64url: string): Uint8Array {
  const padded = (base64url + '='.repeat((4 - (base64url.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(padded)
  const bytes = new Uint8Array(raw.length)
  for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index)
  return bytes
}

export function subscriptionJson(subscription: Pick<PushSubscription, 'toJSON'>): WebPushSubscriptionJSON {
  const json = subscription.toJSON()
  if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) throw new Error('Browser returned an incomplete push subscription')
  return { endpoint: json.endpoint, expirationTime: json.expirationTime ?? null, keys: { p256dh: json.keys.p256dh, auth: json.keys.auth } }
}

/** Must be called from a user gesture (button click): asks permission, then subscribes. */
export async function enableBrowserPush(env: Env, publicKey: string, save: (sub: WebPushSubscriptionJSON) => Promise<PushSettings>): Promise<PushSettings> {
  const support = pushSupport(env)
  if (!support.supported) throw new Error('Push is not supported in this browser')
  const permission = support.permission === 'granted' ? 'granted' : await env.Notification!.requestPermission()
  if (permission !== 'granted') throw new Error(permission === 'denied' ? 'Notifications are blocked for RoomOS in browser settings' : 'Notification permission was not granted')
  const registration = await env.serviceWorker!.ready
  const existing = await registration.pushManager.getSubscription()
  const subscription = existing ?? await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: applicationServerKey(publicKey) as BufferSource })
  return save(subscriptionJson(subscription))
}

export async function disableBrowserPush(env: Env, remove: () => Promise<PushSettings>): Promise<PushSettings> {
  const registration = env.serviceWorker ? await env.serviceWorker.ready : null
  const existing = registration ? await registration.pushManager.getSubscription() : null
  const result = await remove()
  await existing?.unsubscribe().catch(() => undefined)
  return result
}

/** One-shot repair when the settings panel opens: the browser holds a subscription the server
    lost (or a rotated one). Never runs on the Panel bootstrap; never polls. */
export async function reconcileBrowserPush(env: Env, settings: PushSettings, save: (sub: WebPushSubscriptionJSON) => Promise<PushSettings>): Promise<PushSettings | null> {
  const support = pushSupport(env)
  if (!support.supported || support.permission !== 'granted' || !settings.can_manage || !settings.capabilities.webpush_available) return null
  const registration = await env.serviceWorker!.ready
  const existing = await registration.pushManager.getSubscription()
  // Only repair a subscription the server has lost; an invalidated one is never re-submitted.
  if (!existing || !settings.preferences.enabled || settings.channels.some((channel) => channel.kind === 'webpush')) return null
  return save(subscriptionJson(existing))
}

export function deliveryLabel(status: string): string {
  return ({
    accepted: 'Push service accepted', displayed: 'Shown on device', opened: 'Opened', pending: 'Queued',
    deferred: 'Waiting for quiet hours to end', failed: 'Failed', expired: 'Expired', superseded: 'No longer relevant',
    skipped: 'Not sent (preferences)', aggregated: 'Grouped into a summary', invalid: 'Subscription invalid', cancelled: 'Cancelled',
  } as Record<string, string>)[status] ?? status
}

const WORKER_PATH = /^\/notifications(\?alert=[a-z0-9._-]{1,80})?$/

/** A notification tap in an already-open RoomOS window routes in-app (no reload, no new socket). */
export function listenForWorkerNavigation(
  container: Pick<ServiceWorkerContainer, 'addEventListener' | 'removeEventListener'> | null,
  navigate: (path: string) => void,
): () => void {
  if (!container) return () => undefined
  const handler = (event: MessageEvent) => {
    const data = event.data as { type?: string; path?: string } | null
    if (data?.type === 'roomos:navigate' && typeof data.path === 'string' && WORKER_PATH.test(data.path)) navigate(data.path)
  }
  container.addEventListener('message', handler as EventListener)
  return () => container.removeEventListener('message', handler as EventListener)
}

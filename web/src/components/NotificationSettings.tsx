import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { browserEnv, deliveryLabel, disableBrowserPush, enableBrowserPush, pushSupport, reconcileBrowserPush, type PushSupport } from '../push-client'
import type { PushPreferencesUpdate, PushSettings } from '../types'
import { relativeActivityTime } from '../activity-ui'

type Deps = {
  load: () => Promise<PushSettings>
  update: (change: PushPreferencesUpdate) => Promise<PushSettings>
  support: () => PushSupport
  enable: (publicKey: string) => Promise<PushSettings>
  disable: () => Promise<PushSettings>
  reconcile: (settings: PushSettings) => Promise<PushSettings | null>
}

const defaultDeps = (): Deps => ({
  load: api.pushSettings,
  update: api.updatePushSettings,
  support: () => pushSupport(browserEnv()),
  enable: (key) => enableBrowserPush(browserEnv(), key, api.subscribeWebPush),
  disable: () => disableBrowserPush(browserEnv(), api.unsubscribeWebPush),
  reconcile: (settings) => reconcileBrowserPush(browserEnv(), settings, api.subscribeWebPush),
})

function supportText(support: PushSupport, standalone: boolean) {
  if (!support.supported) {
    return { no_service_worker: 'This browser has no service worker support, so RoomOS cannot notify while closed.',
      no_push_manager: 'This browser does not support Web Push.', no_notifications: 'This browser does not support system notifications.',
      insecure_context: 'Push needs the secure RoomOS address.' }[support.reason]
  }
  if (support.permission === 'denied') return 'Notifications are blocked for RoomOS. Allow them in the browser site settings.'
  if (support.permission === 'default') return 'The browser will ask for permission when you turn notifications on.'
  return standalone ? 'Allowed · works while the RoomOS app is closed.' : 'Allowed in this browser.'
}

export function NotificationSettingsPanel({ deps: injected }: { deps?: Deps }) {
  const [deps] = useState(() => injected ?? defaultDeps())
  const [settings, setSettings] = useState<PushSettings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [support] = useState(() => deps.support())

  useEffect(() => {
    let active = true
    void deps.load().then(async (loaded) => {
      if (!active) return
      setSettings(loaded)
      const repaired = await deps.reconcile(loaded).catch(() => null)
      if (active && repaired) setSettings(repaired)
    }).catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : 'Settings unavailable') })
    return () => { active = false }
  }, [deps])

  const run = useCallback(async (action: () => Promise<PushSettings>) => {
    setBusy(true); setError(null)
    try { setSettings(await action()) } catch (reason) { setError(reason instanceof Error ? reason.message : 'Change failed') } finally { setBusy(false) }
  }, [])

  if (error && !settings) return <p className="push-settings-note is-error" role="alert">{error}</p>
  if (!settings) return <p className="push-settings-note">Reading notification settings…</p>
  const prefs = settings.preferences
  const native = settings.session_kind === 'android'
  const webChannel = settings.channels.find((channel) => channel.kind === 'webpush')
  const fcmChannel = settings.channels.find((channel) => channel.kind === 'fcm')
  const canManage = settings.can_manage && !native
  const webPushReady = settings.capabilities.webpush_available && support.supported
  const save = (change: PushPreferencesUpdate) => run(() => deps.update(change))
  const toggleDevice = () => run(async () => {
    if (prefs.enabled) {
      await deps.update({ enabled: false })
      return webChannel ? deps.disable() : deps.load()
    }
    if (!fcmChannel && webPushReady) await deps.enable(settings.capabilities.webpush_public_key!)
    return deps.update({ enabled: true })
  })

  return <div className="push-settings" aria-label="Notification settings">
    {!settings.capabilities.delivery_active && <p className="push-settings-note">Push delivery is not active on this RoomOS server yet. Your choices are saved and apply when it is turned on.</p>}
    {!settings.can_receive && <p className="push-settings-note">This device has no Activity access, so it never receives alert content.</p>}
    {!settings.can_manage && <p className="push-settings-note">This device can view but not change notification settings (operator permission “notifications:self” is required).</p>}
    <dl className="push-settings-status">
      <div><dt>This device</dt><dd>{settings.display_name}</dd></div>
      <div><dt>Delivery</dt><dd>{fcmChannel ? `Android app · ${fcmChannel.status}` : webChannel ? `Web Push · ${webChannel.status}` : 'Not subscribed'}</dd></div>
      {!fcmChannel && <div><dt>Browser</dt><dd>{supportText(support, support.supported && support.standalone)}</dd></div>}
    </dl>
    <div className="push-settings-grid">
      <label className="push-toggle"><input type="checkbox" checked={prefs.enabled} disabled={!canManage || busy || (!fcmChannel && !webPushReady && !prefs.enabled)} onChange={() => void toggleDevice()} /><span>Notifications on this device</span></label>
      {(['critical', 'warning', 'info'] as const).map((level) => <label key={level} className="push-toggle"><input type="checkbox" checked={prefs[level]} disabled={!canManage || busy} onChange={() => void save({ [level]: !prefs[level] })} /><span>{({ critical: 'Critical', warning: 'Warning', info: 'Info' })[level]}</span></label>)}
      <label>Recovery notices<select value={prefs.recovery} disabled={!canManage || busy} onChange={(event) => void save({ recovery: event.target.value as PushSettings['preferences']['recovery'] })}><option value="off">Off</option><option value="critical">Critical only</option><option value="all">All</option></select></label>
      <label className="push-toggle"><input type="checkbox" checked={prefs.lock_screen_details} disabled={!canManage || busy} onChange={() => void save({ lock_screen_details: !prefs.lock_screen_details })} /><span>Show details on the lock screen</span></label>
    </div>
    <fieldset className="push-quiet" disabled={!canManage || busy}>
      <legend>Quiet hours ({settings.capabilities.timezone})</legend>
      <label className="push-toggle"><input type="checkbox" checked={prefs.quiet_hours.enabled} onChange={() => void save({ quiet_hours: { ...prefs.quiet_hours, enabled: !prefs.quiet_hours.enabled } })} /><span>Hold non-critical alerts</span></label>
      <label>From<input type="time" value={prefs.quiet_hours.start} onChange={(event) => event.target.value && void save({ quiet_hours: { ...prefs.quiet_hours, start: event.target.value } })} /></label>
      <label>Until<input type="time" value={prefs.quiet_hours.end} onChange={(event) => event.target.value && void save({ quiet_hours: { ...prefs.quiet_hours, end: event.target.value } })} /></label>
      <label className="push-toggle"><input type="checkbox" checked={prefs.quiet_hours.allow_critical} onChange={() => void save({ quiet_hours: { ...prefs.quiet_hours, allow_critical: !prefs.quiet_hours.allow_critical } })} /><span>Critical alerts still come through</span></label>
    </fieldset>
    <div className="push-settings-actions">
      {prefs.snooze_until && new Date(prefs.snooze_until) > new Date()
        ? <button type="button" disabled={!canManage || busy} onClick={() => void save({ snooze_minutes: 0 })}>Resume now (snoozed until {new Date(prefs.snooze_until).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })})</button>
        : <button type="button" disabled={!canManage || busy || !prefs.enabled} onClick={() => void save({ snooze_minutes: 60 })}>Snooze non-critical for 1 hour</button>}
    </div>
    {error && <p className="push-settings-note is-error" role="alert">{error}</p>}
    {settings.recent.length > 0 && <ol className="push-recent" aria-label="Recent deliveries to this device">{[...settings.recent].reverse().map((item) => <li key={item.id}><span className={`push-dot is-${item.severity}`} aria-hidden="true" /><span>{deliveryLabel(item.status)}</span><time dateTime={item.created_at}>{relativeActivityTime(item.created_at)}</time></li>)}</ol>}
  </div>
}

// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PushSettings } from '../types'
import { NotificationSettingsPanel } from './NotificationSettings'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const base: PushSettings = {
  device_id: 'dev', display_name: 'Second phone', session_kind: 'web', can_manage: true, can_receive: true,
  preferences: { enabled: false, critical: true, warning: true, info: false, recovery: 'critical', lock_screen_details: false,
    quiet_hours: { enabled: false, start: '23:00', end: '08:00', allow_critical: true } },
  channels: [], recent: [],
  capabilities: { mode: 'smart', delivery_active: true, webpush_available: true, webpush_public_key: 'KEY', fcm_available: true, timezone: 'Europe/Istanbul' },
}

let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.append(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove() })

function deps(settings: PushSettings, overrides = {}) {
  return {
    load: vi.fn(async () => settings),
    update: vi.fn(async (change) => ({ ...settings, preferences: { ...settings.preferences, ...change } })),
    support: () => ({ supported: true as const, permission: 'default' as NotificationPermission, standalone: true }),
    enable: vi.fn(async () => ({ ...settings, channels: [{ kind: 'webpush' as const, status: 'active' as const, since: 'now' }] })),
    disable: vi.fn(async () => ({ ...settings, channels: [] })),
    reconcile: vi.fn(async () => null),
    ...overrides,
  }
}

async function mount(d: ReturnType<typeof deps>) {
  await act(async () => { root.render(<NotificationSettingsPanel deps={d} />) })
  await act(async () => { await Promise.resolve() })
}

const checkbox = (label: string) => [...host.querySelectorAll('label')].find((node) => node.textContent?.includes(label))!.querySelector('input')!

describe('Notification Settings', () => {
  it('loads once and turning on asks the browser (user gesture) then saves enabled', async () => {
    const d = deps(base)
    await mount(d)
    expect(d.load).toHaveBeenCalledTimes(1)
    expect(host.textContent).toContain('The browser will ask for permission')
    await act(async () => { checkbox('Notifications on this device').click() })
    expect(d.enable).toHaveBeenCalledWith('KEY')
    expect(d.update).toHaveBeenLastCalledWith({ enabled: true })
  })

  it('saves severity, recovery, quiet hours and lock-screen choices for this device only', async () => {
    const d = deps({ ...base, preferences: { ...base.preferences, enabled: true } })
    await mount(d)
    await act(async () => { checkbox('Info').click() })
    expect(d.update).toHaveBeenLastCalledWith({ info: true })
    await act(async () => { checkbox('Hold non-critical alerts').click() })
    expect(d.update).toHaveBeenLastCalledWith({ quiet_hours: { enabled: true, start: '23:00', end: '08:00', allow_critical: true } })
    await act(async () => { checkbox('Show details on the lock screen').click() })
    expect(d.update).toHaveBeenLastCalledWith({ lock_screen_details: true })
    expect(host.textContent).toContain('Quiet hours (Europe/Istanbul)')
  })

  it('is read-only without notifications:self and explains missing Activity access', async () => {
    const d = deps({ ...base, can_manage: false, can_receive: false })
    await mount(d)
    expect(checkbox('Notifications on this device').disabled).toBe(true)
    expect(host.textContent).toContain('notifications:self')
    expect(host.textContent).toContain('no Activity access')
  })

  it('states unsupported browsers and an inactive server instead of pretending', async () => {
    const d = deps({ ...base, capabilities: { ...base.capabilities, mode: 'legacy', delivery_active: false } },
      { support: () => ({ supported: false as const, reason: 'no_push_manager' as const }) })
    await mount(d)
    expect(host.textContent).toContain('does not support Web Push')
    expect(host.textContent).toContain('not active on this RoomOS server yet')
    expect(checkbox('Notifications on this device').disabled).toBe(true)
  })

  it('shows the Android app channel instead of Web Push for native devices and recent truthful states', async () => {
    const d = deps({ ...base, channels: [{ kind: 'fcm', status: 'active', since: 'x' }],
      recent: [{ id: 'psh_1', transition: 'started', severity: 'warning', status: 'accepted', created_at: new Date().toISOString() }] })
    await mount(d)
    expect(host.textContent).toContain('Android app · active')
    expect(host.textContent).toContain('Push service accepted')
    expect(host.textContent).not.toContain('Browser')
  })
})

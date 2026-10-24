// @vitest-environment jsdom

import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { catalog, health, lights } from '../home-fixtures.test-support'
import type { AuthStatus } from '../types'
import { DEFAULT_PANEL_LAYOUT } from '../panel-layout'

// React only flushes act() work when the environment declares support for it.
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

type Status = 'connected' | 'reconnecting' | 'offline'
const harness = vi.hoisted(() => {
  const statusListeners = new Set<(status: string) => void>()
  const refreshListeners = new Set<() => Promise<void>>()
  const lifecycle = {
    snapshot: 'reconnecting' as string,
    subscribeStatus(listener: (status: string) => void) { statusListeners.add(listener); listener(lifecycle.snapshot); return () => { statusListeners.delete(listener) } },
    subscribeEvents(_listener: unknown, refresh?: () => Promise<void>) {
      if (refresh) refreshListeners.add(refresh)
      return () => { if (refresh) refreshListeners.delete(refresh) }
    },
  }
  // Same order as RoomOSLifecycle: every status listener first, then every refresh listener.
  const setStatus = (status: string) => {
    if (status === lifecycle.snapshot) return
    lifecycle.snapshot = status
    statusListeners.forEach((listener) => listener(status))
    if (status === 'connected') refreshListeners.forEach((refresh) => void refresh())
  }
  const names = ['health', 'lights', 'scenes', 'systemHealth', 'alerts', 'routines', 'devices', 'system', 'panelLayout', 'savePanelLayout', 'resetPanelLayout', 'activateScene', 'allPower', 'power', 'brightness', 'color', 'temperature', 'effect']
  const api = Object.fromEntries(names.map((name) => [name, vi.fn()])) as Record<string, ReturnType<typeof vi.fn>>
  return { lifecycle, setStatus, api }
})

vi.mock('../lifecycle', () => ({ roomosLifecycle: harness.lifecycle }))
vi.mock('../api', () => ({ api: harness.api }))
vi.mock('../camera-ui', () => ({ loadCameraAccess: vi.fn(async () => null) }))

import { AuthContext } from '../auth-context'
import { PanelPage } from './Panel'

const auth = { authenticated: true, device: { scopes: ['lights:read', 'lights:write', 'system:read', 'routines:read', 'activity:read', 'devices:read'] } } as unknown as AuthStatus
let root: Root
let container: HTMLDivElement

beforeEach(() => {
  const responses: Record<string, unknown> = {
    health, lights, scenes: catalog, systemHealth: { overall: 'healthy', checked_at: '2026-10-15T09:00:00Z', components: [] },
    alerts: { alerts: [] }, routines: { routines: [] }, devices: { devices: [] },
    panelLayout: { layout: DEFAULT_PANEL_LAYOUT, source: 'default', updated_at: null },
  }
  for (const [name, mock] of Object.entries(harness.api)) {
    mock.mockReset()
    mock.mockImplementation(async () => responses[name])
  }
  harness.lifecycle.snapshot = 'reconnecting'
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
})

async function mount() {
  await act(async () => {
    root.render(<AuthContext.Provider value={auth}><MemoryRouter><PanelPage /></MemoryRouter></AuthContext.Provider>)
  })
}
const settle = () => act(async () => { for (let i = 0; i < 5; i++) await Promise.resolve() })
async function status(next: Status) { await act(async () => harness.setStatus(next)); await settle() }

it('panel bootstrap reads /lights and /health exactly once and never /system', async () => {
  await mount()
  await status('connected')
  expect(harness.api.lights).toHaveBeenCalledTimes(1)
  expect(harness.api.health).toHaveBeenCalledTimes(1)
  expect(harness.api.devices).toHaveBeenCalledTimes(1)
  expect(harness.api.panelLayout).toHaveBeenCalledTimes(1)
  expect(harness.api.system).not.toHaveBeenCalled()
  // Both stores received the shared data: every light is rendered and controllable.
  expect(container.querySelectorAll('[data-light]').length).toBeGreaterThan(0)
})

it('applies a device layout to card order, visibility, density, header and scene order', async () => {
  harness.api.panelLayout.mockResolvedValue({
    layout: {
      ...DEFAULT_PANEL_LAYOUT,
      cards: ['scenes', 'ambient', 'main_lights', 'system', 'routines'],
      hidden: ['routines'], density: 'compact',
      scene_order: [...DEFAULT_PANEL_LAYOUT.scene_order].reverse(),
      header: { clock: false, date: false, connection: false, health: false },
    }, source: 'saved', updated_at: '2026-10-16T18:00:00Z',
  })
  harness.lifecycle.snapshot = 'connected'
  await mount(); await settle()
  expect(container.querySelector('.panel-page')?.classList.contains('is-compact')).toBe(true)
  expect(Array.from(container.querySelectorAll('[data-panel-card]')).map((node) => node.getAttribute('data-panel-card'))).toEqual(['scenes', 'ambient', 'main_lights', 'system'])
  expect(container.querySelector('.panel-clock')).toBeNull()
  expect(container.querySelector('.panel-health')).toBeNull()
  expect(container.querySelector('.panel-header-status .status-pill')).toBeNull()
  expect(Array.from(container.querySelectorAll('.panel-scenes button')).map((node) => node.textContent?.trim())).toEqual(['Tümü kapalı', 'Night', 'Movie', 'Gaming 2', 'Gaming 1', 'Work 2', 'Work 1', 'Normal'])
})

it('layout failure keeps safe defaults and never retries or polls', async () => {
  harness.api.panelLayout.mockRejectedValue(new Error('503'))
  harness.lifecycle.snapshot = 'connected'
  await mount(); await settle()
  expect(harness.api.panelLayout).toHaveBeenCalledTimes(1)
  expect(container.querySelectorAll('[data-panel-card]')).toHaveLength(5)
  expect(container.querySelector('.panel-clock')).not.toBeNull()
})

it('opening while already connected also reads each once', async () => {
  harness.lifecycle.snapshot = 'connected'
  await mount()
  await settle()
  expect(harness.api.lights).toHaveBeenCalledTimes(1)
  expect(harness.api.health).toHaveBeenCalledTimes(1)
})

it('a reconnect resynchronizes with one fresh /lights and /health', async () => {
  await mount()
  await status('connected')
  await status('reconnecting')
  await status('connected')
  expect(harness.api.lights).toHaveBeenCalledTimes(2)
  expect(harness.api.health).toHaveBeenCalledTimes(2)
  expect(harness.api.system).not.toHaveBeenCalled()
})

it('a failed shared read is retried on the next connection', async () => {
  harness.api.lights.mockRejectedValueOnce(new Error('503'))
  await mount()
  await status('connected')
  expect(harness.api.lights).toHaveBeenCalledTimes(1)
  await status('reconnecting')
  await status('connected')
  expect(harness.api.lights).toHaveBeenCalledTimes(2)
  expect(container.querySelectorAll('[data-light]').length).toBeGreaterThan(0)
})

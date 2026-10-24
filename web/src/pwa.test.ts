import { describe, expect, it, vi } from 'vitest'
import manifestText from '../public/manifest.webmanifest?raw'
import workerText from '../public/sw.js?raw'
import { activateWaitingWorker, installPwaLifecycle, isStandalone, shouldOfferUpdate } from './pwa'

describe('RoomOS PWA shell', () => {
  it('declares a standalone local controller manifest', () => {
    const manifest = JSON.parse(manifestText)
    expect(manifest.name).toBe('RoomOS')
    expect(manifest.display).toBe('standalone')
    expect(manifest.start_url).toBe('/home')
    expect(manifest.scope).toBe('/')
    expect(manifest.theme_color).toBe('#0a0712')
    expect(manifest.icons).toEqual(expect.arrayContaining([
      expect.objectContaining({ src: '/icons/electronic-m-v1-192.png', sizes: '192x192', type: 'image/png' }),
      expect.objectContaining({ src: '/icons/electronic-m-v1-maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' }),
    ]))
    expect(manifest.icons.every((icon: { src: string }) => !icon.src.includes('roomos-'))).toBe(true)
  })

  it('uses a network-only service worker with no state cache', () => {
    expect(workerText).not.toContain('caches.open')
    expect(workerText).not.toContain('cache.put')
    expect(workerText).not.toContain('event.respondWith')
    expect(workerText).not.toContain('localStorage')
    expect(workerText).not.toContain('indexedDB')
  })

  it('waits for an explicit update reload before activating a new worker', () => {
    expect(workerText).toContain("event.data?.type === 'SKIP_WAITING'")
    expect(workerText).not.toContain("addEventListener('install', () => self.skipWaiting())")
  })

  it('offers updates only to an already-controlled PWA and reloads by explicit message', async () => {
    const worker = { postMessage: vi.fn() } as unknown as ServiceWorker
    expect(shouldOfferUpdate(worker, worker)).toBe(true)
    expect(shouldOfferUpdate(worker, null)).toBe(false)
    await activateWaitingWorker({ getRegistration: async () => ({ waiting: worker }) as ServiceWorkerRegistration })
    expect(worker.postMessage).toHaveBeenCalledWith({ type: 'SKIP_WAITING' })
  })

  it('prompts for a waiting update and reloads exactly once after controller change', async () => {
    const listeners: Record<string, () => void> = {}
    const worker = { postMessage: vi.fn() } as unknown as ServiceWorker
    const registration = { waiting: worker, installing: null, addEventListener: vi.fn() } as unknown as ServiceWorkerRegistration
    const container = {
      controller: worker,
      register: vi.fn(async () => registration),
      getRegistration: vi.fn(async () => registration),
      addEventListener: vi.fn((name: string, listener: () => void) => { listeners[name] = listener }),
      removeEventListener: vi.fn(),
    } as unknown as ServiceWorkerContainer
    const update = vi.fn()
    const reload = vi.fn()
    installPwaLifecycle(update, container, reload)
    await Promise.resolve()
    await Promise.resolve()
    expect(update).toHaveBeenCalledWith('available')
    listeners.controllerchange()
    listeners.controllerchange()
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('recognizes installed standalone display mode without browser chrome assumptions', () => {
    expect(isStandalone(() => ({ matches: true }) as MediaQueryList, {} as Navigator)).toBe(true)
    expect(isStandalone(() => ({ matches: false }) as MediaQueryList, {} as Navigator)).toBe(false)
  })
})

it('does not reload on first-install takeover, but reloads once for a later replacement', () => {
  const listeners: Record<string, () => void> = {}
  const container = {
    controller: null,
    register: vi.fn(async () => ({ waiting: null, addEventListener: vi.fn() })),
    getRegistration: vi.fn(),
    addEventListener: vi.fn((name: string, listener: () => void) => { listeners[name] = listener }),
    removeEventListener: vi.fn(),
  } as unknown as ServiceWorkerContainer
  const reload = vi.fn()
  installPwaLifecycle(vi.fn(), container, reload)
  listeners.controllerchange()
  expect(reload).not.toHaveBeenCalled()
  listeners.controllerchange(); listeners.controllerchange()
  expect(reload).toHaveBeenCalledTimes(1)
})

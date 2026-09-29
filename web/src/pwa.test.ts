import { describe, expect, it } from 'vitest'
import manifestText from '../public/manifest.webmanifest?raw'
import workerText from '../public/sw.js?raw'

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
    expect(workerText).not.toContain('/api')
  })
})

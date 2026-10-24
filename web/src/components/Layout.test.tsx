import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { LifecycleBanners } from './Layout'

describe('global lifecycle banners', () => {
  it('stays quiet while connected', () => {
    const html = renderToStaticMarkup(<LifecycleBanners connection="connected" update="current" onReload={() => undefined} />)
    expect(html).not.toContain('Reconnecting')
    expect(html).not.toContain('offline')
  })

  it('distinguishes reconnecting and offline stale state with text', () => {
    expect(renderToStaticMarkup(<LifecycleBanners connection="reconnecting" update="current" onReload={() => undefined} />)).toContain('Reconnecting…')
    const offline = renderToStaticMarkup(<LifecycleBanners connection="offline" update="current" onReload={() => undefined} />)
    expect(offline).toContain('RoomOS offline')
    expect(offline).toContain('Controls unavailable')
  })

  it('renders an explicit user-controlled update action', () => {
    const html = renderToStaticMarkup(<LifecycleBanners connection="connected" update="available" onReload={() => undefined} />)
    expect(html).toContain('RoomOS update available')
    expect(html).toContain('Reload')
  })
})

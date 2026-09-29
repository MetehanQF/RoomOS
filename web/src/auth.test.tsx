import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { authView } from './auth'
import { PairingScreen } from './components/AuthBoundary'
import { BrandLogo } from './components/Brand'

describe('trusted-device authentication UI', () => {
  it('shows pairing for an unauthenticated or revoked session', () => {
    expect(authView(false, { authenticated: false, device: null })).toBe('pairing')
    expect(authView(false, null)).toBe('pairing')
  })

  it('moves a successfully paired session directly to the dashboard', () => {
    expect(authView(false, {
      authenticated: true,
      device: { id: 'device-1', friendly_name: 'Tablet', scopes: ['lights:read'] },
    })).toBe('dashboard')
  })

  it('keeps a loading state until session validation completes', () => {
    expect(authView(true, null)).toBe('loading')
  })

  it('renders a clean pairing screen with the new electronic M identity', () => {
    const html = renderToStaticMarkup(<PairingScreen onPaired={() => undefined} />)
    expect(html).toContain('Pair this device')
    expect(html).toContain('Pairing code')
    expect(html).toContain('Device name')
    expect(html).toContain('/icons/electronic-m-v1-192.png')
    expect(html).not.toContain('sparkles')
  })

  it('uses the electronic M throughout reusable branding', () => {
    expect(renderToStaticMarkup(<BrandLogo />)).toContain('/icons/electronic-m-v1-192.png')
  })
})

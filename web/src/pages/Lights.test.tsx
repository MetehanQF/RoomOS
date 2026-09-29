import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { LightCard } from './Lights'
import type { Light } from '../types'

const base: Light = {
  id: 'desk_ambient',
  name: 'Masa Ambiyans',
  type: 'light',
  available: true,
  on: false,
  brightness: 128,
  color_mode: 'color_temp',
  rgb: [255, 214, 170],
  hs: [30, 33],
  color_temperature_kelvin: 4500,
  effect: 'Off',
  effects: ['Off', 'Aurora'],
  capabilities: { power: true, brightness: true, color: true, temperature: true, effects: true, min_kelvin: 2500, max_kelvin: 9000 },
  last_updated: '2026-09-28T08:00:00Z',
}

const render = (light: Light, initialExpanded = false) => renderToStaticMarkup(<LightCard light={light} health={null} refresh={async () => undefined} initialExpanded={initialExpanded} />)

describe('LightCard daily and advanced layers', () => {
  it('renders daily controls with advanced sections collapsed by default', () => {
    const html = render(base)
    expect(html).toContain('aria-expanded="false"')
    expect(html).toContain('Brightness')
    expect(html).not.toContain('White temperature')
    expect(html).not.toContain('Select an effect')
  })

  it('renders only capability-supported expanded sections', () => {
    const html = render({ ...base, capabilities: { ...base.capabilities, color: false, effects: false }, effects: [] }, true)
    expect(html).toContain('aria-expanded="true"')
    expect(html).toContain('White temperature')
    expect(html).not.toContain('custom color')
    expect(html).not.toContain('Select an effect')
  })

  it('uses distinct ON, OFF, and unavailable card states', () => {
    expect(render({ ...base, on: true })).toContain('state-on')
    expect(render(base)).toContain('state-off')
    expect(render({ ...base, available: false })).toContain('state-unavailable')
  })
})

import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { LightDetail, LightsView, type LightsActions } from './Lights'
import { fixture, writeCalls } from '../lights-fixtures.test-support'
import type { Light } from '../types'

type F = ReturnType<typeof fixture>
const actionsFor = (f: F): LightsActions => ({
  can: (light, action) => f.store.can(light, action), canAllOn: f.store.canAllOn(), canAllOff: f.store.canAllOff(), reason: f.store.page().reason,
  power: vi.fn(), brightness: vi.fn(), color: vi.fn(), temperature: vi.fn(), effect: vi.fn(), allOn: vi.fn(), allOff: vi.fn(), dismiss: vi.fn(),
})
const view = (f: F, selectedId: string | null = 'room_main') => renderToStaticMarkup(<LightsView snapshot={f.store.getSnapshot()} actions={actionsFor(f)} selectedId={selectedId} onSelect={() => {}} />)
const detail = (f: F, id: string) => {
  const light = f.store.getSnapshot().lights!.find((item) => item.id === id) as Light
  return renderToStaticMarkup(<LightDetail light={light} snapshot={f.store.getSnapshot()} actions={actionsFor(f)} />)
}
const WRITE_CONTROL = /<(button|input)\b[^>]*class="(group-action|light-row-power|light-power|swatch|effect-chip|apply-colour|light-range)[^"]*"[^>]*>/g
const enabled = (html: string) => (html.match(WRITE_CONTROL) ?? []).filter((tag) => !tag.includes('disabled=""'))

describe('Lights page rendering', () => {
  it('lists all five lights in two sections with friendly names, no raw IDs, one live region', async () => {
    const f = fixture(); await f.store.load(); const html = view(f)
    expect(html.match(/data-light=/g)).toHaveLength(5)
    expect(html).toContain('>Room lights<'); expect(html).toContain('>LED strip<')
    for (const name of ['Main light', 'Corner light', 'Ambient light', 'Window light', 'Desk ambient']) expect(html).toContain(name)
    expect(html).not.toContain('light.'); expect(html).not.toContain('Lamp Vendor')
    expect(html.match(/aria-live="polite"/g)).toHaveLength(1)
    expect(html).toContain('3 of 5 on · 1 unavailable')
    expect(html).toContain('All on<'); expect(html).toContain('All off<')
  })
  it('does not flash fabricated counts while loading and keeps every write control disabled', () => {
    const f = fixture(); const html = view(f)
    expect(html).toContain('Loading lights…'); expect(html).not.toContain('0 of')
    expect(enabled(html)).toHaveLength(0)
  })
  it('read-only: every write control disabled with a visible reason', async () => {
    const f = fixture({}, ['lights:read']); await f.store.load()
    const html = view(f) + detail(f, 'room_main') + detail(f, 'room_3') + detail(f, 'desk_ambient')
    expect(html).toContain('This device can view the lights but not control them.')
    expect((html.match(WRITE_CONTROL) ?? []).length).toBeGreaterThan(20)
    expect(enabled(html)).toHaveLength(0)
    expect(writeCalls(f.api)).toBe(0)
  })
  it('writer: controls enabled only where the light and capability allow', async () => {
    const f = fixture(); await f.store.load()
    expect(enabled(detail(f, 'room_main')).length).toBeGreaterThan(3)
    expect(enabled(detail(f, 'room_2')).map((tag) => tag.includes('light-power'))).toEqual([true])
    expect(enabled(detail(f, 'room_4'))).toHaveLength(0)
  })
  it('selected detail: real Kelvin with per-device range, explicit effect labels', async () => {
    const f = fixture(); await f.store.load()
    const main = detail(f, 'room_main')
    expect(main).toContain('Warm · 2700 K'); expect(main).toContain('max="6500"'); expect(main).toMatch(/aria-pressed="true"[^>]*>White</)
    expect(main).toContain('>No effect<'); expect(main).toContain('>Party<'); expect(main).toContain('>Relax<')
    const strip = detail(f, 'desk_ambient')
    expect(strip).toContain('max="9000"'); expect(strip).toContain('effect-chips is-many'); expect(strip).toContain('LED strip')
  })
  it('colour light opens in colour mode with 8 swatches and explicit Apply colour; never a fabricated 4500 K', async () => {
    const f = fixture(); await f.store.load(); const html = detail(f, 'room_3')
    expect(html.match(/class="swatch /g)).toHaveLength(8)
    expect(html).toMatch(/class="swatch is-selected"[^>]*aria-pressed="true" aria-label="Red"/)
    expect(html).toContain('>Apply colour<')
    expect(html).not.toContain('4500'); expect(html).not.toContain('White temperature')
  })
  it('off light offers only power with a hint; unavailable light has no power button', async () => {
    const f = fixture(); await f.store.load()
    const off = detail(f, 'room_2')
    expect(off).toContain('Turn on to adjust brightness, colour and effects.'); expect(off).toContain('>Turn on<')
    expect(off).not.toContain('type="range"'); expect(off).not.toContain('swatch')
    const gone = detail(f, 'room_4')
    expect(gone).toContain('Unavailable'); expect(gone).not.toContain('class="light-power')
  })
  it('row targets carry accessible names; slider exposes value text', async () => {
    const f = fixture(); await f.store.load(); const html = view(f)
    expect(html).toContain('aria-label="Turn off Main light"'); expect(html).toContain('aria-label="Turn on Corner light"')
    expect(html).toContain('aria-valuetext="50 percent"')
  })
})

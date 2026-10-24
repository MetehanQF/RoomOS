import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { PanelSettingsSheet } from './Panel'
import { PanelAmbient, PanelMainLights, type MainLightsActions } from './PanelCards'
import { DEFAULT_PANEL_SETTINGS } from '../panel-mode'
import { DEFAULT_AMBIENT } from '../ambient-screen'
import { LightsStore, type LightsApi, type LightsSnapshot } from '../lights-store'
import { health, lights } from '../lights-fixtures.test-support'
import { summarizeMainLights } from '../main-lights'
import type { Light } from '../types'
import type { LightsActions } from './Lights'

const never = () => new Promise<never>(() => undefined)
const api = Object.fromEntries(['lights', 'health', 'power', 'brightness', 'color', 'temperature', 'effect', 'allPower', 'activateScene', 'mainPower', 'mainBrightness', 'mainTemperature'].map((k) => [k, never])) as unknown as LightsApi
const MAIN = ['room_main', 'room_2', 'room_3', 'room_4']
const permitted = { ...health, writable_permissions: { ...health.writable_permissions, main_room_lights: ['power', 'brightness', 'temperature'] as Array<'power' | 'brightness' | 'temperature'> } }
const white = (change: Partial<Light> = {}) => lights.map((light) => MAIN.includes(light.id) ? { ...light, available: true, on: true, brightness: 128, color_mode: 'color_temp', color_temperature_kelvin: 3000, ...change } : light)
const base: LightsSnapshot = { lights: white(), lightsStatus: 'ready', control: permitted, controlStatus: 'ready', connection: 'connected', trusted: true, refreshing: false, pending: null, feedback: null, announcement: null }

function storeFor(snapshot: LightsSnapshot, scopes: string[]) {
  const store = new LightsStore(api, scopes, true, snapshot.connection)
  ;(store as unknown as { snapshot: LightsSnapshot }).snapshot = snapshot
  return store
}
function main(snapshot: LightsSnapshot, scopes = ['lights:read', 'lights:write']) {
  const store = storeFor(snapshot, scopes)
  const actions: MainLightsActions = { can: (action) => store.canMain(action), power: () => undefined, brightness: () => undefined, temperature: () => undefined }
  return renderToStaticMarkup(<MemoryRouter><PanelMainLights summary={summarizeMainLights(snapshot.lights)} snapshot={snapshot} live actions={actions} onDismiss={() => undefined} /></MemoryRouter>)
}
function ambient(snapshot: LightsSnapshot, light: Light | null, scopes = ['lights:read', 'lights:write']) {
  const store = storeFor(snapshot, scopes)
  const actions: LightsActions = {
    can: (l, action) => store.can(l, action), canAllOn: false, canAllOff: false, reason: null,
    power: () => undefined, brightness: () => undefined, color: () => undefined, temperature: () => undefined, effect: () => undefined, allOn: () => undefined, allOff: () => undefined, dismiss: () => undefined,
  }
  return renderToStaticMarkup(<MemoryRouter><PanelAmbient light={light} snapshot={snapshot} actions={actions} onMore={() => undefined} /></MemoryRouter>)
}
const power = (html: string) => html.match(/<button[^>]*class="panel-power[^"]*"[^>]*>/)?.[0] ?? ''

describe('Ana Oda Işıkları card', () => {
  it('one card for the four room lights with state, count, one power button and shared sliders', () => {
    const html = main(base)
    expect(html).toContain('Ana Oda Işıkları')
    expect(html).toContain('Açık · 4/4')
    expect(html).toContain('data-group="main_room_lights"')
    expect(html.match(/class="panel-power/g)).toHaveLength(1)
    expect(power(html)).not.toContain('disabled')
    expect(html).toContain('id="main-brightness"')
    expect(html).toContain('id="main-kelvin"')
    expect(html).toContain('50%')
    expect(html).toContain('3000 K')
    expect(html).not.toContain('Desk') // the strip is never part of the group
  })

  it('mixed brightness and temperature are shown as ranges, not one number', () => {
    const lightsMixed = white().map((light) => light.id === 'room_main' ? { ...light, brightness: 26, color_temperature_kelvin: 6000 } : light)
    const html = main({ ...base, lights: lightsMixed })
    expect(html).toContain('Karışık · 10–50%')
    expect(html).toContain('Karışık · 3000–6000 K')
    expect(html).toContain('is-mixed')
  })

  it('partly on: mixed power state, no shared sliders, explains why', () => {
    const partial = white().map((light) => light.id === 'room_2' ? { ...light, on: false, brightness: null } : light)
    const html = main({ ...base, lights: partial })
    expect(html).toContain('Kısmen açık · 3/4')
    expect(power(html)).toContain('aria-pressed="mixed"')
    expect(html).not.toContain('id="main-brightness"')
    expect(html).toContain('önce tüm ışıkları açın')
  })

  it('all off shows Kapalı and only the power control', () => {
    const html = main({ ...base, lights: white({ on: false, brightness: null, color_mode: null, color_temperature_kelvin: null }) })
    expect(html).toContain('Kapalı · 0/4')
    expect(html).not.toContain('type="range"')
  })

  it('read-only, stale and policy-less sessions cannot control the group', () => {
    for (const [snapshot, scopes] of [[base, ['lights:read']], [{ ...base, trusted: false }, ['lights:read', 'lights:write']], [{ ...base, connection: 'reconnecting' as const }, ['lights:read', 'lights:write']], [{ ...base, control: health }, ['lights:read', 'lights:write']]] as Array<[LightsSnapshot, string[]]>) {
      const html = main(snapshot, scopes)
      expect(power(html)).toContain('disabled')
      expect(html).not.toMatch(/<input[^>]*id="main-brightness"(?![^>]*disabled)/)
    }
  })
})

describe('Ambiyans card', () => {
  const desk = (change: Partial<Light> = {}) => ({ ...lights.find((light) => light.id === 'desk_ambient')!, ...change })

  it('on: power, brightness, colour/white modes from advertised capabilities', () => {
    const html = ambient(base, desk({ color_mode: 'hs', rgb: [255, 55, 70] }))
    expect(html).toContain('Ambiyans')
    expect(html).toContain('data-light="desk_ambient"')
    expect(html).toContain('brightness-desk_ambient')
    expect(html).toContain('Renk')
    expect(html).toContain('Beyaz')
    expect(html).toContain('swatch-grid')
    expect(html).toContain('Efektler ve daha fazlası')
  })

  it('hides controls the hardware does not advertise', () => {
    const plain = desk({ capabilities: { ...desk().capabilities, color: false, effects: false }, effects: [] })
    const html = ambient(base, plain)
    expect(html).not.toContain('swatch-grid')
    expect(html).not.toContain('Ambiyans modu')
    expect(html).toContain('kelvin-desk_ambient')
    expect(html).not.toContain('Efektler')
  })

  it('off shows only power and a note; unavailable has no power button', () => {
    const off = ambient(base, desk({ on: false, brightness: null }))
    expect(off).not.toContain('type="range"')
    expect(off).toContain('Parlaklık ve renk için açın')
    const gone = ambient(base, desk({ available: false, on: null }))
    expect(gone).not.toContain('class="panel-power')
    expect(gone).toContain('Erişilemiyor')
  })
})

describe('Panel settings', () => {
  it('settings explain Android limits and disable an unsupported wake lock', () => {
    const html = renderToStaticMarkup(<PanelSettingsSheet settings={DEFAULT_PANEL_SETTINGS} change={() => undefined} ambient={DEFAULT_AMBIENT} changeAmbient={() => undefined} wake="unsupported" onClose={() => undefined} />)
    expect(html).toMatch(/role="switch" aria-checked="false" disabled=""/)
    expect(html).toContain('cannot change brightness or guarantee the screen stays on')
    expect(html).toContain('Auto 21–07')
    expect(html).toContain('Ambient Screen')
    expect(html).toContain('1 min')
    expect(html).toContain('Very dark')
    expect(html).toContain('System status')
    expect((html.match(/aria-pressed="true"/g) ?? []).length).toBeGreaterThanOrEqual(3)
  })
})

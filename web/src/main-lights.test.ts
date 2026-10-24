import { describe, expect, it } from 'vitest'
import { MAIN_LIGHT_IDS, mainLightsStatusLabel, summarizeMainLights } from './main-lights'
import type { Light } from './types'

const CAPS: Light['capabilities'] = { power: true, brightness: true, color: true, temperature: true, effects: true, min_kelvin: 2500, max_kelvin: 6500 }
const light = (id: string, change: Partial<Light> = {}): Light => ({
  id, name: id, type: 'light', available: true, on: true, brightness: 128, color_mode: 'color_temp', rgb: null, hs: null,
  color_temperature_kelvin: 3000, effect: null, effects: [], capabilities: { ...CAPS }, last_updated: '2026-10-15T10:00:00Z', ...change,
})
const room = (change: Partial<Record<string, Partial<Light>>> = {}) => [
  ...MAIN_LIGHT_IDS.map((id) => light(id, change[id])),
  light('desk_ambient', { capabilities: { ...CAPS, max_kelvin: 9000 }, ...change.desk_ambient }),
]

describe('main room lights summary', () => {
  it('covers exactly the four L530 room lights, never the desk strip', () => {
    expect(MAIN_LIGHT_IDS).toEqual(['room_main', 'room_2', 'room_3', 'room_4'])
    const summary = summarizeMainLights(room({ desk_ambient: { on: false } }))!
    expect(summary.members.map((m) => m.id)).toEqual([...MAIN_LIGHT_IDS])
    expect(summary.total).toBe(4)
  })

  it('all on with one brightness and one white temperature is a single measurement', () => {
    const summary = summarizeMainLights(room())!
    expect(summary.power).toBe('on')
    expect(summary.onCount).toBe(4)
    expect(summary.brightness).toEqual({ kind: 'uniform', value: 50 })
    expect(summary.kelvin).toEqual({ kind: 'uniform', value: 3000 })
    expect(mainLightsStatusLabel(summary)).toBe('Açık · 4/4')
  })

  it('all off', () => {
    const summary = summarizeMainLights(room(Object.fromEntries(MAIN_LIGHT_IDS.map((id) => [id, { on: false, brightness: null, color_mode: null, color_temperature_kelvin: null }]))))!
    expect(summary.power).toBe('off')
    expect(summary.brightness).toEqual({ kind: 'none' })
    expect(summary.kelvin).toEqual({ kind: 'none' })
    expect(mainLightsStatusLabel(summary)).toBe('Kapalı · 0/4')
  })

  it('partly on is "Kısmen açık" with the honest count', () => {
    const summary = summarizeMainLights(room({ room_2: { on: false, brightness: null }, room_4: { on: false, brightness: null } }))!
    expect(summary.power).toBe('partial')
    expect(summary.onCount).toBe(2)
    expect(mainLightsStatusLabel(summary)).toBe('Kısmen açık · 2/4')
  })

  it('different brightness values are reported as a range, never averaged', () => {
    const summary = summarizeMainLights(room({ room_main: { brightness: 26 }, room_3: { brightness: 230 } }))!
    expect(summary.brightness).toEqual({ kind: 'mixed', min: 10, max: 90 })
  })

  it('brightness within one raw step counts as uniform', () => {
    expect(summarizeMainLights(room({ room_2: { brightness: 129 } }))!.brightness).toEqual({ kind: 'uniform', value: 50 })
  })

  it('different white temperatures are a range; a member in colour mode makes white mixed', () => {
    expect(summarizeMainLights(room({ room_2: { color_temperature_kelvin: 6000 } }))!.kelvin).toEqual({ kind: 'mixed', min: 3000, max: 6000 })
    const colour = summarizeMainLights(room({ room_3: { color_mode: 'hs', rgb: [255, 0, 0], color_temperature_kelvin: null } }))!
    expect(colour.kelvin).toEqual({ kind: 'mixed', min: 3000, max: 3000 })
    expect(colour.colourMembers).toBe(1)
  })

  it('unknown brightness on an on member is not invented', () => {
    const summary = summarizeMainLights(room({ room_main: { brightness: null } }))!
    expect(summary.brightness).toEqual({ kind: 'uniform', value: 50 })
    expect(summary.brightnessUnknown).toBe(1)
  })

  it('unavailable members are counted separately and excluded from on/off', () => {
    const summary = summarizeMainLights(room({ room_4: { available: false, on: null, brightness: null } }))!
    expect(summary.available).toBe(3)
    expect(summary.power).toBe('on')
    expect(mainLightsStatusLabel(summary)).toBe('Açık · 3/4 · 1 erişilemiyor')
    const none = summarizeMainLights(room(Object.fromEntries(MAIN_LIGHT_IDS.map((id) => [id, { available: false, on: null }]))))!
    expect(none.power).toBe('unavailable')
    expect(mainLightsStatusLabel(none)).toBe('Erişilemiyor')
  })

  it('kelvin range is the members common range', () => {
    const summary = summarizeMainLights(room({ room_2: { capabilities: { ...CAPS, min_kelvin: 2700 } } }))!
    expect([summary.minKelvin, summary.maxKelvin]).toEqual([2700, 6500])
  })

  it('missing members or no data yields null', () => {
    expect(summarizeMainLights(null)).toBeNull()
    expect(summarizeMainLights(room().filter((l) => l.id !== 'room_3'))).toBeNull()
  })
})

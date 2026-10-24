import { brightnessToPercent, lightNames, temperatureLabel, type LightAction } from './ui'
import type { ConnectionStatus, Health, Light } from './types'

/** UI grouping only; backend logical IDs are unchanged. */
export const LIGHT_SECTIONS: ReadonlyArray<{ key: 'room' | 'strip'; title: string; ids: readonly string[] }> = [
  { key: 'room', title: 'Room lights', ids: ['room_main', 'room_2', 'room_3', 'room_4'] },
  { key: 'strip', title: 'LED strip', ids: ['desk_ambient'] },
]

/** Curated colours sent through the existing RGB endpoint, one command per deliberate tap. */
export const COLOR_SWATCHES: ReadonlyArray<{ name: string; rgb: [number, number, number] }> = [
  { name: 'Red', rgb: [255, 55, 70] },
  { name: 'Orange', rgb: [255, 128, 40] },
  { name: 'Amber', rgb: [255, 190, 60] },
  { name: 'Green', rgb: [40, 220, 115] },
  { name: 'Cyan', rgb: [0, 220, 255] },
  { name: 'Blue', rgb: [32, 102, 255] },
  { name: 'Purple', rgb: [128, 0, 255] },
  { name: 'Pink', rgb: [255, 80, 180] },
]

export const displayName = (light: Pick<Light, 'id' | 'name'>) => lightNames[light.id] ?? light.name

/** "Off" is an effect entry in the device catalog; it means no effect. The API value stays "Off". */
export const effectLabel = (effect: string) => (effect === 'Off' ? 'No effect' : effect)

export type ColorMode = 'white' | 'colour' | 'unknown'

/** Only HA's reported color_mode establishes the mode; nothing is inferred from defaults. */
export function colorMode(light: Light): ColorMode {
  if (!light.available || light.on !== true || !light.color_mode) return 'unknown'
  if (light.color_mode === 'color_temp') return 'white'
  if (['hs', 'rgb', 'rgbw', 'rgbww', 'xy'].includes(light.color_mode)) return 'colour'
  return 'unknown'
}

/** Real Kelvin only when the light is actually in white mode with a reported value. */
export function kelvinValue(light: Light): number | null {
  return colorMode(light) === 'white' && typeof light.color_temperature_kelvin === 'number' ? light.color_temperature_kelvin : null
}

export function stateLine(light: Light) {
  if (!light.available) return 'Unavailable'
  if (light.on !== true) return light.on === false ? 'Off' : 'State unavailable'
  const parts = [light.capabilities.brightness && light.brightness !== null ? `On · ${brightnessToPercent(light.brightness)}%` : 'On']
  const kelvin = kelvinValue(light)
  if (kelvin !== null) parts.push(`${temperatureLabel(kelvin)} ${kelvin} K`)
  else if (colorMode(light) === 'colour') parts.push('Colour')
  if (light.effect && light.effect !== 'Off' && light.effects.includes(light.effect)) parts.push(light.effect)
  return parts.join(' · ')
}

/** Small preview colour; null when nothing trustworthy is known. */
export function previewColor(light: Light): string | null {
  if (!light.available || light.on !== true) return null
  if (colorMode(light) === 'colour' && light.rgb) return `rgb(${light.rgb.join(',')})`
  const kelvin = kelvinValue(light)
  if (kelvin !== null) return kelvin < 3500 ? 'rgb(255,214,170)' : kelvin < 5500 ? 'rgb(255,244,226)' : 'rgb(213,230,255)'
  return null
}

export type LightsControl = { ok: boolean; reason: string | null }

/** Shared preconditions for any write on this page (fail-closed). */
export function pageControl({ authResolved, scopes, connection, control, trusted }: {
  authResolved: boolean; scopes: readonly string[]; connection: ConnectionStatus; control: Health | null; trusted: boolean
}): LightsControl {
  const blocked = (reason: string) => ({ ok: false, reason })
  if (!authResolved) return blocked('Checking access…')
  if (!scopes.includes('lights:write')) return blocked('This device can view the lights but not control them.')
  if (!control) return blocked('Waiting for RoomOS…')
  if (!control.control_enabled) return blocked('Controls are turned off on the server.')
  if (connection === 'offline') return blocked('Offline · controls unavailable.')
  if (connection !== 'connected') return blocked('Reconnecting · controls paused.')
  if (control.home_assistant !== 'connected') return blocked('Home Assistant is unavailable.')
  if (!trusted) return blocked('Refreshing light state · controls paused.')
  return { ok: true, reason: null }
}

/**
 * Per-light, per-action permission. Attribute changes require the light to be ON because the
 * existing endpoints use HA turn_on and would otherwise silently power the light on.
 */
export function canAct(page: LightsControl, control: Health | null, light: Light, action: LightAction, busy: boolean) {
  if (!page.ok || busy || !light.available) return false
  if (!control?.writable_permissions?.[light.id]?.includes(action)) return false
  if (action === 'power') return light.capabilities.power
  if (light.on !== true) return false
  if (action === 'brightness') return light.capabilities.brightness
  if (action === 'color') return light.capabilities.color
  if (action === 'temperature') return light.capabilities.temperature
  return light.capabilities.effects && light.effects.length > 0
}

export function canAllOn(page: LightsControl, control: Health | null, busy: boolean) {
  return page.ok && !busy && Boolean(control?.writable_permissions?.all_room_lights?.includes('power'))
}

/**
 * The all_off scene targets every room light with power only. The server marks it activatable
 * exactly when lights:write is present and policy allows power on each target, which is what
 * this mirrors; the server remains authoritative.
 */
export function canAllOff(page: LightsControl, control: Health | null, lights: Light[] | null, busy: boolean) {
  if (!page.ok || busy || !lights || !LIGHT_SECTIONS.flatMap((s) => s.ids).every((id) => lights.some((light) => light.id === id))) return false
  return lights.every((light) => control?.writable_permissions?.[light.id]?.includes('power'))
}

/** HA ISO last_updated includes microseconds; Date.parse alone would lose their ordering. */
export function observationTime(value: string | null): bigint | null {
  if (!value) return null
  const match = /^(.*T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$/.exec(value)
  if (!match) return null
  const seconds = Date.parse(match[1] + match[3])
  return Number.isFinite(seconds) ? BigInt(seconds) * 1_000_000n + BigInt((match[2] ?? '').padEnd(9, '0')) : null
}

export function isNewer(next: Light, current: Light | undefined) {
  const a = observationTime(next.last_updated)
  const b = observationTime(current?.last_updated ?? null)
  // Missing state may mean HA removed an entity. Accept loss of availability fail-closed,
  // but an undated available frame must never restore controls over a dated observation.
  if (a === null) return !next.available || b === null
  return b === null || a > b
}

export function clampKelvin(value: number, light: Light) {
  return Math.round(Math.min(light.capabilities.max_kelvin, Math.max(light.capabilities.min_kelvin, value)))
}

export function hexToRgb(hex: string): [number, number, number] | null {
  const match = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex)
  return match ? [parseInt(match[1], 16), parseInt(match[2], 16), parseInt(match[3], 16)] : null
}

export function rgbToHex(rgb: [number, number, number]) {
  return `#${rgb.map((channel) => channel.toString(16).padStart(2, '0')).join('')}`
}

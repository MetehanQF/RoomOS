import { brightnessToPercent } from './ui'
import { colorMode } from './lights-state'
import type { Light } from './types'

/**
 * Phase 7E.1 "Ana Oda Işıkları": the four L530 room lights as one panel control. Mirrors the
 * Core group main_room_lights (and the HA group light.all_lights). Desk Ambient is never a member.
 */
export const MAIN_LIGHT_IDS = ['room_main', 'room_2', 'room_3', 'room_4'] as const
export const MAIN_GROUP_ID = 'main_room_lights'

/** A group reading is either one real value, an explicit range, or nothing known. */
export type GroupValue = { kind: 'none' } | { kind: 'uniform'; value: number } | { kind: 'mixed'; min: number; max: number }

export type MainLightsSummary = {
  members: Light[]
  total: number
  available: number
  onCount: number
  power: 'on' | 'off' | 'partial' | 'unavailable'
  /** Percent, from on members that report brightness. */
  brightness: GroupValue
  brightnessUnknown: number
  /** Kelvin of on members in white mode; mixed when values differ or some on member shows colour. */
  kelvin: GroupValue
  colourMembers: number
  minKelvin: number
  maxKelvin: number
}

function group(values: number[], tolerance: number, forceMixed = false): GroupValue {
  if (!values.length) return { kind: 'none' }
  const min = Math.min(...values), max = Math.max(...values)
  if (!forceMixed && max - min <= tolerance) return { kind: 'uniform', value: values[0] }
  return { kind: 'mixed', min, max }
}

export function summarizeMainLights(lights: Light[] | null): MainLightsSummary | null {
  if (!lights) return null
  const members = MAIN_LIGHT_IDS.map((id) => lights.find((light) => light.id === id))
  if (members.some((member) => !member)) return null
  const all = members as Light[]
  const available = all.filter((light) => light.available)
  const on = available.filter((light) => light.on === true)
  const raw = on.map((light) => light.brightness).filter((value): value is number => typeof value === 'number')
  // One raw step (≈0.4 %) is rounding, not a different setting.
  const brightness = raw.length && Math.max(...raw) - Math.min(...raw) <= 1
    ? { kind: 'uniform' as const, value: brightnessToPercent(raw[0]) }
    : group(raw.map(brightnessToPercent), 0)
  const white = on.filter((light) => colorMode(light) === 'white' && typeof light.color_temperature_kelvin === 'number')
  const colourMembers = on.filter((light) => colorMode(light) === 'colour').length
  return {
    members: all,
    total: all.length,
    available: available.length,
    onCount: on.length,
    power: !available.length ? 'unavailable' : on.length === available.length ? 'on' : on.length === 0 ? 'off' : 'partial',
    brightness,
    brightnessUnknown: on.length - raw.length,
    kelvin: group(white.map((light) => light.color_temperature_kelvin as number), 50, colourMembers > 0),
    colourMembers,
    minKelvin: Math.max(...all.map((light) => light.capabilities.min_kelvin)),
    maxKelvin: Math.min(...all.map((light) => light.capabilities.max_kelvin)),
  }
}

const POWER_LABEL: Record<Exclude<MainLightsSummary['power'], 'unavailable'>, string> = { on: 'Açık', off: 'Kapalı', partial: 'Kısmen açık' }

export function mainLightsStatusLabel(summary: MainLightsSummary) {
  if (summary.power === 'unavailable') return 'Erişilemiyor'
  const missing = summary.total - summary.available
  return `${POWER_LABEL[summary.power]} · ${summary.onCount}/${summary.total}${missing ? ` · ${missing} erişilemiyor` : ''}`
}

export function groupValueLabel(value: GroupValue, unit: '%' | ' K') {
  if (value.kind === 'none') return '—'
  if (value.kind === 'uniform') return `${value.value}${unit}`
  return `Karışık · ${value.min}–${value.max}${unit}`
}

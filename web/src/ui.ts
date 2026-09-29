import type { GroupCommandResult, Health, Light, RoomOSEvent } from './types'

export type LightAction = 'power' | 'brightness' | 'color' | 'temperature' | 'effect'

/**
 * Display names come from Core's device registry, so the client never carries a
 * second copy of the deployment's device list.
 */
export function lightNameMap(lights: Light[] | null | undefined): Record<string, string> {
  return Object.fromEntries((lights ?? []).map((light) => [light.id, light.name]))
}

export function brightnessToPercent(value: number | null) {
  return value === null ? 0 : Math.round((value / 255) * 100)
}

export function percentToBrightness(percent: number) {
  return Math.max(1, Math.min(255, Math.round((percent / 100) * 255)))
}

export function kelvinPosition(kelvin: number | null, min: number, max: number) {
  if (kelvin === null || max <= min) return 0
  return Math.round(((Math.min(max, Math.max(min, kelvin)) - min) / (max - min)) * 100)
}

export function controlAccess(light: Light, health: Health | null) {
  const scoped = (action: LightAction) => Boolean(
    light.available &&
    health?.control_enabled &&
    health.writable_permissions?.[light.id]?.includes(action),
  )
  return {
    power: light.capabilities.power && scoped('power'),
    brightness: light.capabilities.brightness && scoped('brightness'),
    color: light.capabilities.color && scoped('color'),
    temperature: light.capabilities.temperature && scoped('temperature'),
    effect: light.capabilities.effects && light.effects.length > 0 && scoped('effect'),
  }
}

export function groupControlEnabled(health: Health | null) {
  return Boolean(
    health?.control_enabled &&
    health.writable_permissions?.all_room_lights?.includes('power'),
  )
}

export function applyLightEvent(lights: Light[] | null, event: RoomOSEvent) {
  if (!lights || event.type !== 'light.state_changed' || !event.data.light) return lights
  return lights.map((light) => light.id === event.device_id ? event.data.light! : light)
}

export function describeGroupResult(
  result: GroupCommandResult,
  names: Record<string, string> = {},
) {
  if (result.accepted && result.failed.length === 0) {
    return { tone: 'success' as const, title: 'All lights updated', detail: `${result.succeeded.length} of ${result.succeeded.length} lights responded.` }
  }
  const failed = result.failed.map((item) => names[item.device_id] ?? item.device_id)
  return {
    tone: 'partial' as const,
    title: 'Some lights did not respond',
    detail: failed.join(', '),
  }
}

export function formatValue(value: unknown, unit?: string | null) {
  if (value === null || value === undefined || value === '') return 'Unknown'
  const rendered = typeof value === 'number' ? Number(value.toFixed(1)).toString() : String(value)
  return unit ? `${rendered} ${unit}` : rendered
}

export function temperatureLabel(kelvin: number | null) {
  if (kelvin === null) return 'No temperature set'
  if (kelvin < 3600) return 'Warm'
  if (kelvin < 5600) return 'Balanced'
  return 'Cool'
}

export function expandedLightSections(light: Light) {
  return {
    temperature: light.capabilities.temperature,
    color: light.capabilities.color,
    effects: light.capabilities.effects && light.effects.length > 0,
  }
}

export function lightVisualState(light: Light) {
  if (!light.available) return 'unavailable'
  return light.on ? 'on' : 'off'
}

export function isCurrentColor(current: [number, number, number] | null, preset: [number, number, number]) {
  if (!current) return false
  return current.every((channel, index) => Math.abs(channel - preset[index]) <= 4)
}

export function serviceName(id: string) {
  if (id === 'dns') return 'DNS'
  return id.replaceAll('_', ' ')
}

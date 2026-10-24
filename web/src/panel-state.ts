import { automationLines, SCENE_TILES, type SceneId } from './home-state'
import type { IconName } from './components/Icons'
import type { DeviceDetail, RoutineCatalog } from './types'
import { relativeTime } from './devices-state'

const DEVICE_ORDER = ['primary', 'secondary', 'workstation', 'phone']

export function panelDevices(devices: readonly DeviceDetail[]) {
  return devices.filter((device) => DEVICE_ORDER.includes(device.id)).sort((a, b) => DEVICE_ORDER.indexOf(a.id) - DEVICE_ORDER.indexOf(b.id))
}

export function panelDeviceTone(device: DeviceDetail) {
  if (device.connection === 'offline' || device.health === 'offline') return 'offline'
  if (device.connection === 'unknown' || device.health === 'unknown') return 'unknown'
  if (device.telemetry_freshness === 'stale') return 'stale'
  if (device.health === 'degraded') return 'degraded'
  return 'healthy'
}

export function panelRoutines(catalog: RoutineCatalog | null) {
  return automationLines(catalog).filter((routine) => routine.enabled).slice(0, 3)
}

export type PanelScene = { id: SceneId; label: string; hint: string; icon: IconName }

export function panelScenes(): PanelScene[] {
  const result: PanelScene[] = []
  for (const tile of SCENE_TILES) {
    if (tile.kind === 'single') result.push(tile)
    else tile.variants.forEach((variant) => result.push({ id: variant.id, label: `${tile.label} ${variant.label}`, hint: variant.hint, icon: tile.icon }))
  }
  return result
}

/** One short, honest line per device: phones that only report while open read "Unknown", never "Offline". */
export function panelDeviceDetail(device: DeviceDetail, now: number) {
  const seen = relativeTime(device.last_seen_at, now)
  if (!device.sources.some((source) => source.configured)) return 'Not connected'
  if (device.connection === 'online') {
    const value = (id: string) => device.sensors.find((sensor) => sensor.id === id && sensor.availability === 'available' && sensor.freshness === 'fresh')?.value
    const battery = value('battery_percent'), cpu = value('cpu_usage')
    if (device.type === 'android' && typeof battery === 'number') return `Online · battery ${Math.round(battery)}%`
    if (typeof cpu === 'number') return `Online · CPU ${Math.round(cpu)}%`
    return 'Online'
  }
  if (device.connection === 'offline') return `Offline · last report ${seen}`
  return device.last_seen_at ? `Unknown · last report ${seen}` : 'Unknown · no report yet'
}

export const PANEL_DEVICE_LABEL: Record<string, string> = { primary: 'Primary', secondary: 'Secondary', workstation: 'Workstation', phone: 'Phone' }

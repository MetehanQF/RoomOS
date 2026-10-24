import type { DeviceDetail } from './types'

export const DEVICE_TABS=['Overview','Performance','Storage','Network','Alerts & Events'] as const
export const PERFORMANCE_SENSORS=['cpu_usage','ram_usage','gpu_usage','cpu_temperature','gpu_temperature','ram_total','ram_used','gpu_memory']
export const STORAGE_SENSORS=['disk_usage','storage_free','storage_used','storage_total']
const OVERVIEW_SENSORS=['cpu_usage','ram_usage','disk_usage','battery_percent','ac_connected']
export type DeviceTab=typeof DEVICE_TABS[number]

/** Every reported or unsupported sensor stays reachable from exactly one detail tab (Overview is a summary). */
export function sensorsForTab(device: DeviceDetail, tab: DeviceTab) {
  if(tab==='Overview') return device.sensors.filter(s=>OVERVIEW_SENSORS.includes(s.id))
  if(tab==='Performance') return device.sensors.filter(s=>PERFORMANCE_SENSORS.includes(s.id))
  if(tab==='Storage') return device.sensors.filter(s=>STORAGE_SENSORS.includes(s.id))
  if(tab==='Network') return device.sensors.filter(s=>!PERFORMANCE_SENSORS.includes(s.id)&&!STORAGE_SENSORS.includes(s.id))
  return []
}

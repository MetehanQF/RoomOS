/* Phase 7L — Dashboard 2.0 widget catalog and per-device layout.

   The catalog is fixed and code-owned: a stored layout can only reorder, resize or hide known
   widgets, never add arbitrary ones. Layout is presentation only and never carries sensor state.
   It is stored in this browser profile under the authenticated RoomOS device id, so two paired
   devices sharing a browser never see each other's layout, and Panel Mode keeps its own storage. */

export const WIDGET_IDS = ['system_overview', 'lights', 'alerts', 'devices', 'services', 'performance', 'routines'] as const
export type WidgetId = typeof WIDGET_IDS[number]
export const WIDGET_SIZES = ['sm', 'md', 'lg', 'xl'] as const
export type WidgetSize = typeof WIDGET_SIZES[number]

export type WidgetDefinition = {
  id: WidgetId
  title: string
  description: string
  sizes: readonly WidgetSize[]
  defaultSize: WidgetSize
  /** Any one of these scopes makes the widget available; empty = any authenticated device. */
  scopes: readonly string[]
  /** Pinned widgets cannot be hidden: they keep alerts and health one glance away. */
  pinned: boolean
}

export const WIDGETS: Record<WidgetId, WidgetDefinition> = {
  system_overview: { id: 'system_overview', title: 'System overview', description: 'Health, devices, alerts and DNS at a glance', sizes: ['lg', 'xl'], defaultSize: 'xl', scopes: [], pinned: true },
  lights: { id: 'lights', title: 'Lights', description: 'Room lights as one group and the desk strip', sizes: ['md', 'lg'], defaultSize: 'md', scopes: ['lights:read'], pinned: false },
  alerts: { id: 'alerts', title: 'Alerts', description: 'Active alerts by severity', sizes: ['sm', 'md', 'lg'], defaultSize: 'md', scopes: ['activity:read'], pinned: false },
  devices: { id: 'devices', title: 'Devices', description: 'Primary, Secondary, Workstation and phones', sizes: ['md', 'lg', 'xl'], defaultSize: 'lg', scopes: ['devices:read', 'system:read'], pinned: false },
  services: { id: 'services', title: 'Services & DNS', description: 'Home Assistant, DNS resolvers and services', sizes: ['sm', 'md'], defaultSize: 'sm', scopes: ['system:read'], pinned: false },
  performance: { id: 'performance', title: 'Performance', description: 'Last hour of CPU, RAM or disk', sizes: ['md', 'lg', 'xl'], defaultSize: 'md', scopes: ['devices:read'], pinned: false },
  routines: { id: 'routines', title: 'Scenes & routines', description: 'Quick scenes and favourite routines', sizes: ['md', 'lg'], defaultSize: 'md', scopes: ['lights:read', 'routines:read'], pinned: false },
}

export type WidgetPlacement = { id: WidgetId; size: WidgetSize; visible: boolean }
export type PerformanceChoice = { device: string; metric: 'cpu_usage' | 'ram_usage' | 'disk_usage' }
export type DashboardLayout = {
  version: 1
  widgets: WidgetPlacement[]
  /** Routine ids pinned to the top of the routines widget (presentation only). */
  favorites: string[]
  performance: PerformanceChoice
}

export const PERFORMANCE_METRICS = ['cpu_usage', 'ram_usage', 'disk_usage'] as const
const MAX_FAVORITES = 12

export function defaultLayout(): DashboardLayout {
  return {
    version: 1,
    widgets: WIDGET_IDS.map((id) => ({ id, size: WIDGETS[id].defaultSize, visible: true })),
    favorites: [],
    performance: { device: 'primary', metric: 'cpu_usage' },
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)

/**
 * Accepts anything (old versions, hand-edited or corrupt storage) and returns a valid layout:
 * unknown widgets are dropped, duplicates keep their first position, sizes a widget does not
 * support fall back to its default, missing (newer) widgets are appended visible, and pinned
 * widgets are always visible. Never throws.
 */
export function normalizeLayout(value: unknown): DashboardLayout {
  const fallback = defaultLayout()
  if (!isRecord(value) || value.version !== 1 || !Array.isArray(value.widgets)) return fallback
  const seen = new Set<WidgetId>()
  const widgets: WidgetPlacement[] = []
  for (const item of value.widgets) {
    if (!isRecord(item) || typeof item.id !== 'string' || !(WIDGET_IDS as readonly string[]).includes(item.id)) continue
    const id = item.id as WidgetId
    if (seen.has(id)) continue
    seen.add(id)
    const definition = WIDGETS[id]
    const size = (definition.sizes as readonly unknown[]).includes(item.size) ? item.size as WidgetSize : definition.defaultSize
    widgets.push({ id, size, visible: definition.pinned || item.visible !== false })
  }
  for (const id of WIDGET_IDS) if (!seen.has(id)) widgets.push({ id, size: WIDGETS[id].defaultSize, visible: true })
  const favorites = Array.isArray(value.favorites)
    ? [...new Set(value.favorites.filter((id): id is string => typeof id === 'string' && /^[A-Za-z0-9_-]{1,80}$/.test(id)))].slice(0, MAX_FAVORITES)
    : []
  const performance = isRecord(value.performance) && typeof value.performance.device === 'string' && /^[a-z0-9_]{1,40}$/.test(value.performance.device)
    && (PERFORMANCE_METRICS as readonly unknown[]).includes(value.performance.metric)
    ? { device: value.performance.device, metric: value.performance.metric as PerformanceChoice['metric'] }
    : fallback.performance
  return { version: 1, widgets, favorites, performance }
}

export function storageKey(deviceId: string) {
  return `roomos:dashboard-layout:v1:${deviceId}`
}

type Storage = Pick<globalThis.Storage, 'getItem' | 'setItem' | 'removeItem'>

export function readLayout(storage: Storage | null, deviceId: string | null): DashboardLayout {
  if (!storage || !deviceId) return defaultLayout()
  try {
    const raw = storage.getItem(storageKey(deviceId))
    return raw ? normalizeLayout(JSON.parse(raw)) : defaultLayout()
  } catch {
    return defaultLayout()
  }
}

/** Returns false when the browser refuses (private mode, quota): the caller must say so. */
export function writeLayout(storage: Storage | null, deviceId: string | null, layout: DashboardLayout): boolean {
  if (!storage || !deviceId) return false
  try {
    storage.setItem(storageKey(deviceId), JSON.stringify(normalizeLayout(layout)))
    return true
  } catch {
    return false
  }
}

export function widgetAllowed(id: WidgetId, scopes: readonly string[]) {
  const required = WIDGETS[id].scopes
  return required.length === 0 || required.some((scope) => scopes.includes(scope))
}

export function moveWidget(layout: DashboardLayout, id: WidgetId, toIndex: number): DashboardLayout {
  const from = layout.widgets.findIndex((item) => item.id === id)
  if (from < 0) return layout
  const target = Math.max(0, Math.min(layout.widgets.length - 1, toIndex))
  if (target === from) return layout
  const widgets = layout.widgets.slice()
  const [item] = widgets.splice(from, 1)
  widgets.splice(target, 0, item)
  return { ...layout, widgets }
}

export function resizeWidget(layout: DashboardLayout, id: WidgetId, size: WidgetSize): DashboardLayout {
  if (!WIDGETS[id].sizes.includes(size)) return layout
  return { ...layout, widgets: layout.widgets.map((item) => item.id === id ? { ...item, size } : item) }
}

export function setWidgetVisible(layout: DashboardLayout, id: WidgetId, visible: boolean): DashboardLayout {
  if (WIDGETS[id].pinned && !visible) return layout
  return { ...layout, widgets: layout.widgets.map((item) => item.id === id ? { ...item, visible } : item) }
}

export function toggleFavorite(layout: DashboardLayout, routineId: string): DashboardLayout {
  const has = layout.favorites.includes(routineId)
  if (!has && layout.favorites.length >= MAX_FAVORITES) return layout
  return { ...layout, favorites: has ? layout.favorites.filter((id) => id !== routineId) : [...layout.favorites, routineId] }
}

export function sameLayout(a: DashboardLayout, b: DashboardLayout) {
  return JSON.stringify(a) === JSON.stringify(b)
}

export const SIZE_LABEL: Record<WidgetSize, string> = { sm: 'Small', md: 'Medium', lg: 'Large', xl: 'Full width' }

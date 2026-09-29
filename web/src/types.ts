export type Capabilities = {
  power: boolean
  brightness: boolean
  color: boolean
  temperature: boolean
  effects: boolean
  min_kelvin: number
  max_kelvin: number
}

export type Light = {
  id: string
  name: string
  type: 'light'
  available: boolean
  on: boolean | null
  brightness: number | null
  color_mode: string | null
  rgb: [number, number, number] | null
  hs: [number, number] | null
  color_temperature_kelvin: number | null
  effect: string | null
  effects: string[]
  capabilities: Capabilities
  last_updated: string | null
}

export type Metric = {
  id: string
  value: string | number | boolean | null
  unit: string | null
  available: boolean
  stale: boolean
  last_updated: string | null
}

export type SystemNode = {
  id: string
  name: string
  online: boolean | null
  metrics: Record<string, Metric>
}

export type ServiceStatus = {
  id: string
  status: 'online' | 'offline' | 'unknown'
  source: string
}

/** Nodes are keyed by the logical node IDs declared in Core's device registry. */
export type SystemData = {
  nodes: Record<string, SystemNode>
  services: ServiceStatus[]
}

export type Health = {
  status: string
  home_assistant: 'connected' | 'disconnected' | 'not_configured'
  live_connection_enabled: boolean
  control_enabled: boolean
  control_policy: 'read_only' | 'production' | 'scoped' | string
  writable_device_ids: string[]
  writable_actions: string[]
  writable_permissions: Record<string, Array<'power' | 'brightness' | 'color' | 'temperature' | 'effect'>>
}

export type GroupCommandResult = {
  accepted: boolean
  device_id: 'all_room_lights'
  succeeded: string[]
  failed: { device_id: string; reason: 'unavailable' | 'service_failed' }[]
}

export type RoomOSEvent = {
  type: 'light.state_changed' | 'system.metric_updated' | 'system.node_online' | 'system.node_offline' | 'roomos.connection'
  timestamp: string
  device_id: string | null
  data: {
    light?: Light
    home_assistant?: 'connecting' | 'connected' | 'disconnected'
    [key: string]: unknown
  }
}

export type RealtimeStatus = 'connecting' | 'connected' | 'disconnected'

export type CommandFeedback = {
  tone: 'pending' | 'success' | 'partial' | 'error'
  title: string
  detail?: string
}

export type AuthDevice = {
  id: string
  friendly_name: string
  scopes: string[]
}

export type AuthStatus = {
  authenticated: boolean
  device: AuthDevice | null
}

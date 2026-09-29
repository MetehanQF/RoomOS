import { describe, expect, it } from 'vitest'
import { friendlyApiMessage } from './api'
import {
  applyLightEvent,
  brightnessToPercent,
  controlAccess,
  describeGroupResult,
  groupControlEnabled,
  kelvinPosition,
  percentToBrightness,
  expandedLightSections,
  isCurrentColor,
  lightVisualState,
  serviceName,
} from './ui'
import type { Health, Light, RoomOSEvent } from './types'

const capabilities = {
  power: true,
  brightness: true,
  color: true,
  temperature: true,
  effects: true,
  min_kelvin: 2500,
  max_kelvin: 6500,
}

function light(overrides: Partial<Light> = {}): Light {
  return {
    id: 'living_room',
    name: 'Living Room',
    type: 'light',
    available: true,
    on: false,
    brightness: 128,
    color_mode: 'color_temp',
    rgb: null,
    hs: null,
    color_temperature_kelvin: 4000,
    effect: null,
    effects: ['Off', 'Aurora'],
    capabilities,
    last_updated: '2026-09-27T12:00:00Z',
    ...overrides,
  }
}

function health(overrides: Partial<Health> = {}): Health {
  return {
    status: 'ok',
    home_assistant: 'connected',
    live_connection_enabled: true,
    control_enabled: true,
    control_policy: 'production',
    writable_device_ids: ['living_room'],
    writable_actions: ['power', 'brightness', 'color', 'temperature', 'effect'],
    writable_permissions: {
      living_room: ['power', 'brightness', 'color', 'temperature', 'effect'],
    },
    ...overrides,
  }
}

describe('capability-driven controls', () => {
  it('enables only supported and explicitly scoped actions', () => {
    const access = controlAccess(light({ capabilities: { ...capabilities, color: false } }), health())
    expect(access).toEqual({ power: true, brightness: true, color: false, temperature: true, effect: true })
  })

  it('disables all controls for unavailable devices', () => {
    expect(Object.values(controlAccess(light({ available: false }), health())).every((value) => !value)).toBe(true)
  })

  it('lets the global kill-switch override a writable scope', () => {
    expect(Object.values(controlAccess(light(), health({ control_enabled: false }))).every((value) => !value)).toBe(true)
    expect(groupControlEnabled(health({ control_enabled: false, writable_permissions: { all_room_lights: ['power'] } }))).toBe(false)
  })

  it('requires the logical group and power action for group control', () => {
    expect(groupControlEnabled(health({ writable_permissions: { all_room_lights: ['power'] } }))).toBe(true)
    expect(groupControlEnabled(health({ writable_permissions: { 'light.all_lights': ['power'] } }))).toBe(false)
  })
})

describe('human-friendly device values', () => {
  it('converts between raw brightness and percentage', () => {
    expect(brightnessToPercent(128)).toBe(50)
    expect(brightnessToPercent(255)).toBe(100)
    expect(brightnessToPercent(null)).toBe(0)
    expect(percentToBrightness(50)).toBe(128)
    expect(percentToBrightness(100)).toBe(255)
  })

  it('uses each device Kelvin range independently', () => {
    expect(kelvinPosition(6500, 2500, 6500)).toBe(100)
    expect(kelvinPosition(6500, 2500, 9000)).toBe(62)
    expect(kelvinPosition(9000, 2500, 9000)).toBe(100)
  })
})

describe('dense light card presentation', () => {
  it('keeps advanced controls collapsed while exposing only supported sections', () => {
    expect(expandedLightSections(light())).toEqual({ temperature: true, color: true, effects: true })
    expect(expandedLightSections(light({ capabilities: { ...capabilities, temperature: false, effects: false }, effects: [] }))).toEqual({ temperature: false, color: true, effects: false })
  })

  it('maps availability and power to unmistakable visual states', () => {
    expect(lightVisualState(light({ available: false }))).toBe('unavailable')
    expect(lightVisualState(light({ on: false }))).toBe('off')
    expect(lightVisualState(light({ on: true }))).toBe('on')
  })

  it('marks only a matching live RGB preset as selected', () => {
    expect(isCurrentColor([128, 0, 255], [128, 0, 255])).toBe(true)
    expect(isCurrentColor([130, 2, 252], [128, 0, 255])).toBe(true)
    expect(isCurrentColor([0, 220, 255], [128, 0, 255])).toBe(false)
  })

  it('keeps DNS capitalization product-consistent', () => {
    expect(serviceName('dns')).toBe('DNS')
    expect(serviceName('home_assistant')).toBe('home assistant')
  })
})

describe('command and realtime state UX', () => {
  it('keeps backend exception details out of user-facing errors', () => {
    expect(friendlyApiMessage(500)).toBe('RoomOS Core is temporarily unavailable.')
    expect(friendlyApiMessage(404)).toBe('That RoomOS device is not available.')
    expect(friendlyApiMessage(418)).toBe('The command could not be completed.')
  })

  it('reports a partial group response with the failed friendly lights', () => {
    const feedback = describeGroupResult(
      {
        accepted: false,
        device_id: 'all_room_lights',
        succeeded: ['living_room', 'bedroom', 'kitchen', 'hallway'],
        failed: [{ device_id: 'desk_ambient', reason: 'service_failed' }],
      },
      { desk_ambient: 'Desk Ambient' },
    )
    expect(feedback.tone).toBe('partial')
    expect(feedback.detail).toBe('Desk Ambient')
  })

  it('falls back to the logical ID when Core reported no name for it', () => {
    const feedback = describeGroupResult({
      accepted: false,
      device_id: 'all_room_lights',
      succeeded: ['living_room'],
      failed: [{ device_id: 'desk_ambient', reason: 'unavailable' }],
    })
    expect(feedback.detail).toBe('desk_ambient')
  })

  it('applies an allowed realtime light update without a page refresh', () => {
    const updated = light({ on: true, brightness: 204 })
    const event: RoomOSEvent = {
      type: 'light.state_changed',
      timestamp: '2026-09-27T12:01:00Z',
      device_id: 'living_room',
      data: { light: updated },
    }
    expect(applyLightEvent([light(), light({ id: 'bedroom' })], event)).toEqual([updated, light({ id: 'bedroom' })])
  })

  it('ignores unrelated realtime events', () => {
    const current = [light()]
    const event: RoomOSEvent = {
      type: 'system.metric_updated',
      timestamp: '2026-09-27T12:01:00Z',
      device_id: 'primary',
      data: {},
    }
    expect(applyLightEvent(current, event)).toBe(current)
  })
})

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { ROOMOS_EVENT_TYPES } from './types'

// The schema is generated from Core's EventType; Core tests prove it matches what Core emits.
// JavaScript like nginx-config.test.js: it reads repository files outside web/ and is not
// part of the TypeScript/Docker build.
const schema = JSON.parse(readFileSync('../shared/api/event.schema.json', 'utf8'))

describe('event contract parity', () => {
  it('Web event union equals the shared schema enum, in order', () => {
    expect([...ROOMOS_EVENT_TYPES]).toEqual(schema.properties.type.enum)
  })

  it('has no duplicate event types', () => {
    expect(new Set(ROOMOS_EVENT_TYPES).size).toBe(ROOMOS_EVENT_TYPES.length)
  })
})

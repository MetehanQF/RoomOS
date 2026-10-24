import { describe, expect, it } from 'vitest'
import { roomosEventUrl } from './hooks'

describe('RoomOS event URL', () => {
  it('uses WSS on the trusted HTTPS origin', () => {
    expect(roomosEventUrl({ protocol: 'https:', host: 'roomos.example.com' } as Location))
      .toBe('wss://roomos.example.com/api/v1/events')
  })

  it('keeps WS only for the legacy HTTP redirect origin', () => {
    expect(roomosEventUrl({ protocol: 'http:', host: '192.0.2.20:5500' } as Location))
      .toBe('ws://192.0.2.20:5500/api/v1/events')
  })
})

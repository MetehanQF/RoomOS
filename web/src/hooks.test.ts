import { describe, expect, it } from 'vitest'
import { roomosEventUrl } from './hooks'

describe('RoomOS event URL', () => {
  it('uses WSS on a trusted HTTPS origin', () => {
    expect(roomosEventUrl({ protocol: 'https:', host: 'roomos.example.com' } as Location))
      .toBe('wss://roomos.example.com/api/v1/events')
  })

  it('keeps WS only for a plain HTTP origin', () => {
    expect(roomosEventUrl({ protocol: 'http:', host: 'roomos.example.com:5500' } as Location))
      .toBe('ws://roomos.example.com:5500/api/v1/events')
  })
})

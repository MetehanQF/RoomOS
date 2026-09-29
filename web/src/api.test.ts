import { describe, expect, it } from 'vitest'
import { ApiError, csrfToken, parseSystemData } from './api'

const node = (id: string) => ({
  id,
  name: id === 'primary' ? 'Primary Node' : 'Secondary Node',
  online: true,
  metrics: {},
})

describe('system response contract', () => {
  it('accepts a registry-driven nodes response', () => {
    const payload = {
      nodes: { primary: node('primary'), secondary: node('secondary') },
      services: [],
    }
    expect(parseSystemData(payload)).toBe(payload)
  })

  it('accepts any node IDs the registry declares', () => {
    const payload = { nodes: { workshop: node('workshop') }, services: [] }
    expect(parseSystemData(payload)).toBe(payload)
  })

  it('rejects a response without a nodes map before React render', () => {
    expect(() => parseSystemData({ primary: node('primary'), services: [] }))
      .toThrowError(new ApiError(502, 'RoomOS Core returned an incompatible system response.'))
  })

  it('rejects a node that is missing its metrics', () => {
    expect(() => parseSystemData({ nodes: { primary: { id: 'primary' } }, services: [] }))
      .toThrowError(new ApiError(502, 'RoomOS Core returned an incompatible system response.'))
  })
})

describe('RoomOS session request helpers', () => {
  it('reads only the CSRF cookie value used for unsafe requests', () => {
    expect(csrfToken('other=x; roomos_csrf=safe-token; theme=dark')).toBe('safe-token')
    expect(csrfToken('other=x')).toBeNull()
  })
})

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

describe('RoomOS WebSocket proxy boundaries', () => {
  const config = readFileSync('../docker/roomos-common.inc', 'utf8')

  it.each(['/api/v1/events', '/api/v1/native/events'])('proxies %s with an explicit upgrade boundary', (path) => {
    const escaped = path.replaceAll('/', '\\/')
    const location = config.match(new RegExp(`location (?:= )?${escaped} \\{([\\s\\S]*?)\\n\\}`))?.[1]
    expect(location).toContain('proxy_pass http://core:5501;')
    expect(location).toContain('proxy_http_version 1.1;')
    expect(location).toContain('proxy_set_header Upgrade $http_upgrade;')
    expect(location).toContain('proxy_set_header Connection "upgrade";')
    expect(location).toContain('proxy_set_header X-Forwarded-Proto $scheme;')
  })
})

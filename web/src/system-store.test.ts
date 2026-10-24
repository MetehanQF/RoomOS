import { describe, expect, it, vi } from 'vitest'
import { SystemStore } from './system-store'
import type { DnsClusterStatus, RoomOSEvent, SystemData } from './types'

const ev = (type: RoomOSEvent['type'], data: Record<string, unknown>, device_id: string | null = null) => ({ type, timestamp: '2026-10-18T10:00:00Z', device_id, data }) as RoomOSEvent

const resolver = (role: 'primary' | 'secondary', status: 'healthy' | 'offline') => ({ id: role, role, status, last_checked: '2026-10-18T10:00:00Z', response_time_ms: 3 })
const data = (status: DnsClusterStatus['status'] = 'healthy'): SystemData => ({
  primary: { id: 'primary', name: 'Primary', online: true, metrics: {} }, secondary: { id: 'secondary', name: 'Secondary', online: true, metrics: {} }, services: [],
  dns_cluster: { status, primary: resolver('primary', 'healthy'), secondary: resolver('secondary', status === 'healthy' ? 'healthy' : 'offline'), last_checked: '2026-10-18T10:00:00Z' },
})

describe('SystemStore', () => {
  it('loads once per connection and trusts only data loaded while connected', async () => {
    const load = vi.fn(async () => data())
    const store = new SystemStore(load, true, 'connected')
    await Promise.all([store.load(), store.load()])
    expect(load).toHaveBeenCalledTimes(1)
    expect(store.getSnapshot()).toMatchObject({ status: 'ready', trusted: true })
    store.setConnection('reconnecting')
    expect(store.getSnapshot().trusted).toBe(false)
  })

  it('applies full snapshot events without a request and ignores malformed ones', async () => {
    const load = vi.fn(async () => data())
    const store = new SystemStore(load, true, 'connected')
    await store.load()
    store.handleEvent(ev('system.snapshot_updated', { system: data('degraded') }))
    expect(store.getSnapshot().data?.dns_cluster.status).toBe('degraded')
    store.handleEvent(ev('system.snapshot_updated', { system: { nope: true } }))
    expect(store.getSnapshot().data?.dns_cluster.status).toBe('degraded')
    expect(load).toHaveBeenCalledTimes(1)
  })

  it('re-reads once only when a DNS probe changes the cluster state', async () => {
    const load = vi.fn(async () => data())
    const store = new SystemStore(load, true, 'connected')
    await store.load()
    for (let index = 0; index < 5; index += 1) store.handleEvent(ev('system.service_updated', { service: 'dns', status: 'healthy' }, 'dns'))
    expect(load).toHaveBeenCalledTimes(1)
    load.mockResolvedValueOnce(data('degraded'))
    store.handleEvent(ev('system.service_updated', { service: 'dns', status: 'degraded' }, 'dns'))
    store.handleEvent(ev('system.service_updated', { service: 'dns', status: 'degraded' }, 'dns'))
    await vi.waitFor(() => expect(store.getSnapshot().data?.dns_cluster.status).toBe('degraded'))
    expect(load).toHaveBeenCalledTimes(2)
  })

  it('never requests without system:read and becomes forbidden on 403', async () => {
    const load = vi.fn(async () => data())
    const blocked = new SystemStore(load, false, 'connected')
    await blocked.load()
    expect(load).not.toHaveBeenCalled()
    expect(blocked.getSnapshot().status).toBe('forbidden')
    const denied = new SystemStore(vi.fn(async () => { throw Object.assign(new Error('no'), { status: 403 }) }), true, 'connected')
    await denied.load()
    expect(denied.getSnapshot()).toMatchObject({ status: 'forbidden', data: null })
  })

  it('keeps last known data on a failed refresh, marked untrusted', async () => {
    const load = vi.fn(async () => data())
    const store = new SystemStore(load, true, 'connected')
    await store.load()
    load.mockRejectedValueOnce(new Error('down'))
    await store.load()
    expect(store.getSnapshot()).toMatchObject({ status: 'error', trusted: false })
    expect(store.getSnapshot().data).not.toBeNull()
  })
})

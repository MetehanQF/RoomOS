import { describe, expect, it, vi } from 'vitest'
import { SharedRequests } from './shared-requests'

const deferred = <T,>() => {
  let resolve!: (value: T) => void, reject!: (error: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

describe('SharedRequests', () => {
  it('concurrent callers in one generation share a single request', async () => {
    const pending = deferred<string[]>()
    const fetcher = vi.fn(() => pending.promise)
    const shared = new SharedRequests()
    const load = shared.share('lights', fetcher)
    const a = load(); const b = load()
    expect(fetcher).toHaveBeenCalledTimes(1)
    pending.resolve(['room_main'])
    await expect(a).resolves.toEqual(['room_main'])
    await expect(b).resolves.toEqual(['room_main'])
  })

  it('a later call after the request settled fetches fresh data', async () => {
    const fetcher = vi.fn(async () => 'value')
    const load = new SharedRequests().share('health', fetcher)
    await load(); await load()
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('a failure reaches every waiting caller and the next call retries', async () => {
    const pending = deferred<string>()
    const fetcher = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValueOnce('recovered')
    const load = new SharedRequests().share('health', fetcher)
    const a = load(); const b = load()
    pending.reject(new Error('503'))
    await expect(a).rejects.toThrow('503')
    await expect(b).rejects.toThrow('503')
    await expect(load()).resolves.toBe('recovered')
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('a new connection generation never joins a request started before it', async () => {
    const before = deferred<string>()
    const fetcher = vi.fn().mockResolvedValue('later').mockReturnValueOnce(before.promise).mockResolvedValueOnce('after reconnect')
    const shared = new SharedRequests()
    const load = shared.share('lights', fetcher)
    const stale = load()
    shared.invalidate()
    await expect(load()).resolves.toBe('after reconnect')
    expect(fetcher).toHaveBeenCalledTimes(2)
    before.resolve('before disconnect')
    await expect(stale).resolves.toBe('before disconnect')
    // The settled stale request does not evict the newer flight bookkeeping.
    await load()
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('keys are independent', () => {
    const shared = new SharedRequests()
    const lights = vi.fn(() => new Promise(() => undefined)); const health = vi.fn(() => new Promise(() => undefined))
    void shared.share('lights', lights)(); void shared.share('health', health)()
    expect(lights).toHaveBeenCalledTimes(1); expect(health).toHaveBeenCalledTimes(1)
  })
})

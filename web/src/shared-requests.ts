/**
 * Single-flight request sharing for a page that owns several stores reading the same resource
 * (Panel: HomeStore and LightsStore both bootstrap /lights and /health on connect).
 *
 * Callers in the same connection generation share one in-flight request. Once it settles the
 * next call fetches again, so a failure is retried by the next load and nothing is cached.
 * `invalidate()` starts a new generation on every connection change: a load after a
 * reconnect never joins a request issued before the disconnect, so stale data cannot be
 * re-trusted. Each store keeps its own epoch checks on top of this.
 */
export class SharedRequests {
  private generation = 0
  private flights = new Map<string, { generation: number; promise: Promise<unknown> }>()

  invalidate() {
    this.generation += 1
  }

  share<T>(key: string, fetcher: () => Promise<T>): () => Promise<T> {
    return () => {
      const current = this.flights.get(key)
      if (current && current.generation === this.generation) return current.promise as Promise<T>
      const promise: Promise<T> = fetcher().finally(() => {
        if (this.flights.get(key)?.promise === promise) this.flights.delete(key)
      })
      this.flights.set(key, { generation: this.generation, promise })
      return promise
    }
  }
}

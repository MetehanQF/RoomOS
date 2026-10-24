import { afterEach, describe, expect, it, vi } from 'vitest'
import { IdleController } from './idle'

afterEach(() => vi.useRealTimers())

describe('tablet idle visual mode', () => {
  it('enters after inactivity and wakes on the first interaction', () => {
    vi.useFakeTimers()
    const changes: boolean[] = []
    const controller = new IdleController((idle) => changes.push(idle), 1_000)
    controller.start()
    vi.advanceTimersByTime(1_000)
    expect(changes).toEqual([true])
    controller.activity()
    expect(changes).toEqual([true, false])
    controller.stop()
  })
})

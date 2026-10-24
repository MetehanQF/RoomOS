import { describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { HomeStore, type HomeApi } from './home-store'
import { fixture, scopes as homeScopes } from './home-fixtures.test-support'
import { automationCatalog, catalogOf, evening, smartAutomation, userRoutine } from './routines-fixtures.test-support'
import type { RoutineRunResponse } from './types'

const WRITE = [...homeScopes, 'routines:write']
const ran = (status: 'success' | 'partial' | 'failed'): RoutineRunResponse => ({
  routine: { ...userRoutine, last_run: '2026-10-18T10:00:00Z', last_result: { execution_id: 'ex_1', status, scene_status: status, source: 'manual', detail: null } },
  scene: { scene: 'night', name: 'Night', status, applied: [], skipped: [], failed: [], applied_at: '2026-10-18T10:00:00Z' },
})

async function loaded(access: readonly string[], runRoutine?: HomeApi['runRoutine']) {
  const { api } = fixture({ routines: vi.fn(async () => catalogOf([evening, userRoutine])), runRoutine }, access)
  const store = new HomeStore(api, access, true, 'connected')
  await store.load()
  return { api, store }
}

describe('Dashboard quick routine run (HomeStore)', () => {
  it('loads smart rules once and applies automation events locally', async () => {
    const automations = vi.fn(async () => automationCatalog())
    const { api } = fixture({ routines: vi.fn(async () => catalogOf([evening])), automations } as Partial<HomeApi>, WRITE)
    const store = new HomeStore(api, WRITE, true, 'connected')
    await store.load()
    store.handleEvent({ type: 'automation.changed', timestamp: '2026-10-19T11:00:00Z', device_id: null, data: { automation_id: smartAutomation.id, automation: { ...smartAutomation, enabled: true, updated_at: '2026-10-19T11:00:00Z' } } })
    expect(store.getSnapshot().resources.automations.data?.automations[0].enabled).toBe(true)
    expect(automations).toHaveBeenCalledTimes(1)
  })
  it('uses the Routines page guard: hidden without routines:write, managed routines never runnable', async () => {
    const run = vi.fn(async () => ran('success'))
    const { store } = await loaded(homeScopes, run)
    expect(store.routineGuard(userRoutine).visible).toBe(false)
    expect(await store.runRoutine(userRoutine.id)).toBe(false)
    const writer = await loaded(WRITE, run)
    expect(writer.store.routineGuard(evening).visible).toBe(false)
    expect(await writer.store.runRoutine(evening.id)).toBe(false)
    expect(run).not.toHaveBeenCalled()
  })

  it('runs once, blocks a second run while pending and applies the returned routine', async () => {
    let resolve!: (value: RoutineRunResponse) => void
    const run = vi.fn(() => new Promise<RoutineRunResponse>((done) => { resolve = done }))
    const { store } = await loaded(WRITE, run)
    expect(store.routineGuard(userRoutine)).toMatchObject({ visible: true, ok: true })
    const first = store.runRoutine(userRoutine.id)
    expect(await store.runRoutine(userRoutine.id)).toBe(false)
    expect(store.getSnapshot().pending).toEqual({ kind: 'routine', id: userRoutine.id })
    resolve(ran('partial'))
    expect(await first).toBe(true)
    expect(run).toHaveBeenCalledTimes(1)
    expect(store.getSnapshot().feedback).toMatchObject({ tone: 'partial', title: 'Weeknight partly applied' })
    expect(store.getSnapshot().resources.routines.data?.routines.find((item) => item.id === userRoutine.id)?.last_result?.status).toBe('partial')
  })

  it('shows the real server rejection, never a fake success', async () => {
    const { store } = await loaded(WRITE, vi.fn(async () => { throw new ApiError(429, 'busy') }))
    await store.runRoutine(userRoutine.id)
    expect(store.getSnapshot().feedback).toMatchObject({ tone: 'error', title: 'Weeknight could not run', detail: 'This routine is already running.' })
    expect(store.getSnapshot().pending).toBeNull()
  })

  it('pauses while reconnecting', async () => {
    const run = vi.fn(async () => ran('success'))
    const { store } = await loaded(WRITE, run)
    store.setConnection('reconnecting')
    expect(store.routineGuard(userRoutine)).toMatchObject({ visible: true, ok: false })
    expect(await store.runRoutine(userRoutine.id)).toBe(false)
    expect(run).not.toHaveBeenCalled()
  })
})

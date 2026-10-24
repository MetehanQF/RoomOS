import { describe, expect, it } from 'vitest'
import { ALL_SCOPES, automationHistory, routinesFixture, simulation, smartAutomation } from './routines-fixtures.test-support'
import type { RoomOSEvent } from './types'

const event = (type: RoomOSEvent['type'], data: RoomOSEvent['data']): RoomOSEvent => ({ type, data, device_id: null, timestamp: '2026-10-19T10:00:00Z' })

describe('Smart Automations store', () => {
  it('bootstraps catalog and first history page once inside the existing single-flight load', async () => {
    const { api, store } = routinesFixture()
    const first = store.load(), second = store.load()
    expect(first).toBe(second)
    await first
    expect(api.automations).toHaveBeenCalledTimes(1)
    expect(api.automationHistory).toHaveBeenCalledWith(50, null)
    expect(store.getSnapshot()).toMatchObject({ automationsStatus: 'ready', automationHistoryStatus: 'ready', automationHistoryCursor: '1' })
    expect(store.getSnapshot().automations?.automations[0].name).toBe('Workstation heat guard')
  })

  it('applies changed, executed and deleted events locally with zero REST refresh', async () => {
    const { api, store } = routinesFixture(); await store.load()
    const changed = { ...smartAutomation, enabled: true, updated_at: '2026-10-19T10:00:00Z' }
    store.handleEvent(event('automation.changed', { automation_id: changed.id, automation: changed }))
    store.handleEvent(event('automation.executed', { automation_id: changed.id, automation: { ...changed, last_triggered: '2026-10-19T10:00:00Z', last_outcome: 'success' }, result: { ...simulation, mode: 'LIVE', outcome: 'success' } }))
    expect(store.getSnapshot().automations?.automations[0]).toMatchObject({ enabled: true, last_outcome: 'success' })
    expect(store.getSnapshot().automationHistory[0]).toMatchObject({ automation_id: changed.id, outcome: 'success' })
    store.handleEvent(event('automation.deleted', { automation_id: changed.id }))
    expect(store.getSnapshot().automations?.automations).toHaveLength(0)
    expect(api.automations).toHaveBeenCalledTimes(1)
    expect(api.automationHistory).toHaveBeenCalledTimes(1)
  })

  it('paginates history once per cursor and appends without duplication', async () => {
    const { api, store } = routinesFixture(); await store.load()
    api.automationHistory.mockResolvedValueOnce({ ...automationHistory(null), items: [{ ...automationHistory(null).items[0], correlation_id: `ac_${'9'.repeat(32)}` }] })
    expect(await store.loadMoreAutomationHistory()).toBe(true)
    expect(await store.loadMoreAutomationHistory()).toBe(false)
    expect(store.getSnapshot().automationHistory).toHaveLength(2)
    expect(api.automationHistory).toHaveBeenLastCalledWith(50, '1')
  })

  it('simulates once without writing history or sending a live action', async () => {
    const { api, store } = routinesFixture({ scopes: ALL_SCOPES }); await store.load()
    expect(await store.simulateAutomation(smartAutomation)).toBe(true)
    expect(api.simulateAutomation).toHaveBeenCalledTimes(1)
    expect(api.runAutomation).not.toHaveBeenCalled()
    expect(store.getSnapshot().automationSimulation?.mode).toBe('SIMULATION')
    expect(store.getSnapshot().automationHistory).toHaveLength(1)
  })

  it('creates disabled, updates without an enabled field, toggles through the dedicated endpoint, and deletes', async () => {
    const { api, store } = routinesFixture({ scopes: ALL_SCOPES }); await store.load()
    api.createAutomation.mockResolvedValueOnce(smartAutomation)
    const draft = { ...smartAutomation }
    expect(await store.saveAutomation(draft, null)).toBe(true)
    expect(api.createAutomation.mock.calls[0][0]).toMatchObject({ enabled: false })
    api.updateAutomation.mockResolvedValueOnce({ ...smartAutomation, name: 'Renamed', updated_at: '2026-10-19T11:00:00Z' })
    expect(await store.saveAutomation({ ...draft, name: 'Renamed', enabled: true }, smartAutomation)).toBe(true)
    expect(api.updateAutomation.mock.calls[0][1]).not.toHaveProperty('enabled')
    api.enableAutomation.mockResolvedValueOnce({ ...smartAutomation, enabled: true, updated_at: '2026-10-19T12:00:00Z' })
    expect(await store.toggleAutomation(smartAutomation)).toBe(true)
    expect(api.enableAutomation).toHaveBeenCalledWith(smartAutomation.id, true)
    api.deleteAutomation.mockResolvedValueOnce(undefined)
    expect(await store.deleteAutomation({ ...smartAutomation, enabled: true, updated_at: '2026-10-19T12:00:00Z' })).toBe(true)
    expect(store.getSnapshot().automations?.automations).toHaveLength(0)
  })

  it('fails closed for live enable/run while allowing a dry simulation with routines:write', async () => {
    const scopes = ALL_SCOPES.filter((scope) => scope !== 'lights:write')
    const { api, store } = routinesFixture({ scopes }); await store.load()
    expect(store.automationGuard(smartAutomation, 'simulate')).toMatchObject({ visible: true, ok: true })
    expect(store.automationGuard(smartAutomation, 'toggle')).toMatchObject({ visible: true, ok: false })
    expect(store.automationGuard(smartAutomation, 'run').visible).toBe(false)
    expect(await store.toggleAutomation(smartAutomation)).toBe(false)
    expect(await store.runAutomation(smartAutomation)).toBe(false)
    expect(api.enableAutomation).not.toHaveBeenCalled()
    expect(api.runAutomation).not.toHaveBeenCalled()
  })
})

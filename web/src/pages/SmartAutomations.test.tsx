// @vitest-environment jsdom

import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { automationCatalog, automationHistory, catalogOf, routinesFixture, simulation, smartAutomation } from '../routines-fixtures.test-support'
import { RoutinesView, SmartAutomationBuilder, SmartAutomationCard, SmartAutomationHistory } from './Routines'
import type { AutomationDraft } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let root: Root
let container: HTMLDivElement
beforeEach(() => { container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container) })
afterEach(async () => { await act(async () => root.unmount()); container.remove() })
const click = async (label: string) => {
  const element = [...container.querySelectorAll('button')].find((item) => item.textContent?.trim() === label || item.getAttribute('aria-label') === label)
  expect(element, `button ${label}`).toBeTruthy()
  await act(async () => element!.dispatchEvent(new MouseEvent('click', { bubbles: true })))
}

describe('Smart Automation builder', () => {
  it('renders one keyboard-safe WHEN IF THEN rail from server-owned target catalogs', async () => {
    await act(async () => root.render(<SmartAutomationBuilder catalog={automationCatalog()} routines={catalogOf([])} controlsAvailable onSave={async () => undefined} onCancel={() => undefined} />))
    expect(container.textContent).toContain('WHEN')
    expect(container.textContent).toContain('IF')
    expect(container.textContent).toContain('THEN')
    expect(container.textContent).toContain('Workstation')
    expect(container.textContent).toContain('Desk ambient')
    expect(container.textContent).toContain('Saved disabled')
    expect(container.textContent).not.toContain('light.living_room')
    expect(container.querySelectorAll('.smart-rail-step')).toHaveLength(3)
  })

  it('adds and reorders actions, validates, trims, and saves a disabled draft', async () => {
    const save = vi.fn<(draft: AutomationDraft) => Promise<void>>(async () => undefined)
    await act(async () => root.render(<SmartAutomationBuilder catalog={automationCatalog()} routines={catalogOf([])} controlsAvailable onSave={save} onCancel={() => undefined} />))
    const name = container.querySelector<HTMLInputElement>('input[name="automation-name"]')!
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(name, '  Evening guard  '); name.dispatchEvent(new Event('input', { bubbles: true })) })
    await click('Add action')
    expect(container.querySelectorAll('.smart-action-row')).toHaveLength(2)
    await click('Move action 2 up')
    await click('Save automation')
    expect(save).toHaveBeenCalledTimes(1)
    expect(save.mock.calls[0][0]).toMatchObject({ name: 'Evening guard', enabled: false })
    expect(save.mock.calls[0][0].actions).toHaveLength(2)
  })

  it('edits bounded device thresholds, hold, cooldown, failure policy and light payloads', async () => {
    const save = vi.fn<(draft: AutomationDraft) => Promise<void>>(async () => undefined)
    await act(async () => root.render(<SmartAutomationBuilder catalog={automationCatalog()} routines={catalogOf([])} initial={smartAutomation} controlsAvailable onSave={save} onCancel={() => undefined} />))
    const change = async (name: string, value: string) => {
      const control = container.querySelector<HTMLInputElement | HTMLSelectElement>(`[name="${name}"]`)
      expect(control, name).toBeTruthy()
      await act(async () => { Object.getOwnPropertyDescriptor(control instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype, 'value')!.set!.call(control, value); control!.dispatchEvent(new Event('change', { bubbles: true })) })
    }
    await change('device-operator', 'below'); await change('device-value', '72'); await change('trigger-hold', '120')
    await change('action-1-type', 'light'); await change('action-1-operation', 'brightness'); await change('action-1-brightness', '96')
    await change('automation-cooldown', '600'); await change('failure-policy', 'continue')
    await click('Save automation')
    expect(save.mock.calls[0][0]).toMatchObject({
      trigger: { type: 'device_health', operator: 'below', value: 72, for_seconds: 120 },
      actions: [{ type: 'light', operation: 'brightness', brightness: 96, on: null, rgb: null, kelvin: null }],
      cooldown_seconds: 600, failure_policy: 'continue', enabled: false,
    })
  })

  it('edits a server-owned IF predicate instead of exposing raw entity identifiers', async () => {
    const save = vi.fn<(draft: AutomationDraft) => Promise<void>>(async () => undefined)
    await act(async () => root.render(<SmartAutomationBuilder catalog={automationCatalog()} routines={catalogOf([])} controlsAvailable onSave={save} onCancel={() => undefined} />))
    await click('Add condition')
    const source = container.querySelector<HTMLSelectElement>('[name="condition-source"]')!
    expect(source).toBeTruthy()
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(source, 'device'); source.dispatchEvent(new Event('change', { bubbles: true })) })
    expect(container.textContent).toContain('Workstation')
    expect(container.textContent).not.toContain('sensor.workstation')
    const field = container.querySelector<HTMLSelectElement>('[name="condition-field"]')!
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(field, 'temperature'); field.dispatchEvent(new Event('change', { bubbles: true })) })
    const value = container.querySelector<HTMLInputElement>('[name="condition-value"]')!
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(value, '75'); value.dispatchEvent(new Event('change', { bubbles: true })) })
    const name = container.querySelector<HTMLInputElement>('input[name="automation-name"]')!
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(name, 'Thermal guard'); name.dispatchEvent(new Event('input', { bubbles: true })) })
    await click('Save automation')
    expect(save.mock.calls[0][0].conditions).toMatchObject({ source: 'device', target_id: 'workstation', field: 'temperature', operator: 'greater_than', value: 75 })
  })
})

describe('Smart Automation explanation surfaces', () => {
  it('labels simulation as non-live and exposes no controls to a read-only device', async () => {
    await act(async () => root.render(<SmartAutomationCard automation={smartAutomation} catalog={automationCatalog()} simulation={simulation} canWrite={false} canEnable={false} live busy={false}
      onEdit={() => undefined} onToggle={() => undefined} onSimulate={() => undefined} onDelete={() => undefined} />))
    expect(container.textContent).toContain('Simulation · would run')
    expect(container.textContent).toContain('No command was sent')
    expect(container.textContent).toContain('Workstation heat guard')
    expect(container.querySelectorAll('button')).toHaveLength(0)
  })

  it('shows bounded history and requests the next cursor only from an explicit button', async () => {
    const more = vi.fn()
    await act(async () => root.render(<SmartAutomationHistory records={automationHistory().items} nextCursor="1" loading={false} onMore={more} />))
    expect(container.textContent).toContain('Required evidence was unknown')
    await click('Load older decisions')
    expect(more).toHaveBeenCalledTimes(1)
  })

  it('integrates smart rules, builder entry and history into the existing Routines surface', async () => {
    const { store } = routinesFixture(); await store.load()
    await act(async () => root.render(<MemoryRouter><RoutinesView store={store} snapshot={store.getSnapshot()} now={Date.parse('2026-10-19T10:00:00Z')} /></MemoryRouter>))
    expect(container.textContent).toContain('Smart rules')
    expect(container.textContent).toContain('Workstation heat guard')
    expect(container.textContent).toContain('Recent history')
    await click('New automation')
    expect(container.textContent).toContain('New rule')
    expect(container.textContent).toContain('WHEN')
  })
})

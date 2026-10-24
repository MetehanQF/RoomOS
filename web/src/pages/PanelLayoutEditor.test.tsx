// @vitest-environment jsdom

import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { DEFAULT_PANEL_LAYOUT } from '../panel-layout'
import { PanelLayoutEditor } from './PanelLayoutEditor'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let root: Root
let container: HTMLDivElement
beforeEach(() => { container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container) })
afterEach(async () => { await act(async () => root.unmount()); container.remove() })

async function renderEditor(overrides: Partial<Parameters<typeof PanelLayoutEditor>[0]> = {}) {
  const props = {
    layout: DEFAULT_PANEL_LAYOUT,
    onSave: vi.fn(async () => undefined),
    onReset: vi.fn(async () => undefined),
    onClose: vi.fn(),
    ...overrides,
  }
  await act(async () => root.render(<PanelLayoutEditor {...props} />))
  return props
}

const click = async (selector: string) => {
  const button = container.querySelector<HTMLButtonElement>(selector)
  expect(button).not.toBeNull()
  await act(async () => button!.click())
}

it('offers fixed card/header/scene choices with 48px action classes and a live preview', async () => {
  await renderEditor()
  expect(container.textContent).toContain('Düzeni düzenle')
  expect(container.querySelectorAll('[data-layout-card]')).toHaveLength(5)
  expect(container.querySelectorAll('[data-layout-scene]')).toHaveLength(8)
  expect(container.querySelectorAll('.panel-layout-action').length).toBeGreaterThan(20)
  expect(container.querySelector('[data-layout-preview]')?.textContent).toContain('Ana Oda Işıkları')
  expect(container.textContent).not.toContain('entity_id')
})

it('previews reorder/hide/density and saves one draft', async () => {
  const props = await renderEditor()
  await click('[aria-label="Ana Oda Işıkları aşağı taşı"]')
  await click('[aria-label="Rutinler kartını gizle"]')
  await click('[aria-label="Kompakt düzen"]')
  expect(container.querySelector('[data-layout-preview]')?.textContent?.indexOf('Ambiyans Işığı')).toBeLessThan(container.querySelector('[data-layout-preview]')!.textContent!.indexOf('Ana Oda Işıkları'))
  expect(container.querySelector('[data-layout-preview]')?.textContent).not.toContain('Rutinler')
  await click('[data-save-layout]')
  expect(props.onSave).toHaveBeenCalledTimes(1)
  expect(props.onSave).toHaveBeenCalledWith(expect.objectContaining({ density: 'compact', hidden: ['routines'], cards: ['ambient', 'main_lights', 'scenes', 'routines', 'system'] }))
  expect(props.onClose).toHaveBeenCalledTimes(1)
})

it('cancel writes nothing and a failed save leaves the editor open with guidance', async () => {
  const failed = vi.fn(async () => { throw new Error('offline') })
  const props = await renderEditor({ onSave: failed })
  await click('[data-save-layout]')
  expect(container.textContent).toContain('Kaydedilemedi')
  expect(props.onClose).not.toHaveBeenCalled()
  await click('[data-cancel-layout]')
  expect(props.onReset).not.toHaveBeenCalled()
  expect(props.onClose).toHaveBeenCalledTimes(1)
})

it('reset uses the dedicated self-only reset action', async () => {
  const props = await renderEditor({ layout: { ...DEFAULT_PANEL_LAYOUT, density: 'compact' } })
  await click('[data-reset-layout]')
  expect(props.onReset).toHaveBeenCalledTimes(1)
  expect(props.onClose).toHaveBeenCalledTimes(1)
})

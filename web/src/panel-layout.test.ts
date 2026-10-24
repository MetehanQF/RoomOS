import { describe, expect, it } from 'vitest'
import { DEFAULT_PANEL_LAYOUT, moveItem, normalizePanelLayout, toggleCard } from './panel-layout'

describe('panel layout contract', () => {
  it('accepts only the fixed logical card and scene catalogs', () => {
    const changed = {
      ...DEFAULT_PANEL_LAYOUT,
      cards: ['ambient', 'main_lights', 'scenes', 'system', 'routines'],
      hidden: ['routines'],
      density: 'compact',
      scene_order: [...DEFAULT_PANEL_LAYOUT.scene_order].reverse(),
      header: { clock: true, date: false, connection: true, health: false },
    }
    expect(normalizePanelLayout(changed)).toEqual(changed)
    for (const bad of [
      { ...changed, cards: ['light.living_room', ...changed.cards.slice(1)] },
      { ...changed, cards: ['ambient', 'ambient', 'scenes', 'system', 'routines'] },
      { ...changed, scene_order: changed.scene_order.slice(1) },
      { ...changed, hidden: ['main_lights', 'ambient'] },
      { ...changed, version: 2 },
    ]) expect(normalizePanelLayout(bad)).toEqual(DEFAULT_PANEL_LAYOUT)
  })

  it('falls back safely for null, arrays, and malformed server responses', () => {
    expect(normalizePanelLayout(null)).toEqual(DEFAULT_PANEL_LAYOUT)
    expect(normalizePanelLayout([])).toEqual(DEFAULT_PANEL_LAYOUT)
    expect(normalizePanelLayout({})).toEqual(DEFAULT_PANEL_LAYOUT)
  })

  it('moves items one step without mutating or wrapping', () => {
    const original = ['a', 'b', 'c']
    expect(moveItem(original, 'b', -1)).toEqual(['b', 'a', 'c'])
    expect(moveItem(original, 'b', 1)).toEqual(['a', 'c', 'b'])
    expect(moveItem(original, 'a', -1)).toEqual(original)
    expect(original).toEqual(['a', 'b', 'c'])
  })

  it('never allows both light cards to be hidden', () => {
    const mainHidden = toggleCard(DEFAULT_PANEL_LAYOUT, 'main_lights')
    expect(mainHidden.hidden).toEqual(['main_lights'])
    expect(toggleCard(mainHidden, 'ambient')).toEqual(mainHidden)
    expect(toggleCard(mainHidden, 'main_lights').hidden).toEqual([])
  })
})

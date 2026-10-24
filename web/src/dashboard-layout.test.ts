import { describe, expect, it } from 'vitest'
import {
  WIDGET_IDS, WIDGETS, defaultLayout, moveWidget, normalizeLayout, readLayout, resizeWidget, setWidgetVisible, storageKey,
  toggleFavorite, widgetAllowed, writeLayout,
} from './dashboard-layout'

function memoryStorage(fail = false) {
  const data = new Map<string, string>()
  return {
    data,
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => { if (fail) throw new DOMException('quota', 'QuotaExceededError'); data.set(key, value) },
    removeItem: (key: string) => { data.delete(key) },
  }
}

describe('dashboard layout', () => {
  it('defaults to every catalog widget, visible, in catalog order', () => {
    const layout = defaultLayout()
    expect(layout.widgets.map((item) => item.id)).toEqual([...WIDGET_IDS])
    expect(layout.widgets.every((item) => item.visible && WIDGETS[item.id].sizes.includes(item.size))).toBe(true)
  })

  it('never throws on corrupt or foreign data and falls back to the default', () => {
    for (const value of [null, 42, 'x', [], { version: 2, widgets: [] }, { version: 1 }, { version: 1, widgets: 'nope' }]) {
      expect(normalizeLayout(value)).toEqual(defaultLayout())
    }
  })

  it('drops unknown and duplicate widgets, fixes unsupported sizes and appends newer widgets', () => {
    const layout = normalizeLayout({ version: 1, widgets: [
      { id: 'alerts', size: 'xl', visible: false },
      { id: 'camera', size: 'md', visible: true },
      { id: 'alerts', size: 'sm', visible: true },
      { id: 'devices', size: 'sm', visible: true },
    ] })
    expect(layout.widgets[0]).toEqual({ id: 'alerts', size: 'md', visible: false })
    expect(layout.widgets[1]).toEqual({ id: 'devices', size: 'lg', visible: true })
    expect(layout.widgets.map((item) => item.id)).toHaveLength(WIDGET_IDS.length)
    expect(layout.widgets.map((item) => item.id)).not.toContain('camera')
  })

  it('keeps pinned widgets visible even when storage says hidden', () => {
    const layout = normalizeLayout({ version: 1, widgets: [{ id: 'system_overview', size: 'xl', visible: false }] })
    expect(layout.widgets.find((item) => item.id === 'system_overview')?.visible).toBe(true)
    expect(setWidgetVisible(defaultLayout(), 'system_overview', false).widgets[0].visible).toBe(true)
  })

  it('stores per authenticated device and never mixes two devices sharing a browser', () => {
    const storage = memoryStorage()
    const mine = setWidgetVisible(defaultLayout(), 'alerts', false)
    expect(writeLayout(storage, 'device-a', mine)).toBe(true)
    expect(readLayout(storage, 'device-a').widgets.find((item) => item.id === 'alerts')?.visible).toBe(false)
    expect(readLayout(storage, 'device-b')).toEqual(defaultLayout())
    expect([...storage.data.keys()]).toEqual([storageKey('device-a')])
    expect(storageKey('x')).not.toContain('panel')
  })

  it('reports a refused write instead of pretending it saved', () => {
    expect(writeLayout(memoryStorage(true), 'device-a', defaultLayout())).toBe(false)
    expect(writeLayout(memoryStorage(), null, defaultLayout())).toBe(false)
    expect(readLayout(null, 'device-a')).toEqual(defaultLayout())
    const broken = memoryStorage(); broken.data.set(storageKey('a'), '{not json')
    expect(readLayout(broken, 'a')).toEqual(defaultLayout())
  })

  it('moves, resizes only to supported sizes and bounds favourites', () => {
    const moved = moveWidget(defaultLayout(), 'routines', 0)
    expect(moved.widgets[0].id).toBe('routines')
    expect(moveWidget(moved, 'routines', -5).widgets[0].id).toBe('routines')
    expect(moveWidget(moved, 'routines', 99).widgets.at(-1)?.id).toBe('routines')
    expect(resizeWidget(defaultLayout(), 'services', 'xl')).toEqual(defaultLayout())
    expect(resizeWidget(defaultLayout(), 'services', 'md').widgets.find((item) => item.id === 'services')?.size).toBe('md')
    let layout = defaultLayout()
    for (let index = 0; index < 20; index += 1) layout = toggleFavorite(layout, `rt_${index}`)
    expect(layout.favorites).toHaveLength(12)
    expect(toggleFavorite(layout, 'rt_0').favorites).not.toContain('rt_0')
  })

  it('gates widgets by scope: any listed scope is enough, none means every device', () => {
    expect(widgetAllowed('system_overview', [])).toBe(true)
    expect(widgetAllowed('lights', ['system:read'])).toBe(false)
    expect(widgetAllowed('devices', ['system:read'])).toBe(true)
    expect(widgetAllowed('services', ['devices:read'])).toBe(false)
  })
})

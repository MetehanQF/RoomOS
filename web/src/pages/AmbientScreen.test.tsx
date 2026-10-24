import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { DEFAULT_AMBIENT } from '../ambient-screen'
import { AmbientScreen } from './AmbientScreen'

const base = { settings: DEFAULT_AMBIENT, time: '23:41', date: '17 Ekim Cumartesi', main: 'Açık · 3/4', ambient: 'Kapalı', system: 'Primary sağlıklı · Secondary erişilemiyor', routine: null, critical: 0, live: true, onDismiss: () => undefined }

describe('Ambient Screen 2.0', () => {
  it('shows a restrained clock and only selected live cards without controls', () => {
    const html = renderToStaticMarkup(<AmbientScreen {...base} />)
    expect(html).toContain('23:41')
    expect(html).toContain('Ana Oda Işıkları')
    expect(html).toContain('Secondary erişilemiyor')
    expect(html).not.toContain('Rutin')
    expect(html).not.toContain('/lights')
    expect(html).toContain('aria-label="Ambient Screen. Tap to return to the panel"')
  })

  it('never presents stale light and system values as live', () => {
    const html = renderToStaticMarkup(<AmbientScreen {...base} live={false} critical={2} />)
    expect(html).toContain('Son bilinen durum')
    expect(html).not.toContain('Açık · 3/4')
    expect(html).not.toContain('Primary sağlıklı')
    expect(html).toContain('2 kritik uyarı · son bilinen')
  })

  it('respects hidden/reordered cards and compact clock', () => {
    const html = renderToStaticMarkup(<AmbientScreen {...base} settings={{ ...DEFAULT_AMBIENT, clock: 'compact', date: false, cards: ['routines', 'lights'] }} routine="Gece rutini" />)
    expect(html.indexOf('Gece rutini')).toBeLessThan(html.indexOf('Ana Oda Işıkları'))
    expect(html).not.toContain('17 Ekim')
    expect(html).toContain('is-compact-clock')
  })
})

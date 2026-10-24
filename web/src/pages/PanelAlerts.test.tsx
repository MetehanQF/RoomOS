import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { AlertCatalog } from '../types'
import { PanelAlertIndicator, PanelCriticalStrip } from './PanelCards'

const catalog: AlertCatalog = {
  active: [{ id: 'node.primary.offline', type: 'node.offline', severity: 'critical', title: 'Primary is offline', message: 'Trusted check failed.', source: 'primary', started_at: '2026-10-16T08:00:00Z', updated_at: '2026-10-16T08:00:00Z', status: 'active' }],
  active_count: 1, highest_severity: 'critical', storage_status: 'ok',
}

describe('Phone 2 smart alert integration', () => {
  it('renders one lightweight tappable header indicator', () => {
    const html = renderToStaticMarkup(<MemoryRouter><PanelAlertIndicator alerts={catalog} status="ready" /></MemoryRouter>)
    expect(html).toContain('href="/notifications"')
    expect(html).toContain('1')
    expect(html).toContain('Critical notification')
  })

  it('shows a concise critical strip without duplicating control cards', () => {
    const html = renderToStaticMarkup(<MemoryRouter><PanelCriticalStrip alerts={catalog} /></MemoryRouter>)
    expect(html).toContain('Primary is offline')
    expect(html).toContain('View notification')
    expect(html).not.toContain('Trusted check failed')
  })

  it('stays quiet when access is forbidden or there is no critical alert', () => {
    expect(renderToStaticMarkup(<MemoryRouter><PanelAlertIndicator alerts={null} status="forbidden" /></MemoryRouter>)).toBe('')
    expect(renderToStaticMarkup(<MemoryRouter><PanelCriticalStrip alerts={{ ...catalog, active: [], active_count: 0, highest_severity: null }} /></MemoryRouter>)).toBe('')
  })
})

import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { DeviceOverviewRow } from './DeviceOverview'
import { T, workstation } from '../devices-fixtures.test-support'

describe('compact device overview', () => {
  it('links to an in-app detail, without mounting all sensors', () => {
    const html = renderToStaticMarkup(<MemoryRouter><DeviceOverviewRow device={workstation()} trusted now={T} /></MemoryRouter>)
    expect(html).toContain('href="/devices/workstation"')
    expect((html.match(/data-sensor/g) ?? []).length).toBeLessThanOrEqual(3)
    expect(html).not.toContain('Charging')
  })
  it('never labels offline telemetry healthy', () => {
    const html = renderToStaticMarkup(<MemoryRouter><DeviceOverviewRow device={workstation({connection:'offline'})} trusted now={T} /></MemoryRouter>)
    expect(html).toContain('Offline')
    expect(html).not.toContain('Healthy')
    expect(html).toContain('not current')
  })
})

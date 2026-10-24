import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { DnsClusterPanel, NodePanel, ReleaseIdentityPanel } from './System'
import type { DnsClusterStatus, Metric, SystemNode } from '../types'

const metric = (id: string, value: Metric['value'], unit: string | null = null, freshness: Metric['freshness'] = 'fresh'): Metric => ({
  id,
  value,
  unit,
  available: freshness !== 'unknown' && freshness !== 'unavailable',
  stale: freshness === 'stale',
  freshness,
  last_updated: '2026-10-03T08:00:00Z',
})

function node(id: SystemNode['id'], online = true): SystemNode {
  const prefix = `${id}_`
  return {
    id,
    name: id === 'primary' ? 'Primary' : 'Secondary',
    online,
    metrics: {
      [`${prefix}online`]: metric(`${prefix}online`, online),
      [`${prefix}cpu`]: metric(`${prefix}cpu`, 12, '%'),
      [`${prefix}ram`]: metric(`${prefix}ram`, 42, '%'),
      [`${prefix}disk`]: metric(`${prefix}disk`, 30, '%'),
      [`${prefix}temperature`]: metric(`${prefix}temperature`, 48, '°C'),
      [`${prefix}uptime`]: metric(`${prefix}uptime`, '2d 4h'),
      [`${prefix}docker`]: metric(`${prefix}docker`, true),
      ...(id === 'primary'
        ? {
            primary_tailscale: metric('primary_tailscale', true),
            primary_throttled: metric('primary_throttled', false),
          }
        : {
            secondary_smart: metric('secondary_smart', false),
            secondary_portainer: metric('secondary_portainer', true),
          }),
    },
  }
}

describe('System monitoring node cards', () => {
  it('renders every approved Primary metric with explicit freshness text', () => {
    const html = renderToStaticMarkup(<NodePanel node={node('primary')} label="Primary host" />)
    for (const label of ['CPU', 'RAM', 'Disk', 'Temperature', 'Uptime', 'Docker', 'Tailscale', 'Throttling']) {
      expect(html).toContain(label)
    }
    expect(html).toContain('Normal')
    expect(html).toContain('Fresh')
    expect(html).toContain('Reporting normally')
    expect(html).toContain('Online')
  })

  it('renders every approved Secondary metric', () => {
    const html = renderToStaticMarkup(<NodePanel node={node('secondary')} label="Companion machine" />)
    for (const label of ['CPU', 'RAM', 'Disk', 'Temperature', 'Uptime', 'Docker', 'SMART', 'Portainer']) {
      expect(html).toContain(label)
    }
  })

  it('does not present cached numeric values as live when a node is offline', () => {
    const html = renderToStaticMarkup(<NodePanel node={node('secondary', false)} label="Companion machine" />)
    expect(html).toContain('Offline')
    expect(html).toContain('No response')
    expect(html).not.toContain('12 %')
    expect(html).toContain('Stale')
  })

  it('renders DNS cluster and resolver health with measured response time', () => {
    const cluster: DnsClusterStatus = {
      status: 'degraded',
      primary: { id: 'primary', role: 'primary', status: 'healthy', last_checked: '2026-10-03T08:00:00Z', response_time_ms: 1.2 },
      secondary: { id: 'secondary', role: 'secondary', status: 'offline', last_checked: '2026-10-03T08:00:00Z', response_time_ms: null },
      last_checked: '2026-10-03T08:00:00Z',
    }
    const html = renderToStaticMarkup(<DnsClusterPanel cluster={cluster} />)
    expect(html).toContain('DNS Cluster')
    expect(html).toContain('Primary')
    expect(html).toContain('Secondary')
    expect(html).toContain('Response 1.2 ms')
    expect(html).toContain('Response unavailable')
    expect(html).toContain('Degraded')
    expect(html).not.toContain('192.0.2.')
  })

  it('shows a matching bounded Core/Web release identity', () => {
    const revision = 'a'.repeat(40)
    const html = renderToStaticMarkup(<ReleaseIdentityPanel
      core={{ release: 'phase-6a.1', revision, component: 'core' }}
      web={{ release: 'phase-6a.1', revision, component: 'web' }}
    />)
    expect(html).toContain('RoomOS phase-6a.1')
    expect(html).toContain('aaaaaaa')
    expect(html).toContain('Core and Web revisions match.')
    expect(html).toContain('data-release-match="true"')
  })

  it('makes a Core/Web revision mismatch visible', () => {
    const html = renderToStaticMarkup(<ReleaseIdentityPanel
      core={{ release: 'phase-6a.1', revision: 'a'.repeat(40), component: 'core' }}
      web={{ release: 'phase-6a.1', revision: 'b'.repeat(40), component: 'web' }}
    />)
    expect(html).toContain('Core/Web release mismatch')
    expect(html).toContain('role="alert"')
    expect(html).toContain('data-release-match="false"')
  })
})

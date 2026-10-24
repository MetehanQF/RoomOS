import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { NotificationCatalog } from '../types'
import { NotificationCenterView, NotificationsLocked } from './Notifications'

const catalog: NotificationCatalog = {
  notifications: [
    { id: 'node.primary.offline', type: 'node.offline', severity: 'critical', title: 'Primary is offline', message: 'Canonical check failed.', source: 'primary', started_at: '2026-10-16T08:00:00Z', updated_at: '2026-10-16T08:05:00Z', status: 'active', occurrence_count: 2, read_at: null, acknowledged_at: null, acknowledged_by: null, muted_until: null, muted_by: null },
    { id: 'resource.secondary.disk', type: 'resource.high', severity: 'warning', title: 'Secondary disk usage is high', message: 'Disk usage recovered.', source: 'secondary', started_at: '2026-10-15T08:00:00Z', updated_at: '2026-10-15T09:00:00Z', status: 'resolved', occurrence_count: 1, read_at: '2026-10-15T09:01:00Z', acknowledged_at: null, acknowledged_by: null, muted_until: null, muted_by: null },
  ],
  total: 2, unread_count: 1, active_count: 1, highest_severity: 'critical', limit: 50, offset: 0, storage_status: 'ok',
}

describe('Notification Center', () => {
  it('renders lifecycle, severity, source, occurrence and unread state', () => {
    const html = renderToStaticMarkup(<NotificationCenterView catalog={catalog} canAcknowledge onRead={vi.fn()} onAcknowledge={vi.fn()} onMute={vi.fn()} />)
    for (const text of ['Primary is offline', 'Critical', 'primary', '2 observations', 'Active', 'Resolved', 'Mark read', 'Acknowledge', 'Mute 1 hour']) expect(html).toContain(text)
    expect(html).toContain('notification-signal-stack')
    expect(html).not.toContain('192.168')
  })

  it('hides privileged actions while retaining read access', () => {
    const html = renderToStaticMarkup(<NotificationCenterView catalog={catalog} canAcknowledge={false} onRead={vi.fn()} onAcknowledge={vi.fn()} onMute={vi.fn()} />)
    expect(html).toContain('Mark read')
    expect(html).not.toContain('Acknowledge')
    expect(html).not.toContain('Mute 1 hour')
  })

  it('uses explicit locked and empty states', () => {
    expect(renderToStaticMarkup(<NotificationsLocked />)).toContain('Notification access required')
    const html = renderToStaticMarkup(<NotificationCenterView catalog={{ ...catalog, notifications: [], total: 0, unread_count: 0, active_count: 0, highest_severity: null }} canAcknowledge={false} onRead={vi.fn()} onAcknowledge={vi.fn()} onMute={vi.fn()} />)
    expect(html).toContain('No notifications match')
  })

  it('highlights the deep-linked lifecycle from a push tap', () => {
    const html = renderToStaticMarkup(<NotificationCenterView catalog={catalog} canAcknowledge={false} onRead={vi.fn()} onAcknowledge={vi.fn()} onMute={vi.fn()} focusId="node.primary.offline" />)
    expect(html).toContain('id="notification-node.primary.offline" aria-current="true"')
    expect(html).toContain('is-focused')
    expect(html.match(/is-focused/g)).toHaveLength(1)
  })

  it('shows evidence, snooze state and snooze controls only with alerts:snooze', () => {
    const now = Date.parse('2026-10-16T08:00:00Z')
    const withEvidence = { ...catalog, snoozes: { 'node.primary.offline': { until: '2026-10-16T12:00:00Z', severity: 'critical' as const } },
      insights: { 'node.primary.offline': { value: 86, unit: '°C', profile: 'normal' as const, severity_basis: 'critical' as const, threshold: 85, minutes: 2, coverage: 1, since: null, free_gb: null, reason: 'x' } } }
    const html = renderToStaticMarkup(<NotificationCenterView catalog={withEvidence} canAcknowledge={false} canSnooze now={now} onRead={vi.fn()} onAcknowledge={vi.fn()} onMute={vi.fn()} />)
    for (const text of ['Now 86°C', '≥ 85°C for at least 2 min', 'Snoozed · 4 h left', 'Remove snooze', 'Raspberry Pi 5']) expect(html).toContain(text)
    const viewer = renderToStaticMarkup(<NotificationCenterView catalog={{ ...withEvidence, snoozes: {} }} canAcknowledge={false} onRead={vi.fn()} onAcknowledge={vi.fn()} onMute={vi.fn()} />)
    expect(viewer).not.toContain('Snooze 8 hours')
    const editor = renderToStaticMarkup(<NotificationCenterView catalog={{ ...withEvidence, snoozes: {} }} canAcknowledge={false} canSnooze onRead={vi.fn()} onAcknowledge={vi.fn()} onMute={vi.fn()} />)
    for (const hours of ['2 hours', '8 hours', '24 hours', '7 days']) expect(editor).toContain(`Snooze ${hours}`)
    for (const hours of ['1 hour', '4 hours']) expect(editor).not.toContain(`Snooze ${hours}`)
  })
})

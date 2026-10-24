/* 7I: human wording for resource-alert evidence. Never claims a duration the server did not prove. */
import type { AlertInsight, AlertSnooze } from './types'

const SOURCES: Record<string, string> = { workstation: 'Workstation', primary: 'Raspberry Pi 5', secondary: 'Secondary', dns: 'DNS', frigate: 'Frigate', nextcloud: 'Nextcloud', home_assistant: 'Home Assistant' }

export function sourceLabel(source: string) {
  return SOURCES[source] ?? source
}

export function remaining(until: string, now = Date.now()) {
  const minutes = Math.max(0, Math.round((new Date(until).getTime() - now) / 60000))
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  if (hours >= 48) return hours % 24 ? `${Math.floor(hours / 24)} d ${hours % 24} h` : `${hours / 24} d`
  return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`
}

export function insightLines(insight: AlertInsight | undefined): string[] {
  if (!insight) return []
  const lines: string[] = []
  if (insight.value !== null) lines.push(`Now ${insight.value}${insight.unit}`)
  if (insight.free_gb !== null) lines.push(`${insight.free_gb} GB free`)
  if (insight.severity_basis && insight.threshold !== null && insight.minutes !== null) {
    lines.push(`≥ ${insight.threshold}${insight.unit} for at least ${insight.minutes} min`)
    if (insight.coverage !== null) lines.push(`${Math.round(insight.coverage * 100)}% sample coverage`)
    if (insight.since) lines.push(`since ${new Date(insight.since).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`)
  } else if (insight.reason) {
    lines.push(insight.reason === 'within policy' ? 'Currently within policy' : insight.reason)
  }
  if (insight.profile !== 'normal') lines.push(`${insight.profile === 'gaming' ? 'Gaming' : 'Development'} profile`)
  return lines
}

export function snoozeLabel(snooze: AlertSnooze | undefined, now = Date.now()) {
  return snooze ? `Snoozed · ${remaining(snooze.until, now)} left` : null
}

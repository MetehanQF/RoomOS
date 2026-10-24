/** Browser-local Phone 2 ambient preferences; no HA state or account data is stored here. */
export type AmbientCard = 'lights' | 'system' | 'routines'
export type AmbientSettings = {
  enabled: boolean
  idleMinutes: 1 | 3 | 5 | 10
  clock: 'large' | 'compact'
  date: boolean
  tone: 'dark' | 'very-dark'
  cards: AmbientCard[]
}

export const AMBIENT_KEY = 'roomos:ambient:v1'
export const AMBIENT_IDLE_CHOICES = [1, 3, 5, 10] as const
export const AMBIENT_CARDS: AmbientCard[] = ['lights', 'system', 'routines']
export const DEFAULT_AMBIENT: AmbientSettings = {
  enabled: true, idleMinutes: 5, clock: 'large', date: true, tone: 'very-dark', cards: ['lights', 'system'],
}

export function formatAmbientClock(now: Date): { time: string; date: string } {
  return {
    time: new Intl.DateTimeFormat('tr-TR', { timeZone: 'Europe/Istanbul', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(now),
    date: new Intl.DateTimeFormat('tr-TR', { timeZone: 'Europe/Istanbul', weekday: 'long', day: 'numeric', month: 'long' }).format(now),
  }
}

export function readAmbientSettings(storage: Pick<Storage, 'getItem'> | null, legacyMinutes: number): AmbientSettings {
  let raw: unknown
  try { raw = JSON.parse(storage?.getItem(AMBIENT_KEY) ?? 'null') } catch { raw = null }
  if (raw === null) return { ...DEFAULT_AMBIENT, enabled: legacyMinutes !== 0, idleMinutes: legacyMinutes === 1 || legacyMinutes === 3 || legacyMinutes === 5 || legacyMinutes === 10 ? legacyMinutes : legacyMinutes === 2 ? 3 : 5 }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return DEFAULT_AMBIENT
  const value = raw as Record<string, unknown>
  const cards = Array.isArray(value.cards) ? [...new Set(value.cards.filter((item): item is AmbientCard => AMBIENT_CARDS.includes(item as AmbientCard)))] : DEFAULT_AMBIENT.cards
  return {
    enabled: typeof value.enabled === 'boolean' ? value.enabled : DEFAULT_AMBIENT.enabled,
    idleMinutes: (AMBIENT_IDLE_CHOICES as readonly unknown[]).includes(value.idleMinutes) ? value.idleMinutes as AmbientSettings['idleMinutes'] : DEFAULT_AMBIENT.idleMinutes,
    clock: value.clock === 'compact' ? 'compact' : 'large',
    date: typeof value.date === 'boolean' ? value.date : DEFAULT_AMBIENT.date,
    tone: value.tone === 'dark' ? 'dark' : 'very-dark',
    cards,
  }
}

export function writeAmbientSettings(storage: Pick<Storage, 'setItem'> | null, settings: AmbientSettings): void {
  try { storage?.setItem(AMBIENT_KEY, JSON.stringify(settings)) } catch { /* private storage: keep in-memory settings */ }
}

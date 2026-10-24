import { describe, expect, it } from 'vitest'
import { AMBIENT_KEY, DEFAULT_AMBIENT, formatAmbientClock, readAmbientSettings, writeAmbientSettings } from './ambient-screen'

const storage = (initial: Record<string, string> = {}) => {
  const values = new Map(Object.entries(initial))
  return { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value) } }
}

describe('Phone 2 ambient preferences', () => {
  it('formats Istanbul time without a separate AM/PM line', () => {
    expect(formatAmbientClock(new Date('2026-10-17T06:46:00Z'))).toMatchObject({ time: '09:46' })
    expect(formatAmbientClock(new Date('2026-10-17T06:46:00Z')).date.toLocaleLowerCase('tr-TR')).toContain('ekim')
  })
  it('migrates the old idle timer without changing other panel settings', () => {
    expect(readAmbientSettings(storage(), 0)).toMatchObject({ enabled: false, idleMinutes: 5 })
    expect(readAmbientSettings(storage(), 2)).toMatchObject({ enabled: true, idleMinutes: 3 })
    expect(readAmbientSettings(storage(), 10)).toMatchObject({ enabled: true, idleMinutes: 10 })
  })

  it('accepts only known cards and repairs corrupt or duplicate order', () => {
    const s = storage({ [AMBIENT_KEY]: JSON.stringify({ enabled: true, idleMinutes: 1, cards: ['system', 'system', 'bogus', 'lights'], tone: 'very-dark' }) })
    expect(readAmbientSettings(s, 5).cards).toEqual(['system', 'lights'])
    expect(readAmbientSettings(storage({ [AMBIENT_KEY]: '{' }), 5)).toEqual(DEFAULT_AMBIENT)
  })

  it('persists only this browser profile and tolerates blocked storage', () => {
    const s = storage()
    const next = { ...DEFAULT_AMBIENT, idleMinutes: 1 as const, cards: ['routines', 'lights'] as Array<'routines' | 'lights' | 'system'> }
    writeAmbientSettings(s, next)
    expect(readAmbientSettings(s, 5)).toEqual(next)
    expect(() => writeAmbientSettings({ setItem: () => { throw Error('blocked') } }, next)).not.toThrow()
  })
})

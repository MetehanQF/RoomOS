import type { RoutineDraft } from './types'

export const routineWeekdays = [
  ['mon', 'Mon'], ['tue', 'Tue'], ['wed', 'Wed'], ['thu', 'Thu'],
  ['fri', 'Fri'], ['sat', 'Sat'], ['sun', 'Sun'],
] as const

export function validateRoutineDraft(draft: RoutineDraft) {
  if (!draft.name.trim() || draft.name.trim().length > 60) return 'Enter a name up to 60 characters.'
  if (!draft.action.scene_id) return 'Choose a scene.'
  if (draft.trigger.type === 'time') {
    if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(draft.trigger.time)) return 'Choose a valid time.'
    if (draft.trigger.days.length === 0) return 'Choose at least one weekday.'
  }
  return null
}

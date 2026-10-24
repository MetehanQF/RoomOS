import { useState } from 'react'

/** Keys that finish a keyboard slider adjustment. */
export const COMMIT_KEYS = new Set(['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'])

/** Local preview while dragging; exactly one commit on release; pointercancel discards. */
export function useDraft(live: number, enabled: boolean) {
  const [draft, setDraft] = useState<number | null>(null)
  // Losing permission mid-drag (light off/unavailable, disconnect, pending) discards the draft.
  if (!enabled && draft !== null) setDraft(null)
  const take = (onCommit: (value: number) => void) => {
    if (draft === null) return
    setDraft(null)
    if (enabled && draft !== live) onCommit(draft)
  }
  return { value: draft ?? live, dragging: draft !== null, set: setDraft, cancel: () => setDraft(null), take }
}

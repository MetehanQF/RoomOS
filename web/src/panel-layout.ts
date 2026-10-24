export const PANEL_CARD_IDS = ['main_lights', 'ambient', 'scenes', 'routines', 'system'] as const
export const PANEL_SCENE_IDS = ['normal', 'work_1', 'work_2', 'gaming_1', 'gaming_2', 'movie', 'night', 'all_off'] as const

export type PanelCardId = typeof PANEL_CARD_IDS[number]
export type PanelSceneId = typeof PANEL_SCENE_IDS[number]
export type PanelLayout = {
  version: 1
  cards: PanelCardId[]
  hidden: PanelCardId[]
  density: 'normal' | 'compact'
  scene_order: PanelSceneId[]
  header: { clock: boolean; date: boolean; connection: boolean; health: boolean }
}
export type PanelLayoutResponse = { layout: PanelLayout; source: 'default' | 'saved'; updated_at: string | null }

export const DEFAULT_PANEL_LAYOUT: PanelLayout = {
  version: 1,
  cards: [...PANEL_CARD_IDS],
  hidden: [],
  density: 'normal',
  scene_order: [...PANEL_SCENE_IDS],
  header: { clock: true, date: true, connection: true, health: true },
}

const exactCatalog = <T extends string>(value: unknown, catalog: readonly T[]): value is T[] =>
  Array.isArray(value) && value.length === catalog.length && new Set(value).size === catalog.length
    && value.every((item) => catalog.includes(item as T))

export function normalizePanelLayout(value: unknown): PanelLayout {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return structuredClone(DEFAULT_PANEL_LAYOUT)
  const candidate = value as Record<string, unknown>
  const header = candidate.header
  if (candidate.version !== 1
    || !exactCatalog(candidate.cards, PANEL_CARD_IDS)
    || !Array.isArray(candidate.hidden)
    || new Set(candidate.hidden).size !== candidate.hidden.length
    || !candidate.hidden.every((item) => PANEL_CARD_IDS.includes(item as PanelCardId))
    || candidate.hidden.includes('main_lights') && candidate.hidden.includes('ambient')
    || candidate.density !== 'normal' && candidate.density !== 'compact'
    || !exactCatalog(candidate.scene_order, PANEL_SCENE_IDS)
    || !header || typeof header !== 'object' || Array.isArray(header)
    || !['clock', 'date', 'connection', 'health'].every((key) => typeof (header as Record<string, unknown>)[key] === 'boolean')) {
    return structuredClone(DEFAULT_PANEL_LAYOUT)
  }
  return {
    version: 1,
    cards: [...candidate.cards],
    hidden: [...candidate.hidden] as PanelCardId[],
    density: candidate.density,
    scene_order: [...candidate.scene_order],
    header: { ...(header as PanelLayout['header']) },
  }
}

export function moveItem<T>(items: readonly T[], item: T, delta: -1 | 1): T[] {
  const from = items.indexOf(item)
  const to = from + delta
  if (from < 0 || to < 0 || to >= items.length) return [...items]
  const next = [...items]
  ;[next[from], next[to]] = [next[to], next[from]]
  return next
}

export function toggleCard(layout: PanelLayout, card: PanelCardId): PanelLayout {
  if (layout.hidden.includes(card)) return { ...layout, hidden: layout.hidden.filter((id) => id !== card) }
  if ((card === 'main_lights' && layout.hidden.includes('ambient')) || (card === 'ambient' && layout.hidden.includes('main_lights'))) return layout
  return { ...layout, hidden: [...layout.hidden, card] }
}

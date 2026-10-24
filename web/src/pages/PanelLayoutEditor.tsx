import { useState } from 'react'
import {
  moveItem, PANEL_SCENE_IDS, toggleCard,
  type PanelCardId, type PanelLayout, type PanelSceneId,
} from '../panel-layout'

const CARD_LABELS: Record<PanelCardId, string> = {
  main_lights: 'Ana Oda Işıkları', ambient: 'Ambiyans Işığı', scenes: 'Sahneler', routines: 'Rutinler', system: 'Sistem özeti',
}
const SCENE_LABELS: Record<PanelSceneId, string> = {
  normal: 'Normal', work_1: 'Çalışma 1', work_2: 'Çalışma 2', gaming_1: 'Oyun 1', gaming_2: 'Oyun 2', movie: 'Film', night: 'Gece', all_off: 'Tümü kapalı',
}
const HEADER_LABELS: Record<keyof PanelLayout['header'], string> = { clock: 'Saat', date: 'Tarih', connection: 'Bağlantı', health: 'Sistem durumu' }

export function PanelLayoutEditor({ layout, onSave, onReset, onClose }: {
  layout: PanelLayout
  onSave: (layout: PanelLayout) => Promise<void>
  onReset: () => Promise<void>
  onClose: () => void
}) {
  const [draft, setDraft] = useState(() => structuredClone(layout))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(false)
  const act = async (action: () => Promise<void>) => {
    setBusy(true); setError(false)
    try { await action(); onClose() } catch { setError(true) } finally { setBusy(false) }
  }
  const moveCard = (id: PanelCardId, delta: -1 | 1) => setDraft((current) => ({ ...current, cards: moveItem(current.cards, id, delta) }))
  const moveScene = (id: PanelSceneId, delta: -1 | 1) => setDraft((current) => ({ ...current, scene_order: moveItem(current.scene_order, id, delta) }))

  return <section className="panel-layout-editor" aria-label="Panel düzeni">
    <header className="panel-layout-editor-head"><div><h2>Düzeni düzenle</h2><p>Değişiklikleri kaydetmeden önce aşağıda önizleyin.</p></div><button type="button" className="panel-layout-action" onClick={onClose}>Kapat</button></header>
    <div className="panel-layout-editor-grid">
      <div className="panel-layout-editor-column">
        <fieldset className="panel-layout-density"><legend>Kart yoğunluğu</legend><div>
          {(['normal', 'compact'] as const).map((density) => <button key={density} type="button" className="panel-layout-action" aria-label={`${density === 'normal' ? 'Normal' : 'Kompakt'} düzen`} aria-pressed={draft.density === density} onClick={() => setDraft((current) => ({ ...current, density }))}>{density === 'normal' ? 'Normal' : 'Kompakt'}</button>)}
        </div></fieldset>
        <div className="panel-layout-preview" data-layout-preview data-density={draft.density}>
          <strong>Önizleme</strong><ol>{draft.cards.filter((id) => !draft.hidden.includes(id)).map((id) => <li key={id}>{CARD_LABELS[id]}</li>)}</ol>
        </div>
        <section><h3>Kartlar</h3><div className="panel-layout-list">
          {draft.cards.map((id, index) => { const visible = !draft.hidden.includes(id); return <div className="panel-layout-row" data-layout-card={id} key={id}>
            <span><b>{index + 1}</b><strong>{CARD_LABELS[id]}</strong></span>
            <div><button type="button" className="panel-layout-action" aria-label={`${CARD_LABELS[id]} kartını ${visible ? 'gizle' : 'göster'}`} aria-pressed={visible} onClick={() => setDraft((current) => toggleCard(current, id))}>{visible ? 'Göster' : 'Gizli'}</button>
              <button type="button" className="panel-layout-action" aria-label={`${CARD_LABELS[id]} yukarı taşı`} disabled={index === 0} onClick={() => moveCard(id, -1)}>↑</button>
              <button type="button" className="panel-layout-action" aria-label={`${CARD_LABELS[id]} aşağı taşı`} disabled={index === draft.cards.length - 1} onClick={() => moveCard(id, 1)}>↓</button></div>
          </div> })}
        </div></section>
      </div>
      <div className="panel-layout-editor-column">
        <section><h3>Üst bölüm</h3><div className="panel-layout-toggles">
          {(Object.keys(HEADER_LABELS) as Array<keyof PanelLayout['header']>).map((key) => <button key={key} type="button" className="panel-layout-action" role="switch" aria-checked={draft.header[key]} onClick={() => setDraft((current) => ({ ...current, header: { ...current.header, [key]: !current.header[key] } }))}>{HEADER_LABELS[key]}</button>)}
        </div></section>
        <section><h3>Sahne sırası</h3><div className="panel-layout-list is-scenes">
          {draft.scene_order.map((id, index) => <div className="panel-layout-row" data-layout-scene={id} key={id}><span><b>{index + 1}</b><strong>{SCENE_LABELS[id]}</strong></span><div>
            <button type="button" className="panel-layout-action" aria-label={`${SCENE_LABELS[id]} yukarı taşı`} disabled={index === 0} onClick={() => moveScene(id, -1)}>↑</button>
            <button type="button" className="panel-layout-action" aria-label={`${SCENE_LABELS[id]} aşağı taşı`} disabled={index === PANEL_SCENE_IDS.length - 1} onClick={() => moveScene(id, 1)}>↓</button>
          </div></div>)}
        </div></section>
      </div>
    </div>
    {error && <p className="panel-layout-error" role="alert">Kaydedilemedi. Bağlantıyı kontrol edip yeniden deneyin.</p>}
    <footer className="panel-layout-actions">
      <button type="button" className="panel-layout-action is-reset" data-reset-layout disabled={busy} onClick={() => void act(onReset)}>Varsayılanlar</button>
      <button type="button" className="panel-layout-action" data-cancel-layout disabled={busy} onClick={onClose}>İptal</button>
      <button type="button" className="panel-layout-action is-primary" data-save-layout disabled={busy} onClick={() => void act(() => onSave(draft))}>{busy ? 'Kaydediliyor…' : 'Kaydet'}</button>
    </footer>
  </section>
}

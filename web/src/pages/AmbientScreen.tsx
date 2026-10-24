import type { AmbientSettings } from '../ambient-screen'

type Props = {
  settings: AmbientSettings
  time: string
  date: string
  main: string | null
  ambient: string | null
  system: string | null
  routine: string | null
  critical: number
  live: boolean
  onDismiss: () => void
}

/** Passive in-PWA night view. It never invokes an HA action or fetches a resource. */
export function AmbientScreen({ settings, time, date, main, ambient, system, routine, critical, live, onDismiss }: Props) {
  const cards = {
    lights: main || ambient ? <div className="ambient-info" key="lights"><span>Ana Oda Işıkları</span><strong>{live ? main ?? 'Bilinmiyor' : 'Son bilinen durum'}</strong><span>Ambiyans</span><strong>{live ? ambient ?? 'Bilinmiyor' : 'Son bilinen durum'}</strong></div> : null,
    system: system ? <div className="ambient-info" key="system"><span>Sistem</span><strong>{live ? system : 'Son bilinen durum'}</strong></div> : null,
    routines: routine ? <div className="ambient-info" key="routines"><span>Etkin rutin</span><strong>{routine}</strong></div> : null,
  }
  return <div role="button" tabIndex={0} className={`panel-saver ambient-screen is-${settings.tone} ${settings.clock === 'compact' ? 'is-compact-clock' : ''}`} onClick={onDismiss} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onDismiss() } }} aria-label="Ambient Screen. Tap to return to the panel">
    <div className="ambient-clock"><time>{time}</time>{settings.date && <span>{date}</span>}</div>
    {critical > 0 && <p className="ambient-critical" role="status">{critical} kritik uyarı{live ? '' : ' · son bilinen'}</p>}
    <div className="ambient-info-grid">{settings.cards.map((id) => cards[id])}</div>
    {!live && <small className="ambient-stale">RoomOS bağlantısı doğrulanamadı · Son bilinen durum</small>}
    <span className="ambient-return">Panele dönmek için dokun</span>
  </div>
}

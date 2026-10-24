import { useState, type CSSProperties, type KeyboardEvent } from 'react'
import { Link } from 'react-router-dom'
import { Icon, type IconName } from '../components/Icons'
import { COMMIT_KEYS, useDraft } from '../light-draft'
import { displayName, previewColor, stateLine } from '../lights-state'
import type { LightsSnapshot } from '../lights-store'
import { groupValueLabel, mainLightsStatusLabel, type GroupValue, type MainLightsSummary } from '../main-lights'
import { PANEL_DEVICE_LABEL, panelDeviceDetail, panelDeviceTone } from '../panel-state'
import type { SceneId } from '../home-state'
import type { AlertCatalog, DeviceDetail, Light } from '../types'
import { BrightnessSlider, ColourControls, KelvinSlider, type LightsActions } from './Lights'

/* Phase 7E.1 panel cards. Flat surfaces (no blur, no animation) for low-end phone GPUs; every
   control is a ≥48×48 CSS px target. Values are shown only when RoomOS knows them. */

type Feedback = NonNullable<LightsSnapshot['feedback']>['value']

export function PanelFeedback({ feedback, onDismiss }: { feedback: Feedback | null; onDismiss: () => void }) {
  if (!feedback) return null
  return <div className={`panel-feedback is-${feedback.tone}`} role="status">
    <span><strong>{feedback.title}</strong>{feedback.detail && <small>{feedback.detail}</small>}</span>
    {feedback.tone !== 'pending' && <button type="button" onClick={onDismiss} aria-label="Dismiss result">×</button>}
  </div>
}

export function PanelAlertIndicator({ alerts, status }: { alerts: AlertCatalog | null; status: string }) {
  if (status === 'forbidden') return null
  const count = alerts?.active_count ?? 0
  const severity = alerts?.highest_severity ?? 'clear'
  const label = severity === 'critical' ? `Critical notification · ${count} active` : `${count} active notification${count === 1 ? '' : 's'}`
  return <Link to="/notifications" className={`panel-alert-indicator is-${severity}`} aria-label={label}><Icon name="alert" /><span>{count}</span></Link>
}

export function PanelCriticalStrip({ alerts }: { alerts: AlertCatalog | null }) {
  const critical = alerts?.active?.find((item) => item.severity === 'critical')
  if (!critical) return null
  const remaining = Math.max(0, (alerts?.active_count ?? 1) - 1)
  return <Link to="/notifications" className="panel-critical-strip" role="alert"><Icon name="alert" /><span><strong>{critical.title}</strong>{remaining > 0 && <small>+{remaining} more active</small>}</span><b>View notification</b><Icon name="chevron" /></Link>
}

/** One commit on release, like the Lights page sliders. A mixed group shows a range, not a value. */
export function GroupSlider({ id, label, value, unit, min, max, step, enabled, onCommit, ends }: {
  id: string; label: string; value: GroupValue; unit: '%' | ' K'; min: number; max: number; step: number
  enabled: boolean; onCommit: (value: number) => void; ends?: [string, string]
}) {
  const known = value.kind === 'uniform' ? value.value : null
  const resting = known ?? (value.kind === 'mixed' ? Math.round((value.min + value.max) / 2 / step) * step : Math.round((min + max) / 2 / step) * step)
  const draft = useDraft(resting, enabled)
  const shown = draft.dragging ? `${draft.value}${unit}` : groupValueLabel(value, unit)
  const commit = () => draft.take(onCommit)
  const unset = !draft.dragging && known === null
  return <div className="panel-slider">
    <div className="panel-slider-label"><label htmlFor={id}>{label}</label><strong className={value.kind === 'mixed' && !draft.dragging ? 'is-mixed' : ''}>{shown}</strong></div>
    <input id={id} className={`light-range ${unit === ' K' ? 'kelvin-range' : 'brightness-range'} ${unset ? 'is-unset' : ''}`} type="range" min={min} max={max} step={step}
      value={draft.value} disabled={!enabled} style={{ '--fill': `${((draft.value - min) / (max - min)) * 100}%` } as CSSProperties}
      aria-valuetext={draft.dragging || known !== null ? `${draft.value}${unit === '%' ? ' percent' : ' Kelvin'}` : value.kind === 'mixed' ? `Mixed, ${value.min} to ${value.max}` : 'Unknown'}
      onChange={(event) => draft.set(Number(event.target.value))} onPointerUp={commit} onPointerCancel={draft.cancel}
      onKeyUp={(event: KeyboardEvent) => { if (COMMIT_KEYS.has(event.key)) commit() }} onBlur={draft.cancel} />
    {ends && <div className="light-range-ends"><span>{ends[0]}</span><span>{ends[1]}</span></div>}
  </div>
}

export type MainLightsActions = {
  can: (action: 'power' | 'brightness' | 'temperature') => boolean
  power: (on: boolean) => void
  brightness: (percent: number) => void
  temperature: (kelvin: number) => void
}

/** "Ana Oda Işıkları": the four L530 room lights as one control (Core group main_room_lights). */
export function PanelMainLights({ summary, snapshot, live, actions, onDismiss }: {
  summary: MainLightsSummary | null; snapshot: LightsSnapshot; live: boolean; actions: MainLightsActions; onDismiss: () => void
}) {
  const pending = snapshot.pending?.lightId === null && snapshot.pending.action.startsWith('main_') ? snapshot.pending.action : null
  const feedback = snapshot.feedback?.scope === 'main' ? snapshot.feedback.value : null
  const state = summary?.power ?? 'unknown'
  const turnOn = state !== 'on'
  const warm = summary && summary.kelvin.kind === 'uniform' ? summary.kelvin.value : null
  return <section className={`panel-card panel-main is-${state}`} data-group="main_room_lights" aria-labelledby="panel-main-title"
    style={warm ? { '--glow': warm < 3500 ? '255,190,120' : warm < 5500 ? '255,232,200' : '200,220,255' } as CSSProperties : undefined}>
    <div className="panel-card-head">
      <span className="panel-card-icon" aria-hidden="true"><Icon name="bulb" /></span>
      <div><h2 id="panel-main-title">Ana Oda Işıkları</h2>
        <p className="panel-card-state">{pending ? 'Çalışıyor…' : summary ? mainLightsStatusLabel(summary) : snapshot.lightsStatus === 'error' ? 'Işıklar okunamadı' : 'Yükleniyor…'}</p></div>
      <button type="button" className={`panel-power ${state === 'on' ? 'is-on' : state === 'partial' ? 'is-partial' : ''}`} disabled={!actions.can('power')}
        aria-pressed={state === 'on' ? true : state === 'partial' ? 'mixed' : false} aria-label={turnOn ? 'Ana Oda Işıklarını aç' : 'Ana Oda Işıklarını kapat'}
        onClick={() => actions.power(turnOn)}><Icon name="power" /><span>{turnOn ? 'Aç' : 'Kapat'}</span></button>
    </div>
    {summary && <div className="panel-meter" aria-hidden="true">{summary.members.map((light) =>
      <i key={light.id} className={!light.available ? 'is-unavailable' : light.on === true ? 'is-on' : ''} title={displayName(light)} />)}</div>}
    {summary && summary.power === 'on' && <>
      <GroupSlider id="main-brightness" label="Parlaklık" value={summary.brightness} unit="%" min={1} max={100} step={1}
        enabled={actions.can('brightness')} onCommit={actions.brightness} />
      <GroupSlider id="main-kelvin" label="Beyaz ışık sıcaklığı" value={summary.kelvin} unit=" K" min={summary.minKelvin} max={summary.maxKelvin} step={50}
        enabled={actions.can('temperature')} onCommit={actions.temperature} ends={[`Sıcak ${summary.minKelvin} K`, `Soğuk ${summary.maxKelvin} K`]} />
      {summary.colourMembers > 0 && <p className="panel-note">{summary.colourMembers} ışık renkli modda · sıcaklık seçmek hepsini beyaza alır.</p>}
    </>}
    {summary && summary.power === 'partial' && <p className="panel-note">Ortak parlaklık ve sıcaklık için önce tüm ışıkları açın.</p>}
    {live && summary && summary.power === 'on' && !actions.can('brightness') && !pending && <p className="panel-note">Bu cihazdan grup ayarı yapılamıyor.</p>}
    <PanelFeedback feedback={feedback && feedback.tone !== 'pending' ? feedback : null} onDismiss={onDismiss} />
  </section>
}

/** Desk Ambient (L920 strip): only the controls the device advertises. Effects stay in the sheet. */
export function PanelAmbient({ light, snapshot, actions, onMore }: { light: Light | null; snapshot: LightsSnapshot; actions: LightsActions; onMore: () => void }) {
  const [mode, setMode] = useState<'white' | 'colour' | null>(null)
  if (!light) return <section className="panel-card panel-ambient" aria-label="Ambiyans"><p className="panel-empty">{snapshot.lightsStatus === 'error' ? 'Ambiyans ışığı okunamadı.' : 'Yükleniyor…'}</p></section>
  const on = light.available && light.on === true
  const caps = light.capabilities
  const pending = snapshot.pending?.lightId === light.id
  const feedback = snapshot.feedback?.scope === light.id ? snapshot.feedback.value : null
  const reported = light.color_mode === 'color_temp' ? 'white' : light.color_mode ? 'colour' : null
  const active = mode ?? reported ?? (caps.color ? 'colour' : 'white')
  const preview = previewColor(light)
  return <section className={`panel-card panel-ambient ${on ? 'is-on' : light.available ? 'is-off' : 'is-unavailable'}`} data-light={light.id} aria-labelledby="panel-ambient-title"
    style={preview ? { '--glow-color': preview } as CSSProperties : undefined}>
    <div className="panel-card-head">
      <span className="panel-card-icon light-preview" style={{ '--preview': preview ?? 'transparent' } as CSSProperties} aria-hidden="true" />
      <div><h2 id="panel-ambient-title">Ambiyans</h2><p className="panel-card-state">{pending ? 'Çalışıyor…' : stateLine(light)}</p></div>
      {light.available && <button type="button" className={`panel-power ${on ? 'is-on' : ''}`} disabled={!actions.can(light, 'power')} aria-pressed={on}
        aria-label={`${on ? 'Turn off' : 'Turn on'} ${displayName(light)}`} onClick={() => actions.power(light, !on)}><Icon name="power" /><span>{on ? 'Kapat' : 'Aç'}</span></button>}
    </div>
    {!light.available && <p className="panel-note">Erişilemiyor · şu an kontrol edilemez.</p>}
    {on && <>
      {caps.brightness && light.brightness !== null && <BrightnessSlider key={`b-${light.id}`} light={light} enabled={actions.can(light, 'brightness')} onCommit={(value) => actions.brightness(light, value)} />}
      {caps.temperature && caps.color && <div className="panel-segmented" role="group" aria-label="Ambiyans modu">
        <button type="button" aria-pressed={active === 'colour'} className={active === 'colour' ? 'is-active' : ''} onClick={() => setMode('colour')}>Renk</button>
        <button type="button" aria-pressed={active === 'white'} className={active === 'white' ? 'is-active' : ''} onClick={() => setMode('white')}>Beyaz</button>
      </div>}
      {active === 'colour' && caps.color && <ColourControls key={`c-${light.id}`} light={light} enabled={actions.can(light, 'color')} onCommit={(rgb) => actions.color(light, rgb)} />}
      {active === 'white' && caps.temperature && <KelvinSlider key={`k-${light.id}`} light={light} enabled={actions.can(light, 'temperature')} onCommit={(kelvin) => actions.temperature(light, kelvin)} />}
    </>}
    {light.available && !on && <p className="panel-note">Parlaklık ve renk için açın.</p>}
    {light.available && <button type="button" className="panel-more" onClick={onMore}>{caps.effects && light.effects.length ? 'Efektler ve daha fazlası' : 'Tüm ayarlar'}<Icon name="chevron" /></button>}
    <PanelFeedback feedback={feedback && feedback.tone !== 'pending' ? feedback : null} onDismiss={actions.dismiss} />
  </section>
}

export type PanelSceneButton = { id: SceneId; label: string; icon: IconName; active: boolean; pending: boolean; enabled: boolean }

export function PanelScenes({ scenes, title, onActivate }: { scenes: PanelSceneButton[]; title: string; onActivate: (id: SceneId) => void }) {
  return <section className="panel-section panel-scenes-section" aria-labelledby="panel-scenes-title">
    <div className="panel-section-head"><h2 id="panel-scenes-title">Sahneler</h2><span className="panel-section-meta">{title}</span></div>
    <div className="panel-scenes">{scenes.map((scene) =>
      <button key={scene.id} type="button" className={scene.active ? 'is-active' : ''} disabled={!scene.enabled} aria-pressed={scene.active} onClick={() => onActivate(scene.id)}>
        <Icon name={scene.icon} /><span>{scene.pending ? '…' : scene.label}</span>
      </button>)}
    </div>
  </section>
}

export function PanelRoutines({ routines, status }: { routines: Array<{ id: string; name: string; schedule: string }>; status: 'idle' | 'loading' | 'ready' | 'error' | 'forbidden' }) {
  if (status === 'forbidden') return null
  return <section className="panel-section panel-routines" aria-labelledby="panel-routines-title">
    <div className="panel-section-head"><h2 id="panel-routines-title">Rutinler</h2><Link to="/routines" aria-label="Open routines">Aç <Icon name="chevron" /></Link></div>
    {routines.length ? <ul className="panel-routine-list">{routines.map((routine) => <li key={routine.id}><span className="panel-dot is-healthy" aria-label="enabled" /><strong>{routine.name}</strong><small>{routine.schedule}</small></li>)}</ul>
      : <p className="panel-empty">{status === 'ready' ? 'Etkin rutin yok.' : status === 'error' ? 'Rutinler okunamadı.' : 'Yükleniyor…'}</p>}
  </section>
}

export function PanelSystem({ devices, access, status, now }: { devices: DeviceDetail[]; access: boolean; status: string; now: number }) {
  return <section className="panel-section panel-devices" aria-labelledby="panel-devices-title">
    <div className="panel-section-head"><h2 id="panel-devices-title">Sistem</h2>{access && <Link to="/devices" aria-label="Device details">Detay <Icon name="chevron" /></Link>}</div>
    {!access || status === 'forbidden' ? <p className="panel-empty">Bu cihazda görüntülenemiyor.</p>
      : devices.length ? <div className="panel-device-grid">{devices.map((device) => {
        const tone = panelDeviceTone(device)
        const detail = panelDeviceDetail(device, now)
        return <div key={device.id} className={`is-${tone}`} data-device={device.id} title={detail}>
          <span className={`panel-dot is-${tone}`} aria-hidden="true" /><span><strong>{PANEL_DEVICE_LABEL[device.id] ?? device.name}</strong><small>{detail}</small></span>
        </div>
      })}</div> : <p className="panel-empty">{status === 'error' ? 'Cihaz durumu alınamadı.' : 'Yükleniyor…'}</p>}
  </section>
}

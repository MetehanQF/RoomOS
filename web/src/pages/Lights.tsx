import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties, type KeyboardEvent, type MouseEvent, type ReactNode } from 'react'
import { api } from '../api'
import { useAuthStatus } from '../auth-context'
import { roomosLifecycle } from '../lifecycle'
import { StatusPill } from '../components/Common'
import { Icon } from '../components/Icons'
import { brightnessToPercent, percentToBrightness, temperatureLabel, type LightAction } from '../ui'
import { COLOR_SWATCHES, LIGHT_SECTIONS, clampKelvin, colorMode, displayName, effectLabel, hexToRgb, kelvinValue, previewColor, rgbToHex, stateLine } from '../lights-state'
import { LightsStore, type LightsApi, type LightsSnapshot } from '../lights-store'
import type { ConnectionStatus, Light } from '../types'
import { COMMIT_KEYS, useDraft } from '../light-draft'

const lightsApi: LightsApi = {
  lights: api.lights,
  health: api.health,
  power: api.power,
  brightness: api.brightness,
  color: api.color,
  temperature: api.temperature,
  effect: api.effect,
  allPower: api.allPower,
  mainPower: api.mainPower,
  mainBrightness: api.mainBrightness,
  mainTemperature: api.mainTemperature,
  activateScene: api.activateScene,
}

export type LightsActions = {
  can: (light: Light, action: LightAction) => boolean
  canAllOn: boolean
  canAllOff: boolean
  reason: string | null
  power: (light: Light, on: boolean) => void
  brightness: (light: Light, value: number) => void
  color: (light: Light, rgb: [number, number, number]) => void
  temperature: (light: Light, kelvin: number) => void
  effect: (light: Light, effect: string) => void
  allOn: () => void
  allOff: () => void
  dismiss: () => void
  refresh?: () => void
}

const CONNECTION_LABEL: Record<ConnectionStatus, string> = { connected: 'Live', reconnecting: 'Reconnecting…', offline: 'Offline' }
export function BrightnessSlider({ light, enabled, onCommit }: { light: Light; enabled: boolean; onCommit: (value: number) => void }) {
  const live = Math.max(1, brightnessToPercent(light.brightness))
  const draft = useDraft(live, enabled)
  const commit = () => draft.take((percent) => onCommit(percentToBrightness(percent)))
  return <div className="light-control">
    <div className="light-control-label"><label htmlFor={`brightness-${light.id}`}>Brightness</label><strong>{draft.value}%</strong></div>
    <input id={`brightness-${light.id}`} className="light-range brightness-range" type="range" min={1} max={100} step={1} value={draft.value} disabled={!enabled}
      aria-valuetext={`${draft.value} percent`} style={{ '--fill': `${draft.value}%` } as CSSProperties}
      onChange={(event) => draft.set(Number(event.target.value))} onPointerUp={commit} onPointerCancel={draft.cancel}
      onKeyUp={(event: KeyboardEvent) => { if (COMMIT_KEYS.has(event.key)) commit() }} onBlur={draft.cancel} />
  </div>
}

export function KelvinSlider({ light, enabled, onCommit }: { light: Light; enabled: boolean; onCommit: (value: number) => void }) {
  const { min_kelvin: min, max_kelvin: max } = light.capabilities
  const actual = kelvinValue(light)
  const draft = useDraft(actual ?? min, enabled)
  const shown = draft.dragging || actual !== null ? `${temperatureLabel(draft.value)} · ${draft.value} K` : colorMode(light) === 'colour' ? 'Not in white mode' : 'Value unavailable'
  const commit = () => draft.take((value) => onCommit(clampKelvin(value, light)))
  return <div className="light-control">
    <div className="light-control-label"><label htmlFor={`kelvin-${light.id}`}>White temperature</label><strong>{shown}</strong></div>
    <input id={`kelvin-${light.id}`} className={`light-range kelvin-range ${actual === null && !draft.dragging ? 'is-unset' : ''}`} type="range" min={min} max={max} step={50} value={draft.value} disabled={!enabled}
      aria-valuetext={draft.dragging || actual !== null ? `${draft.value} Kelvin, ${temperatureLabel(draft.value).toLowerCase()}` : 'Not set'}
      onChange={(event) => draft.set(Number(event.target.value))} onPointerUp={commit} onPointerCancel={draft.cancel}
      onKeyUp={(event: KeyboardEvent) => { if (COMMIT_KEYS.has(event.key)) commit() }} onBlur={draft.cancel} />
    <div className="light-range-ends"><span>Warm · {min} K</span><span>Cool · {max} K</span></div>
  </div>
}

export function ColourControls({ light, enabled, onCommit }: { light: Light; enabled: boolean; onCommit: (rgb: [number, number, number]) => void }) {
  const current = colorMode(light) === 'colour' ? light.rgb : null
  const [custom, setCustom] = useState(() => rgbToHex(current ?? [128, 80, 255]))
  const customRgb = hexToRgb(custom)
  const same = (a: readonly number[] | null, b: readonly number[]) => Boolean(a && a.every((channel, index) => Math.abs(channel - b[index]) <= 4))
  return <div className="light-control">
    <p className="light-control-title">Colour</p>
    <div className="swatch-grid" role="group" aria-label="Colour presets">{COLOR_SWATCHES.map((swatch) => {
      const selected = same(current, swatch.rgb)
      return <button key={swatch.name} type="button" className={`swatch ${selected ? 'is-selected' : ''}`} disabled={!enabled} aria-pressed={selected} aria-label={swatch.name} title={swatch.name}
        style={{ '--swatch': `rgb(${swatch.rgb.join(',')})` } as CSSProperties} onClick={() => onCommit(swatch.rgb)}><span aria-hidden="true" /></button>
    })}</div>
    <div className="custom-colour">
      <label className="custom-colour-picker"><input type="color" value={custom} disabled={!enabled} aria-label={`${displayName(light)} custom colour`}
        onChange={(event) => setCustom(event.target.value)} /><span>Custom</span></label>
      <button type="button" className="apply-colour" disabled={!enabled || !customRgb || same(current, customRgb)} onClick={() => customRgb && onCommit(customRgb)}>Apply colour</button>
    </div>
  </div>
}

function EffectChips({ light, enabled, onCommit }: { light: Light; enabled: boolean; onCommit: (effect: string) => void }) {
  return <div className="light-control">
    <p className="light-control-title">Effect</p>
    <div className={`effect-chips ${light.effects.length > 6 ? 'is-many' : ''}`} role="group" aria-label={`${displayName(light)} effects`}>
      {light.effects.map((effect) => {
        const active = light.effect === effect
        return <button key={effect} type="button" className={`effect-chip ${active ? 'is-active' : ''}`} aria-pressed={active} disabled={!enabled || active} onClick={() => onCommit(effect)}>{effectLabel(effect)}</button>
      })}
    </div>
  </div>
}

export function LightDetail({ light, snapshot, actions, onClose }: { light: Light; snapshot: LightsSnapshot; actions: LightsActions; onClose?: () => void }) {
  const reported = colorMode(light)
  const [mode, setMode] = useState<'white' | 'colour'>(() => reported === 'colour' || !light.capabilities.temperature ? 'colour' : 'white')
  // Follow the light's reported mode when it changes; the switch itself never sends anything.
  const [seen, setSeen] = useState(reported)
  if (seen !== reported) {
    setSeen(reported)
    if (reported !== 'unknown') setMode(reported)
  }
  const feedback = snapshot.feedback?.scope === light.id ? snapshot.feedback.value : null
  const pending = snapshot.pending?.lightId === light.id
  const on = light.available && light.on === true
  const caps = light.capabilities
  return <article className="light-detail surface" aria-labelledby={`light-detail-${light.id}`}>
    <header className="light-detail-head">
      <span className="light-preview" style={{ '--preview': previewColor(light) ?? 'transparent' } as CSSProperties} aria-hidden="true" />
      <div><p className="eyebrow">{light.id === 'desk_ambient' ? 'LED strip' : 'Room light'}</p><h2 id={`light-detail-${light.id}`}>{displayName(light)}</h2><p className="light-detail-state">{stateLine(light)}</p></div>
      {light.available && <button type="button" className={`light-power ${on ? 'is-on' : ''}`} disabled={!actions.can(light, 'power')} aria-pressed={on} onClick={() => actions.power(light, !on)}>
        <Icon name="power" /><span>{pending && snapshot.pending?.action === 'power' ? 'Working…' : on ? 'Turn off' : 'Turn on'}</span></button>}
      {onClose && <button type="button" className="sheet-close" onClick={onClose} aria-label="Close">Close</button>}
    </header>
    {!light.available && <p className="light-note">Unavailable · can't control right now.</p>}
    {light.available && !on && <p className="light-note">Turn on to adjust brightness, colour and effects.</p>}
    {on && <>
      {caps.brightness && light.brightness === null && <p className="light-note">Brightness unavailable</p>}
      {caps.brightness && light.brightness !== null && <BrightnessSlider key={`b-${light.id}`} light={light} enabled={actions.can(light, 'brightness')} onCommit={(value) => actions.brightness(light, value)} />}
      {(caps.temperature || caps.color) && <div className="mode-switch" role="group" aria-label="Light mode">
        {caps.temperature && <button type="button" aria-pressed={mode === 'white'} className={mode === 'white' ? 'is-active' : ''} onClick={() => setMode('white')}>White</button>}
        {caps.color && <button type="button" aria-pressed={mode === 'colour'} className={mode === 'colour' ? 'is-active' : ''} onClick={() => setMode('colour')}>Colour</button>}
      </div>}
      {mode === 'white' && caps.temperature && <KelvinSlider key={`k-${light.id}`} light={light} enabled={actions.can(light, 'temperature')} onCommit={(value) => actions.temperature(light, value)} />}
      {mode === 'colour' && caps.color && <ColourControls key={`c-${light.id}`} light={light} enabled={actions.can(light, 'color')} onCommit={(rgb) => actions.color(light, rgb)} />}
      {caps.effects && light.effects.length > 0 && <EffectChips light={light} enabled={actions.can(light, 'effect')} onCommit={(effect) => actions.effect(light, effect)} />}
    </>}
    {on && !pending && ['power', 'brightness', 'color', 'temperature', 'effect'].some((action) => !actions.can(light, action as LightAction)) && <p className="light-permission-note">{actions.reason ?? 'Only supported controls permitted for this device are enabled.'}</p>}
    {feedback && <div className={`light-feedback is-${feedback.tone}`}><div><strong>{feedback.title}</strong>{feedback.detail && <span>{feedback.detail}</span>}</div>
      {feedback.tone !== 'pending' && <button type="button" onClick={actions.dismiss} aria-label="Dismiss result">×</button>}</div>}
  </article>
}

function LightRow({ light, selected, actions, snapshot, onSelect }: { light: Light; selected: boolean; actions: LightsActions; snapshot: LightsSnapshot; onSelect: (light: Light, origin: HTMLButtonElement) => void }) {
  const on = light.available && light.on === true
  const pending = snapshot.pending?.lightId === light.id && snapshot.pending.action === 'power'
  return <li className={`light-row ${selected ? 'is-selected' : ''} ${light.available ? (on ? 'is-on' : 'is-off') : 'is-unavailable'}`} data-light={light.id}>
    <button type="button" className="light-row-select" aria-pressed={selected} aria-label={`${displayName(light)}, ${stateLine(light)}`} onClick={(event: MouseEvent<HTMLButtonElement>) => onSelect(light, event.currentTarget)}>
      <span className="light-preview" style={{ '--preview': previewColor(light) ?? 'transparent' } as CSSProperties} aria-hidden="true" />
      <span className="light-row-text"><strong>{displayName(light)}</strong><span>{stateLine(light)}</span></span>
    </button>
    {light.available && <button type="button" className={`light-row-power ${on ? 'is-on' : ''}`} disabled={!actions.can(light, 'power')} aria-pressed={on}
      aria-label={`${on ? 'Turn off' : 'Turn on'} ${displayName(light)}`} onClick={() => actions.power(light, !on)}>
      <Icon name="power" />{pending && <span>…</span>}</button>}
  </li>
}

export function LightsView({ snapshot, actions, selectedId, onSelect, sheet }: {
  snapshot: LightsSnapshot; actions: LightsActions; selectedId: string | null
  onSelect: (light: Light, origin: HTMLButtonElement) => void; sheet?: ReactNode
}) {
  const lights = snapshot.lights
  const byId = new Map((lights ?? []).map((light) => [light.id, light]))
  const selected = selectedId ? byId.get(selectedId) ?? null : null
  const onCount = lights?.filter((light) => light.available && light.on === true).length ?? 0
  const unavailable = lights?.filter((light) => !light.available).length ?? 0
  const group = snapshot.feedback?.scope === 'group' ? snapshot.feedback.value : null
  const groupPending = snapshot.pending?.lightId === null ? snapshot.pending.action : null
  const live = snapshot.connection === 'connected' && snapshot.trusted && snapshot.control?.home_assistant === 'connected'
  return <section className="page lights-page lights-v2">
    <header className="lights-top">
      <div><p className="eyebrow">Room lighting</p><h1>Lights</h1>
        <p className="lights-summary">{lights ? `${onCount} of ${lights.length} on${unavailable ? ` · ${unavailable} unavailable` : ''}` : snapshot.lightsStatus === 'error' ? 'Light state unavailable' : 'Loading lights…'}{lights && !live ? ' · last known' : ''}</p></div>
      <div className="lights-top-actions">
        <StatusPill status={snapshot.connection === 'connected' ? 'online' : snapshot.connection === 'offline' ? 'offline' : 'stale'} label={CONNECTION_LABEL[snapshot.connection]} />
        <div className="lights-group" role="group" aria-label="All room lights">
          <button type="button" className="group-action group-all-on" disabled={!actions.canAllOn} onClick={actions.allOn}><Icon name="power" />{groupPending === 'all_on' ? 'Turning on…' : 'All on'}</button>
          <button type="button" className="group-action group-all-off" disabled={!actions.canAllOff} onClick={actions.allOff}><Icon name="moon" />{groupPending === 'all_off' ? 'Turning off…' : 'All off'}</button>
        </div>
      </div>
    </header>
    {actions.reason && <p className="lights-reason">{actions.reason}</p>}
    {(snapshot.lightsStatus === 'error' || snapshot.controlStatus === 'error' || snapshot.feedback?.value.tone === 'partial' || snapshot.feedback?.value.tone === 'error') && actions.refresh && <button type="button" className="lights-refresh" disabled={snapshot.refreshing || snapshot.pending !== null} onClick={actions.refresh}>Refresh status</button>}
    {group && <div className={`light-feedback is-${group.tone} lights-group-feedback`}><div><strong>{group.title}</strong>{group.detail && <span>{group.detail}</span>}</div>
      {group.tone !== 'pending' && <button type="button" onClick={actions.dismiss} aria-label="Dismiss result">×</button>}</div>}
    <div className="lights-layout">
      <nav className="lights-list" aria-label="Lights">
        {!lights && <p className="lights-loading">{snapshot.lightsStatus === 'error' ? 'Lights could not be loaded.' : 'Loading lights…'}</p>}
        {lights && LIGHT_SECTIONS.map((section) => {
          const members = section.ids.map((id) => byId.get(id)).filter((light): light is Light => Boolean(light))
          if (!members.length) return null
          return <div key={section.key} className="lights-section"><h2 className="lights-section-title">{section.title}</h2>
            <ul>{members.map((light) => <LightRow key={light.id} light={light} selected={light.id === selectedId} actions={actions} snapshot={snapshot} onSelect={onSelect} />)}</ul></div>
        })}
      </nav>
      <div className="lights-detail-pane">{selected ? <LightDetail key={selected.id} light={selected} snapshot={snapshot} actions={actions} /> : lights && <p className="lights-loading">Select a light.</p>}</div>
    </div>
    {sheet}
    <div className="sr-only" aria-live="polite" aria-atomic="true">{snapshot.announcement && <span key={snapshot.announcement.id}>{snapshot.announcement.text}</span>}</div>
  </section>
}

function usePhone() {
  const query = '(max-width: 640px)'
  const [phone, setPhone] = useState(() => typeof window !== 'undefined' && window.matchMedia(query).matches)
  useEffect(() => {
    const media = window.matchMedia(query)
    const listener = () => setPhone(media.matches)
    media.addEventListener('change', listener)
    return () => media.removeEventListener('change', listener)
  }, [])
  return phone
}

export function LightsPage() {
  const auth = useAuthStatus()
  const scopeKey = (auth?.device?.scopes ?? []).join(' ')
  const store = useMemo(() => new LightsStore(lightsApi, scopeKey ? scopeKey.split(' ') : [], auth?.authenticated === true, roomosLifecycle?.snapshot ?? 'reconnecting'), [scopeKey, auth])
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot)
  const phone = usePhone()
  const [selectedId, setSelectedId] = useState<string | null>('room_main')
  const dialogRef = useRef<HTMLDialogElement>(null)
  const originRef = useRef<HTMLButtonElement | null>(null)

  useEffect(() => {
    if (!roomosLifecycle) { void store.load(); return }
    if (roomosLifecycle.snapshot === 'connected') void store.load()
    const unsubscribeStatus = roomosLifecycle.subscribeStatus((status) => {
      store.setConnection(status)
      if (status === 'offline' && !store.getSnapshot().lights) void store.load()
    })
    const unsubscribeEvents = roomosLifecycle.subscribeEvents((event) => store.handleEvent(event), () => store.load(), { refreshOnEvent: false })
    return () => { unsubscribeStatus(); unsubscribeEvents() }
  }, [store])

  const closeSheet = () => dialogRef.current?.close()
  const trapSheetFocus = (event: KeyboardEvent<HTMLDialogElement>) => {
    if (event.key !== 'Tab') return
    const targets = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), [tabindex="0"]')].filter((element) => element.getClientRects().length)
    const first = targets[0], last = targets.at(-1)
    if (!first || !last) return
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
  }
  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    const onClose = () => { document.body.classList.remove('sheet-open'); originRef.current?.focus() }
    dialog.addEventListener('close', onClose)
    return () => { dialog.removeEventListener('close', onClose); if (dialog.open) dialog.close(); document.body.classList.remove('sheet-open') }
  }, [phone])

  const onSelect = (light: Light, origin: HTMLButtonElement) => {
    setSelectedId(light.id)
    if (phone && dialogRef.current && !dialogRef.current.open) {
      originRef.current = origin
      document.body.classList.add('sheet-open')
      dialogRef.current.showModal()
    }
  }

  const actions: LightsActions = {
    can: (light, action) => store.can(light, action),
    canAllOn: store.canAllOn(),
    canAllOff: store.canAllOff(),
    reason: store.page().reason,
    power: (light, on) => { void store.power(light, on) },
    brightness: (light, value) => { void store.brightness(light, value) },
    color: (light, rgb) => { void store.color(light, rgb) },
    temperature: (light, kelvin) => { void store.temperature(light, kelvin) },
    effect: (light, effect) => { void store.effect(light, effect) },
    allOn: () => { void store.allOn() },
    allOff: () => { void store.allOff() },
    dismiss: () => store.dismiss(),
    refresh: () => { void store.load() },
  }
  const selected = snapshot.lights?.find((light) => light.id === selectedId) ?? null
  const sheet = phone ? <dialog ref={dialogRef} className="light-sheet" onKeyDown={trapSheetFocus} aria-label={selected ? displayName(selected) : 'Light'}>
    {selected && <LightDetail key={selected.id} light={selected} snapshot={snapshot} actions={actions} onClose={closeSheet} />}
  </dialog> : null
  return <LightsView snapshot={snapshot} actions={actions} selectedId={phone ? null : selectedId} onSelect={onSelect} sheet={sheet} />
}

import { useCallback, useMemo, useRef, useState, type CSSProperties } from 'react'
import { api } from '../api'
import { useApi, useRoomOSEvents } from '../hooks'
import { CommandNotice, ErrorPanel, LoadingPanel, PageHeader, RealtimePill, SectionHeading, StatusPill } from '../components/Common'
import { Icon } from '../components/Icons'
import { applyLightEvent, brightnessToPercent, controlAccess, describeGroupResult, expandedLightSections, groupControlEnabled, isCurrentColor, kelvinPosition, lightNameMap, lightVisualState, percentToBrightness, temperatureLabel } from '../ui'
import type { CommandFeedback, Health, Light, RoomOSEvent } from '../types'

const colorPresets = [
  { name: 'Warm white', rgb: [255, 214, 170] as [number, number, number] },
  { name: 'Purple', rgb: [128, 0, 255] as [number, number, number] },
  { name: 'Blue', rgb: [32, 102, 255] as [number, number, number] },
  { name: 'Cyan', rgb: [0, 220, 255] as [number, number, number] },
  { name: 'Red', rgb: [255, 55, 70] as [number, number, number] },
  { name: 'Green', rgb: [40, 220, 115] as [number, number, number] },
]

function rgbToHex(rgb: [number, number, number] | null) {
  return `#${(rgb ?? [128, 80, 255]).map((channel) => channel.toString(16).padStart(2, '0')).join('')}`
}

function BrightnessControl({ light, enabled, busy, onCommit }: { light: Light; enabled: boolean; busy: boolean; onCommit: (value: number) => void }) {
  const live = brightnessToPercent(light.brightness)
  const [draft, setDraft] = useState(live || 50)
  const lastCommitted = useRef(live)
  const commit = () => {
    if (!enabled || busy || draft === lastCommitted.current) return
    lastCommitted.current = draft
    onCommit(percentToBrightness(draft))
  }
  return <div className={`control-block brightness-block ${light.on ? 'is-active' : 'is-idle'}`}><div className="control-label"><span>Brightness</span><strong>{light.on ? draft : 0}%</strong></div><input aria-label={`${light.name} brightness`} className="brightness-slider" type="range" min="1" max="100" value={draft} disabled={!enabled || busy} onChange={(event) => setDraft(Number(event.target.value))} onPointerUp={commit} onKeyUp={commit} style={{ '--brightness-fill': `${light.on ? draft : 0}%` } as CSSProperties} /><div className="range-ends"><span>Dim</span><span>Bright</span></div></div>
}

function TemperatureControl({ light, enabled, busy, onCommit }: { light: Light; enabled: boolean; busy: boolean; onCommit: (value: number) => void }) {
  const { min_kelvin: min, max_kelvin: max } = light.capabilities
  const initial = light.color_temperature_kelvin ?? Math.round((min + max) / 2)
  const [draft, setDraft] = useState(initial)
  const lastCommitted = useRef(initial)
  const commit = () => {
    if (!enabled || busy || draft === lastCommitted.current) return
    lastCommitted.current = draft
    onCommit(draft)
  }
  return <div className="control-block temperature-block"><div className="control-label"><span>White temperature</span><strong>{temperatureLabel(draft)} · {draft} K</strong></div><input aria-label={`${light.name} color temperature`} className="temperature-slider" type="range" min={min} max={max} step="50" value={draft} disabled={!enabled || busy} onChange={(event) => setDraft(Number(event.target.value))} onPointerUp={commit} onKeyUp={commit} style={{ '--kelvin-position': `${kelvinPosition(draft, min, max)}%` } as CSSProperties} /><div className="range-ends"><span>Warm · {min} K</span><span>Cool · {max} K</span></div></div>
}

export function LightCard({ light, health, refresh, initialExpanded = false }: { light: Light; health: Health | null; refresh: () => Promise<void>; initialExpanded?: boolean }) {
  const [feedback, setFeedback] = useState<CommandFeedback | null>(null)
  const [busy, setBusy] = useState(false)
  const [expanded, setExpanded] = useState(initialExpanded)
  const access = controlAccess(light, health)
  const sections = expandedLightSections(light)
  const displayName = light.name
  const glow = light.rgb ? light.rgb.join(',') : light.color_mode === 'color_temp' ? '255,211,155' : '151,91,255'
  const run = async (work: () => Promise<unknown>, success: string) => {
    setBusy(true); setFeedback({ tone: 'pending', title: 'Sending command…' })
    try { await work(); setFeedback({ tone: 'success', title: success }); await refresh() }
    catch (cause) { setFeedback({ tone: 'error', title: 'Command failed', detail: cause instanceof Error ? cause.message : 'Please try again.' }) }
    finally { setBusy(false) }
  }
  const currentMode = !light.available ? 'Unavailable' : !light.on ? 'Off' : light.color_mode === 'color_temp' ? `${light.color_temperature_kelvin ?? ''} K white` : light.rgb ? `RGB ${light.rgb.join(' · ')}` : 'White light'
  const advancedCount = Object.values(sections).filter(Boolean).length

  return <article className={`light-panel surface state-${lightVisualState(light)} ${expanded ? 'is-expanded' : ''}`} style={{ '--light-glow': glow } as CSSProperties}>
    <div className="light-panel-head"><div className="light-identity"><span className="light-icon"><Icon name="bulb" /></span><div><p>Smart light</p><h2>{displayName}</h2></div></div><StatusPill status={!light.available ? 'unknown' : light.on ? 'online' : 'offline'} label={!light.available ? 'Unavailable' : light.on ? 'On' : 'Off'} /></div>
    <div className="light-daily-row"><div className="light-visual"><div className="light-aura" /><div><strong>{currentMode}</strong><span>{light.on ? `${brightnessToPercent(light.brightness)}% brightness` : light.available ? 'Ready' : 'No response'}</span></div></div><button className={`device-power ${light.on ? 'turn-off' : 'turn-on'}`} disabled={!access.power || busy} onClick={() => void run(() => api.power(light.id, !light.on), light.on ? `${displayName} turned off` : `${displayName} turned on`)}><Icon name="power" /><span>{busy ? 'Working…' : light.on ? 'Turn off' : 'Turn on'}</span></button></div>

    {light.capabilities.brightness && <BrightnessControl key={`brightness-${light.brightness}`} light={light} enabled={access.brightness} busy={busy} onCommit={(value) => void run(() => api.brightness(light.id, value), `${displayName} brightness updated`)} />}
    <div className="light-color-summary"><span className="current-color" style={{ '--current-color': `rgb(${glow})` } as CSSProperties} /><span>{light.on ? currentMode : 'Color idle'}</span><div className="capability-row"><span>{light.capabilities.color ? 'Color' : 'White'}</span>{light.capabilities.temperature && <span>{light.capabilities.min_kelvin}–{light.capabilities.max_kelvin} K</span>}</div></div>
    {advancedCount > 0 && <button className="expand-controls" aria-expanded={expanded} aria-controls={`advanced-${light.id}`} onClick={() => setExpanded((value) => !value)}><span>{expanded ? 'Hide controls' : 'More controls'}</span><small>{advancedCount} options</small><span className="expand-chevron">⌄</span></button>}
    {expanded && <div className="advanced-controls" id={`advanced-${light.id}`}>
      {sections.temperature && <TemperatureControl key={`temperature-${light.color_temperature_kelvin}-${light.capabilities.min_kelvin}-${light.capabilities.max_kelvin}`} light={light} enabled={access.temperature} busy={busy} onCommit={(value) => void run(() => api.temperature(light.id, value), `${displayName} white temperature updated`)} />}
      {sections.color && <div className="control-block color-block"><div className="control-label"><span>Color</span><label className={`color-picker ${!access.color || busy ? 'disabled' : ''}`}><input aria-label={`${displayName} custom color`} type="color" value={rgbToHex(light.rgb)} disabled={!access.color || busy} onChange={(event) => { const hex = event.target.value; const rgb: [number, number, number] = [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)]; void run(() => api.color(light.id, rgb), `${displayName} color updated`) }} /><span>Custom</span></label></div><div className="color-presets">{colorPresets.map((preset) => <button key={preset.name} className={`color-preset ${isCurrentColor(light.rgb, preset.rgb) ? 'is-selected' : ''}`} aria-label={`${displayName}: ${preset.name}`} aria-pressed={isCurrentColor(light.rgb, preset.rgb)} title={preset.name} disabled={!access.color || busy} style={{ '--preset-color': `rgb(${preset.rgb.join(',')})` } as CSSProperties} onClick={() => void run(() => api.color(light.id, preset.rgb), `${displayName} set to ${preset.name.toLowerCase()}`)}><span /></button>)}</div></div>}
      {sections.effects && <div className="control-block effect-block"><div className="control-label"><span>Effect</span></div><select aria-label={`${displayName} effect`} value={light.effect ?? ''} disabled={!access.effect || busy} onChange={(event) => { if (event.target.value) void run(() => api.effect(light.id, event.target.value), `${displayName} effect updated`) }}><option value="" disabled>Select an effect</option>{light.effects.map((effect) => <option value={effect} key={effect}>{effect}</option>)}</select></div>}
    </div>}
    <CommandNotice feedback={feedback} onDismiss={() => setFeedback(null)} />
  </article>
}

export function LightsPage() {
  const loader = useCallback(() => api.lights(), [])
  const healthLoader = useCallback(() => api.health(), [])
  const { data: lights, error, loading, refresh, setData } = useApi(loader)
  const { data: health, refresh: refreshHealth } = useApi(healthLoader)
  const [groupFeedback, setGroupFeedback] = useState<CommandFeedback | null>(null)
  const onEvent = useCallback((event: RoomOSEvent) => setData((current) => applyLightEvent(current, event)), [setData])
  const realtime = useRoomOSEvents(onEvent, refresh)
  const canControlAll = groupControlEnabled(health)
  const onCount = useMemo(() => lights?.filter((light) => light.on).length ?? 0, [lights])

  const runGroup = async (on: boolean) => {
    setGroupFeedback({ tone: 'pending', title: on ? 'Turning all lights on…' : 'Turning all lights off…' })
    try { const result = await api.allPower(on); setGroupFeedback(describeGroupResult(result, lightNameMap(lights))); await Promise.all([refresh(), refreshHealth()]) }
    catch (cause) { setGroupFeedback({ tone: 'error', title: 'Group command failed', detail: cause instanceof Error ? cause.message : 'Please try again.' }) }
  }

  return <section className="page lights-page">
    <PageHeader eyebrow="Room lighting" title="Lights" subtitle="Shape the room, one light at a time."><RealtimePill status={realtime} /></PageHeader>
    {error && <ErrorPanel message={error} />}{loading && <LoadingPanel label="Reading light states…" />}
    <article className="group-control surface"><div className="group-copy"><span className="group-icon"><Icon name="lights" /></span><div><p className="eyebrow">All lights</p><h2>{onCount} of {lights?.length ?? 0} on</h2></div></div><div className="group-actions"><button className="group-on" disabled={!canControlAll || groupFeedback?.tone === 'pending'} onClick={() => void runGroup(true)}><Icon name="power" />All on</button><button className="group-off" disabled={!canControlAll || groupFeedback?.tone === 'pending'} onClick={() => void runGroup(false)}><Icon name="power" />All off</button></div><CommandNotice feedback={groupFeedback} onDismiss={() => setGroupFeedback(null)} />{!canControlAll && <div className="locked-strip"><Icon name="system" />Controls locked</div>}</article>
    <SectionHeading icon="bulb" title="Your lights" />
    <div className="lights-grid">{lights?.map((light) => <LightCard key={light.id} light={light} health={health} refresh={refresh} />)}</div>
  </section>
}

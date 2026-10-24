import { useEffect, useState } from 'react'
import { api } from '../api'
import { remaining } from '../alert-insight'
import type { AlertProfile, AlertProfiles } from '../types'

const DEVICES = [['workstation', 'Workstation'], ['primary', 'Raspberry Pi 5'], ['secondary', 'Secondary']] as const
const LABELS: Record<AlertProfile, string> = { normal: 'Normal', gaming: 'Gaming', development: 'Development' }

type Deps = { load: () => Promise<AlertProfiles>; save: (device: keyof AlertProfiles['devices'], profile: AlertProfile, hours: number | null) => Promise<unknown> }
const DEFAULT_DEPS: Deps = { load: () => api.alertProfiles(), save: (device, profile, hours) => api.setAlertProfile(device, profile, hours) }

/** 7K: usage profiles pause utilization warnings only; temperature, disk exhaustion and services never change. */
export function AlertProfilesPanel({ canEdit, deps = DEFAULT_DEPS }: { canEdit: boolean; deps?: Deps }) {
  const [profiles, setProfiles] = useState<AlertProfiles | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [duration, setDuration] = useState<'1' | '4' | '8' | '24' | 'never'>('8')
  useEffect(() => { void deps.load().then(setProfiles).catch(() => setError('Profiles unavailable')) }, [deps])
  const change = async (device: keyof AlertProfiles['devices'], profile: AlertProfile) => {
    setError(null)
    try { await deps.save(device, profile, profile === 'normal' || duration === 'never' ? null : Number(duration)); setProfiles(await deps.load()) } catch { setError('Change failed') }
  }
  return <section className="alert-profiles" aria-label="Usage profiles">
    <h3>Usage profiles</h3>
    <p className="push-settings-note">CPU, GPU and RAM only raise a warning after 6 hours of unbroken high usage. Gaming and Development pause those warnings entirely; temperature, disk exhaustion and service alerts are never paused.</p>
    <label>Return to Normal after <select aria-label="Usage profile duration" value={duration} disabled={!canEdit} onChange={(event) => setDuration(event.target.value as typeof duration)}><option value="1">1 hour</option><option value="4">4 hours</option><option value="8">8 hours</option><option value="24">24 hours</option><option value="never">No automatic reset</option></select></label>
    {!canEdit && <p className="push-settings-note">View only: the operator permission “alerts:profile” is required to change profiles.</p>}
    {error && <p className="push-settings-note is-error" role="alert">{error}</p>}
    {profiles && DEVICES.map(([id, name]) => { const current = profiles.devices[id]; return <div key={id} className="alert-profile-row">
      <span><strong>{name}</strong>{current.until && <small>{LABELS[current.profile]} · {remaining(current.until)} left</small>}</span>
      <div role="group" aria-label={`${name} profile`}>{(Object.keys(LABELS) as AlertProfile[]).map((profile) => <button key={profile} type="button" aria-pressed={current.profile === profile} disabled={!canEdit} onClick={() => void change(id, profile)}>{LABELS[profile]}</button>)}</div>
    </div> })}
  </section>
}

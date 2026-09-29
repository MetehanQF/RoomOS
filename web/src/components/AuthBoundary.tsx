import { type FormEvent, type ReactNode, useEffect, useState } from 'react'
import { ApiError, api } from '../api'
import { authView } from '../auth'
import type { AuthStatus } from '../types'
import { BrandLogo } from './Brand'

export function PairingScreen({ onPaired }: { onPaired: (status: AuthStatus) => void }) {
  const [code, setCode] = useState('')
  const [friendlyName, setFriendlyName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      onPaired(await api.pair(code, friendlyName))
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 429) setError('Too many attempts. Wait a few minutes and try again.')
      else if (cause instanceof ApiError && (cause.status === 400 || cause.status === 422)) setError('That pairing code is invalid or has expired.')
      else setError('RoomOS could not pair this device. Try again.')
    } finally {
      setBusy(false)
    }
  }

  return <main className="pairing-shell">
    <div className="ambient ambient-one" /><div className="ambient ambient-two" />
    <section className="pairing-card surface" aria-labelledby="pairing-title">
      <BrandLogo className="pairing-logo" />
      <p className="eyebrow">Trusted local access</p>
      <h1 id="pairing-title">Pair this device</h1>
      <p className="pairing-copy">Enter the short-lived code created by the RoomOS operator.</p>
      <form onSubmit={(event) => void submit(event)}>
        <label>Pairing code<input name="pairing-code" value={code} onChange={(event) => setCode(event.target.value.toUpperCase())} autoComplete="one-time-code" inputMode="text" placeholder="XXXX-XXXX-XXXX" required minLength={8} maxLength={32} /></label>
        <label>Device name<input name="friendly-name" value={friendlyName} onChange={(event) => setFriendlyName(event.target.value)} autoComplete="off" placeholder="Kitchen tablet" required minLength={1} maxLength={60} /></label>
        {error && <div className="pairing-error" role="alert">{error}</div>}
        <button className="pairing-submit" type="submit" disabled={busy || !code.trim() || !friendlyName.trim()}>{busy ? 'Pairing…' : 'Pair device'}</button>
      </form>
      <small>Pairing is available only while an operator opens a temporary window.</small>
    </section>
  </main>
}

export function AuthBoundary({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    api.authStatus()
      .then((result) => { if (active) setStatus(result) })
      .catch(() => { if (active) setStatus({ authenticated: false, device: null }) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])
  useEffect(() => {
    const requireAuth = () => setStatus({ authenticated: false, device: null })
    window.addEventListener('roomos:auth-required', requireAuth)
    return () => window.removeEventListener('roomos:auth-required', requireAuth)
  }, [])

  const view = authView(loading, status)
  if (view === 'loading') return <main className="auth-loading"><BrandLogo className="auth-loading-logo" /><span>Opening RoomOS…</span></main>
  if (view === 'pairing') return <PairingScreen onPaired={setStatus} />
  return children
}

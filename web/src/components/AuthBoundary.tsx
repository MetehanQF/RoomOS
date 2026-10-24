import { type FormEvent, type ReactNode, useCallback, useEffect, useState } from 'react'
import { ApiError, api } from '../api'
import { authView } from '../auth'
import { roomosLifecycle } from '../lifecycle'
import { AuthContext } from '../auth-context'
import type { AuthStatus } from '../types'
import { BrandLogo } from './Brand'

export function PairingScreen({ onPaired }: { onPaired: (status: AuthStatus) => void }) {
  const [code, setCode] = useState('')
  const [friendlyName, setFriendlyName] = useState('Wall tablet')
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
        <label>Device name<input name="friendly-name" value={friendlyName} onChange={(event) => setFriendlyName(event.target.value)} autoComplete="off" required minLength={1} maxLength={60} /></label>
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
  const [unavailable, setUnavailable] = useState(false)
  const [permissionDenied, setPermissionDenied] = useState(false)

  const verify = useCallback(() => {
    setLoading(true)
    setUnavailable(false)
    setPermissionDenied(false)
    let active = true
    api.authStatus()
      .then((result) => { if (active) setStatus(result) })
      .catch((cause) => {
        if (!active) return
        if (cause instanceof ApiError && cause.status === 401) setStatus({ authenticated: false, device: null })
        else setUnavailable(true)
      })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    let active = true
    api.authStatus()
      .then((result) => { if (active) setStatus(result) })
      .catch((cause) => {
        if (!active) return
        if (cause instanceof ApiError && cause.status === 401) setStatus({ authenticated: false, device: null })
        else setUnavailable(true)
      })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])
  useEffect(() => {
    const requireAuth = () => {
      roomosLifecycle?.stop()
      setStatus({ authenticated: false, device: null })
      setUnavailable(false)
    }
    const denyPermission = () => setPermissionDenied(true)
    // Scopes changed while paired (WebSocket 4409): refresh them quietly, never show pairing
    // or a loading screen. A 401 here is handled by request() via roomos:auth-required.
    let scopeEpoch = 0
    const refreshScopes = () => {
      const epoch = ++scopeEpoch
      api.authStatus()
        .then((result) => { if (epoch === scopeEpoch && result.authenticated) setStatus(result) })
        .catch(() => undefined)
    }
    window.addEventListener('roomos:auth-required', requireAuth)
    window.addEventListener('roomos:permission-denied', denyPermission)
    window.addEventListener('roomos:scopes-changed', refreshScopes)
    return () => {
      scopeEpoch += 1
      window.removeEventListener('roomos:auth-required', requireAuth)
      window.removeEventListener('roomos:permission-denied', denyPermission)
      window.removeEventListener('roomos:scopes-changed', refreshScopes)
    }
  }, [])

  if (unavailable) return <main className="auth-loading auth-recovery" role="alert"><BrandLogo className="auth-loading-logo" /><strong>RoomOS is unavailable</strong><span>Check the private connection, then try again.</span><button onClick={() => { verify() }}>Retry</button></main>
  if (permissionDenied) return <main className="auth-loading auth-recovery" role="alert"><BrandLogo className="auth-loading-logo" /><strong>Permission required</strong><span>This device is paired but cannot open the live RoomOS channel.</span><button onClick={() => { verify() }}>Retry</button></main>
  const view = authView(loading, status)
  if (view === 'loading') return <main className="auth-loading"><BrandLogo className="auth-loading-logo" /><span>Opening RoomOS…</span></main>
  if (view === 'pairing') return <PairingScreen onPaired={(result) => { setStatus(result); roomosLifecycle?.ensureStarted() }} />
  return <AuthContext.Provider value={status}>{children}</AuthContext.Provider>
}

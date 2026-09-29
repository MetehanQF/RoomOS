import type { AuthStatus } from './types'

export type AuthView = 'loading' | 'pairing' | 'dashboard'

export function authView(loading: boolean, status: AuthStatus | null): AuthView {
  if (loading) return 'loading'
  return status?.authenticated ? 'dashboard' : 'pairing'
}

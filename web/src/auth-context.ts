import { createContext, useContext } from 'react'
import type { AuthStatus } from './types'

/** The verified auth status already fetched by AuthBoundary; avoids repeated /auth/status calls. */
export const AuthContext = createContext<AuthStatus | null>(null)

export function useAuthStatus() {
  return useContext(AuthContext)
}

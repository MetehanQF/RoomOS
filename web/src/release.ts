import type { ReleaseInfo } from './types'

export type WebReleaseInfo = {
  release: string
  revision: string
  component: 'web'
}

export const WEB_RELEASE_INFO: WebReleaseInfo = Object.freeze({
  release: import.meta.env.VITE_ROOMOS_RELEASE || 'development',
  revision: import.meta.env.VITE_ROOMOS_REVISION || 'unknown',
  component: 'web',
})

export function shortRevision(revision: string) {
  return revision === 'unknown' ? revision : revision.slice(0, 7)
}

export function releaseMatches(core: ReleaseInfo, web: WebReleaseInfo) {
  return core.release === web.release && core.revision === web.revision
}

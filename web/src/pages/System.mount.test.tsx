// @vitest-environment jsdom

import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  system: vi.fn(async () => undefined),
  version: vi.fn(async () => undefined),
}))

vi.mock('../api', () => ({ api: { system: mocks.system, version: mocks.version } }))
vi.mock('../components/Health', () => ({ HealthSection: () => null }))
vi.mock('../components/Trends', () => ({ TrendsSection: () => null }))
vi.mock('../lifecycle', () => ({
  roomosLifecycle: {
    snapshot: 'connected',
    subscribeStatus: (listener: (status: string) => void) => { listener('connected'); return () => undefined },
    subscribeEvents: (_listener: unknown, refresh?: () => Promise<void>) => {
      void refresh?.()
      return () => undefined
    },
  },
}))

import { MemoryRouter } from 'react-router-dom'
import { SystemPage } from './System'

let root: Root
let container: HTMLDivElement

beforeEach(() => {
  mocks.system.mockClear()
  mocks.version.mockClear()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
})

it('loads system data once when the connected lifecycle refreshes on mount', async () => {
  await act(async () => {
    root.render(<MemoryRouter><SystemPage /></MemoryRouter>)
    await Promise.resolve()
  })

  expect(mocks.system).toHaveBeenCalledTimes(1)
  expect(mocks.version).toHaveBeenCalledTimes(1)
})

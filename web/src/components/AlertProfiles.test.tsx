// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { AlertProfilesPanel } from './AlertProfiles'
import { api } from '../api'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
const profiles = { devices: { workstation: { profile: 'normal' as const, until: null }, primary: { profile: 'normal' as const, until: null }, secondary: { profile: 'normal' as const, until: null } } }

it('selects a time-bounded profile with the scope and is read-only without it', async () => {
  const host = document.createElement('div'); document.body.append(host); const root = createRoot(host)
  const deps = { load: vi.fn(async () => profiles), save: vi.fn(async () => ({})) }
  await act(async () => { root.render(<AlertProfilesPanel canEdit deps={deps} />) })
  const gaming = host.querySelector('[aria-label="Workstation profile"] button:nth-child(2)') as HTMLButtonElement
  await act(async () => { gaming.click() })
  expect(deps.save).toHaveBeenCalledWith('workstation', 'gaming', 8)
  const duration = host.querySelector('[aria-label="Usage profile duration"]') as HTMLSelectElement
  await act(async () => { duration.value = '24'; duration.dispatchEvent(new Event('change', { bubbles: true })) })
  await act(async () => { gaming.click() })
  expect(deps.save).toHaveBeenLastCalledWith('workstation', 'gaming', 24)
  expect(host.textContent).toContain('never paused')
  expect(host.textContent).toContain('after 6 hours')
  await act(async () => { root.render(<AlertProfilesPanel canEdit={false} deps={deps} />) })
  expect((host.querySelector('[aria-label="Primary profile"], [aria-label="Raspberry Pi 5 profile"] button') as HTMLButtonElement).disabled).toBe(true)
  act(() => root.unmount()); host.remove()
})

it('loads default profile dependencies once across its own state renders', async () => {
  const load = vi.spyOn(api, 'alertProfiles').mockResolvedValue(profiles)
  const host = document.createElement('div'); document.body.append(host); const root = createRoot(host)
  await act(async () => { root.render(<AlertProfilesPanel canEdit={false} />) })
  expect(load).toHaveBeenCalledTimes(1)
  await act(async () => { root.render(<AlertProfilesPanel canEdit={false} />) })
  expect(load).toHaveBeenCalledTimes(1)
  act(() => root.unmount()); host.remove(); load.mockRestore()
})

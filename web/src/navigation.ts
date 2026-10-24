import type { IconName } from './components/Icons'

type NavItem = { to: string; label: string; icon: IconName; scope?: string }

const nav: NavItem[] = [
  { to: '/home', label: 'Home', icon: 'home' },
  { to: '/lights', label: 'Lights', icon: 'lights' },
  { to: '/routines', label: 'Routines', icon: 'routines' },
  { to: '/notifications', label: 'Notifications', icon: 'alert' },
  // Shown only to apps that may read devices; /devices itself explains missing access.
  { to: '/devices', label: 'Devices', icon: 'devices', scope: 'devices:read' },
  { to: '/system', label: 'System', icon: 'system' },
]

/** Primary navigation for this app's scopes. */
export function navigationItems(scopes: readonly string[]) {
  return nav.filter((item) => !item.scope || scopes.includes(item.scope))
}

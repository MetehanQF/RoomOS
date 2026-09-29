import type { ReactNode, SVGProps } from 'react'

export type IconName = 'home' | 'lights' | 'system' | 'bulb' | 'server' | 'pulse' | 'power' | 'wifi' | 'thermometer' | 'memory' | 'disk' | 'cpu' | 'chevron'

const paths: Record<IconName, ReactNode> = {
  home: <><path d="m3 11 9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M9 20v-6h6v6"/></>,
  lights: <><path d="M9 18h6"/><path d="M10 22h4"/><path d="M8.2 14.8A7 7 0 1 1 15.8 14.8C14.7 15.6 14 16.6 14 18h-4c0-1.4-.7-2.4-1.8-3.2Z"/></>,
  system: <><rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01"/></>,
  bulb: <><path d="M9 18h6M10 22h4"/><path d="M8.2 14.8A7 7 0 1 1 15.8 14.8C14.7 15.6 14 16.6 14 18h-4c0-1.4-.7-2.4-1.8-3.2Z"/></>,
  server: <><rect x="4" y="3" width="16" height="18" rx="3"/><path d="M8 8h.01M8 13h.01M8 18h.01M12 8h4M12 13h4M12 18h4"/></>,
  pulse: <path d="M3 12h4l2-7 4 14 2-7h6"/>,
  power: <><path d="M12 2v10"/><path d="M6.3 5.7a8 8 0 1 0 11.4 0"/></>,
  wifi: <><path d="M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0"/><path d="M12 20h.01"/></>,
  thermometer: <><path d="M14 14.8V5a4 4 0 0 0-8 0v9.8a6 6 0 1 0 8 0Z"/><path d="M10 8v9"/></>,
  memory: <><rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 9h6v6H9zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M19 9h3M2 15h3M19 15h3"/></>,
  disk: <><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7"/></>,
  cpu: <><rect x="7" y="7" width="10" height="10" rx="2"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M20 9h3M1 15h3M20 15h3"/></>,
  chevron: <path d="m9 18 6-6-6-6"/>,
}

export function Icon({ name, ...props }: { name: IconName } & SVGProps<SVGSVGElement>) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{paths[name]}</svg>
}

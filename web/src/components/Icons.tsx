import type { ReactNode, SVGProps } from 'react'

export type IconName = 'home' | 'lights' | 'routines' | 'system' | 'alert' | 'bulb' | 'server' | 'pulse' | 'power' | 'wifi' | 'thermometer' | 'memory' | 'disk' | 'cpu' | 'chevron' | 'sun' | 'briefcase' | 'lamp' | 'gamepad' | 'zap' | 'film' | 'moon' | 'play' | 'edit' | 'trash' | 'calendar' | 'clock' | 'camera' | 'cloud' | 'devices' | 'laptop' | 'phone' | 'battery' | 'plug' | 'star' | 'grip' | 'arrowUp' | 'arrowDown' | 'eye' | 'eyeOff' | 'chart' | 'refresh'

const paths: Record<IconName, ReactNode> = {
  home: <><path d="m3 11 9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M9 20v-6h6v6"/></>,
  lights: <><path d="M9 18h6"/><path d="M10 22h4"/><path d="M8.2 14.8A7 7 0 1 1 15.8 14.8C14.7 15.6 14 16.6 14 18h-4c0-1.4-.7-2.4-1.8-3.2Z"/></>,
  routines: <><path d="M4 6h10"/><path d="M4 12h16"/><path d="M10 18h10"/><circle cx="17" cy="6" r="2"/><circle cx="7" cy="18" r="2"/></>,
  system: <><rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01"/></>,
  alert: <><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9Z"/><path d="M10 21h4"/></>,
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
  sun: <><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.66 6.34l1.41-1.41"/></>,
  briefcase: <><rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12h18M10 12v2h4v-2"/></>,
  lamp: <><path d="M8 4h8l3 8H5l3-8Z"/><path d="M12 12v7M8 22h8M9 19h6"/></>,
  gamepad: <><path d="M8.5 7h7a5.5 5.5 0 0 1 5.2 7.3l-1 2.8a2.5 2.5 0 0 1-4.1 1l-1.5-1.4H9.9l-1.5 1.4a2.5 2.5 0 0 1-4.1-1l-1-2.8A5.5 5.5 0 0 1 8.5 7Z"/><path d="M7 11v4M5 13h4M16 12h.01M18 14h.01"/></>,
  zap: <path d="m13 2-9 12h8l-1 8 9-12h-8l1-8Z"/>,
  film: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M17 9h4M3 15h4M17 15h4"/></>,
  moon: <path d="M20 15.4A8.5 8.5 0 0 1 8.6 4a8.5 8.5 0 1 0 11.4 11.4Z"/>,
  play: <path d="m8 5 11 7-11 7V5Z"/>,
  edit: <><path d="M4 20h4L19 9l-4-4L4 16v4Z"/><path d="m13.5 6.5 4 4"/></>,
  trash: <><path d="M4 7h16M9 7V4h6v3M7 7l1 14h8l1-14M10 11v6M14 11v6"/></>,
  calendar: <><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4M16 3v4M3 10h18"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  camera: <><rect x="3" y="6" width="18" height="14" rx="3"/><path d="m8 6 1.5-2h5L16 6"/><circle cx="12" cy="13" r="4"/></>,
  cloud: <path d="M7 18h10a4 4 0 0 0 .7-7.9A6 6 0 0 0 6.2 8.7 4.7 4.7 0 0 0 7 18Z"/>,
  devices: <><rect x="2" y="5" width="13" height="10" rx="2"/><path d="M5 19h7"/><rect x="16" y="8" width="6" height="12" rx="1.5"/><path d="M19 17h.01"/></>,
  laptop: <><rect x="4" y="5" width="16" height="11" rx="2"/><path d="M2 19h20"/></>,
  phone: <><rect x="7" y="2" width="10" height="20" rx="2.5"/><path d="M11 18h2"/></>,
  battery: <><rect x="2" y="7" width="17" height="10" rx="2.5"/><path d="M22 11v2"/><path d="M6 11v2"/></>,
  plug: <><path d="M9 2v5M15 2v5"/><path d="M6 7h12v4a6 6 0 0 1-12 0V7Z"/><path d="M12 17v5"/></>,
  star: <path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9Z"/>,
  grip: <><circle cx="9" cy="6" r="1"/><circle cx="15" cy="6" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="9" cy="18" r="1"/><circle cx="15" cy="18" r="1"/></>,
  arrowUp: <path d="M12 19V5M5 12l7-7 7 7"/>,
  arrowDown: <path d="M12 5v14M19 12l-7 7-7-7"/>,
  eye: <><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></>,
  eyeOff: <><path d="M3 3l18 18"/><path d="M10.6 5.1A10.5 10.5 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.6 6.6A17 17 0 0 0 2 12s3.6 7 10 7a9.6 9.6 0 0 0 4.2-.9"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></>,
  chart: <><path d="M3 3v18h18"/><path d="m7 15 4-5 3 3 5-7"/></>,
  refresh: <><path d="M20 11a8 8 0 0 0-14.9-3.5L3 10"/><path d="M3 4v6h6"/><path d="M4 13a8 8 0 0 0 14.9 3.5L21 14"/><path d="M21 20v-6h-6"/></>,
}

export function Icon({ name, ...props }: { name: IconName } & SVGProps<SVGSVGElement>) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{paths[name]}</svg>
}

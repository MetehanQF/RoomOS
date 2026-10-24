// RoomOS Panel Mode acceptance on the isolated candidate stack (synthetic HA, synthetic phone
// reports, stubbed Wake Lock). NOT a real device. Started by run.mjs (`npm run accept:panel`).
import fs from 'node:fs/promises'
import { execFileSync } from 'node:child_process'
import puppeteer from 'puppeteer-core'

export async function accept({ label, base, haIp, sessions: S, outDir: out, chrome, coreContainer }) {
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const waitSel = (page, sel, opts = {}) => page.waitForSelector(sel, opts).then((h) => { h?.dispose(); return h })
const now = () => Date.now()
const result = { label, checks: {}, errors: [], console: [], nonGet: [], metrics: {}, screenshots: [] }
const check = (name, ok, detail = null) => { result.checks[name] = { ok: !!ok, detail }; console.log(ok ? 'PASS' : 'FAIL', name, detail ? JSON.stringify(detail) : '') }
const haStats = async () => (await fetch(`http://${haIp}:8123/_stats`)).json()
const VIEWPORTS = {
  phone2: { name: 'phone2-portrait', width: 360, height: 800, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },        // 720×1600 panel
  phone2Land: { name: 'phone2-landscape', width: 800, height: 360, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },
  handset: { name: 'handset', width: 412, height: 915, isMobile: true, hasTouch: true, deviceScaleFactor: 2.625 },
  tab: { name: 'tab-landscape', width: 1280, height: 800, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },
  desktop: { name: 'desktop', width: 1440, height: 900 },
}
const browser = await puppeteer.launch({ executablePath: chrome, headless: true, acceptInsecureCerts: true, args: ['--no-sandbox', '--ignore-certificate-errors'] })

async function client(key, who, path = '/panel', { wake = true, settings = null } = {}) {
  const viewport = VIEWPORTS[key]
  const ctx = await browser.createBrowserContext(); const page = await ctx.newPage()
  await page.setViewport(viewport); await page.emulateTimezone('Europe/Istanbul')
  await ctx.setCookie({ name: 'roomos_session', value: S[who].token, domain: '127.0.0.1', path: '/api', secure: true, httpOnly: true, sameSite: 'Strict' },
    { name: 'roomos_csrf', value: S[who].csrf, domain: '127.0.0.1', path: '/', secure: true, sameSite: 'Strict' })
  const rec = { key, name: viewport.name, page, req: {}, posts: [], ws: [], expectErrors: false }
  await page.evaluateOnNewDocument((wake, settings) => {
    window.__hidden = false
    Object.defineProperty(document, 'visibilityState', { get: () => (window.__hidden ? 'hidden' : 'visible') })
    Object.defineProperty(document, 'hidden', { get: () => window.__hidden })
    window.__wake = { requests: 0, active: 0 }
    if (wake) Object.defineProperty(navigator, 'wakeLock', { configurable: true, value: { request: async () => {
      window.__wake.requests += 1; window.__wake.active += 1
      const listeners = []
      const sentinel = { released: false, addEventListener: (_n, l) => listeners.push(l), release: async () => { if (!sentinel.released) { sentinel.released = true; window.__wake.active -= 1; listeners.forEach((l) => l()) } } }
      document.addEventListener('visibilitychange', () => { if (window.__hidden) void sentinel.release() }) // real API: lost when hidden
      return sentinel
    } } })
    else Object.defineProperty(navigator, 'wakeLock', { configurable: true, value: undefined })
    if (settings && !localStorage.getItem('roomos:panel-settings:v2')) localStorage.setItem('roomos:panel-settings:v2', JSON.stringify(settings))
  }, wake, settings)
  await page.setRequestInterception(true)
  page.on('request', (req) => {
    const u = new URL(req.url())
    if (req.method() !== 'GET') {
      if (u.pathname === '/api/v1/devices/self/telemetry') rec.posts.push(now())
      else result.nonGet.push(`${viewport.name} ${req.method()} ${u.pathname}`)
      return req.continue()
    }
    if (u.pathname.startsWith('/api/')) (rec.req[u.pathname] ??= []).push(now())
    req.continue()
  })
  page.on('response', (r) => { const p = new URL(r.url()).pathname; if (p.startsWith('/api/') && r.status() >= 400 && !rec.expectErrors && p !== '/api/v1/devices/self/telemetry') result.errors.push(`${viewport.name}: API ${r.status()} ${p}`) })
  page.on('pageerror', (e) => result.errors.push(`${viewport.name}: ${e.message}`))
  page.on('console', (m) => { if (m.type() === 'error' && !rec.expectErrors) result.console.push(`${viewport.name}: ${m.text()}`) })
  const cdp = await page.createCDPSession(); await cdp.send('Network.enable'); await cdp.send('Performance.enable'); rec.cdp = cdp
  cdp.on('Network.webSocketCreated', () => rec.ws.push(now()))
  rec.frames = []
  cdp.on('Network.webSocketFrameReceived', (e) => { try { const type = JSON.parse(e.response.payloadData).type; if (type) rec.frames.push({ t: now(), type }) } catch { /* not JSON */ } })
  if (path) await page.goto(base + path, { waitUntil: 'domcontentloaded' })
  return rec
}
const count = (rec, path, from = 0, to = Infinity) => (rec.req[path] ?? []).filter((t) => t >= from && t < to).length
const rest = (rec, from = 0) => Object.values(rec.req).flat().filter((t) => t >= from).length
const overflow = (rec) => rec.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
const small = (rec) => rec.page.evaluate(() => [...document.querySelectorAll('.panel-page button, .panel-page a, dialog button')]
  .filter((el) => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden')
  .map((el) => { const r = el.getBoundingClientRect(); return { t: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 30), w: Math.round(r.width), h: Math.round(r.height) } })
  .filter((r) => r.w > 0 && (r.w < 48 || r.h < 48)))
const smallNotifications = (rec) => rec.page.evaluate(() => [...document.querySelectorAll('.notifications-page button, .notifications-page a, .notifications-page select')]
  .filter((el) => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden')
  .map((el) => { const r = el.getBoundingClientRect(); return { t: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 30), w: Math.round(r.width), h: Math.round(r.height) } })
  .filter((r) => r.w > 0 && (r.w < 48 || r.h < 48)))
const panelState = (rec) => rec.page.evaluate(() => {
  const power = [...document.querySelectorAll('.panel-power')]
  return { mainGroups: document.querySelectorAll('[data-group="main_room_lights"]').length,
    ambientCards: document.querySelectorAll('[data-light="desk_ambient"]').length,
    individualRoomCards: document.querySelectorAll('[data-light="room_main"],[data-light="room_2"],[data-light="room_3"],[data-light="room_4"]').length,
    power: power.length, enabledPower: power.filter((b) => !b.disabled).length,
    stale: Boolean(document.querySelector('.panel-stale')), devices: [...document.querySelectorAll('.panel-device-grid [data-device]')].map((d) => d.dataset.device),
    scenesEnabled: [...document.querySelectorAll('.panel-scenes button')].filter((b) => !b.disabled).length, theme: document.querySelector('.panel-page')?.dataset.theme,
    cards: [...document.querySelectorAll('[data-panel-card]')].map((el) => el.dataset.panelCard), compact: document.querySelector('.panel-page')?.classList.contains('is-compact'),
    nav: Boolean(document.querySelector('.navigation')), path: location.pathname, wake: window.__wake }
})
const waitFor = async (fn, ms, step = 500) => { const end = now() + ms; let v; while (now() < end) { v = await fn(); if (v) return v; await sleep(step) } return v }
const metrics = async (rec) => {
  // A heap snapshot forces a full V8 + Blink collection (collectGarbage alone leaves DOM garbage).
  const sink = () => undefined; rec.cdp.on('HeapProfiler.addHeapSnapshotChunk', sink); await rec.cdp.send('HeapProfiler.takeHeapSnapshot', { reportProgress: false }); rec.cdp.off('HeapProfiler.addHeapSnapshotChunk', sink)
  const m = Object.fromEntries((await rec.cdp.send('Performance.getMetrics')).metrics.map((x) => [x.name, x.value])); return { heapKB: Math.round(m.JSHeapUsedSize / 1024), nodes: m.Nodes, listeners: m.JSEventListeners, documents: m.Documents } }
async function shot(rec, name, fullPage = true) { const path = `${out}/shots/${label}-${name}-${rec.name}.png`; await rec.page.screenshot({ path, fullPage }); result.screenshots.push(path) }
await fs.mkdir(`${out}/shots`, { recursive: true })
let seq = Date.now()
let steadyWs = 0
const nativePost = async (battery) => fetch(`${base}/api/v1/native/devices/telemetry`, { method: 'POST', headers: { Authorization: `Bearer ${S.handsetnative.token}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ schema_version: 1, sequence: ++seq, measured_at: new Date().toISOString(), next_report_seconds: 900, sensors: { battery_percent: battery, charging: false, network_type: 'wifi', network_connected: true } }) }).then((r) => r.status)

try {
  check('Phone native report accepted (feeds the Phone tile)', (await nativePost(57)) === 202)
  const before = await haStats()
  // 1. Phone 2 portrait with CPU throttling ≈ an old phone: first useful paint.
  const phone2 = await client('phone2', 'phone2panel', null)
  await phone2.cdp.send('Emulation.setCPUThrottlingRate', { rate: 4 })
  const t0 = now()
  await phone2.page.goto(base + '/panel', { waitUntil: 'domcontentloaded' })
  await waitFor(() => phone2.page.evaluate(() => document.querySelectorAll('.panel-power:not([disabled])').length === 2), 20_000, 200)
  result.metrics.phone2Throttled4xReadyMs = now() - t0
  await phone2.cdp.send('Emulation.setCPUThrottlingRate', { rate: 1 })
  await sleep(4000)
  const s = await panelState(phone2)
  check('Phone 2: panel ready under 4× CPU throttling within 6 s', result.metrics.phone2Throttled4xReadyMs < 6000, { ms: result.metrics.phone2Throttled4xReadyMs })
  check('Phone 2: one main group + one ambient card, 8 scenes, live, normal nav hidden', s.mainGroups === 1 && s.ambientCards === 1 && s.individualRoomCards === 0 && s.power === 2 && s.enabledPower === 2 && s.scenesEnabled === 8 && !s.stale && !s.nav, s)
  check('Phone 2: Primary, Secondary, Workstation, Phone tiles', JSON.stringify(s.devices) === JSON.stringify(['primary', 'secondary', 'workstation', 'phone']), s.devices)
  const boot = { lights: count(phone2, '/api/v1/lights'), health: count(phone2, '/api/v1/health'), system: count(phone2, '/api/v1/system'), devices: count(phone2, '/api/v1/devices'), layout: count(phone2, '/api/v1/panel/layout'), ws: phone2.ws.length }
  result.metrics.bootstrapRequests = { ...boot, all: Object.fromEntries(Object.entries(phone2.req).map(([p, t]) => [p, t.length])) }
  check('Phone 2: bootstrap deduplicated (=1 /lights, =1 /health, 0 /system, 1 /devices, 1 layout, 1 WebSocket)', boot.lights === 1 && boot.health === 1 && boot.system === 0 && boot.devices === 1 && boot.layout === 1 && boot.ws === 1, boot)
  check('Phone 2: no horizontal overflow', (await overflow(phone2)) <= 0)
  const pill = await phone2.page.evaluate(() => { const p = document.querySelector('.panel-header .status-pill'); const r = p?.getBoundingClientRect(); return { w: r ? Math.round(r.width) : 0, label: p?.textContent } })
  check('Phone 2: live/offline state visible in the header', pill.w >= 20 && pill.label === 'Live', pill)
  check('Phone 2: every visible control ≥ 48 px', (await small(phone2)).length === 0, await small(phone2))
  check('Phone 2: alert indicator is compact, visible, and uses existing alert bootstrap', await phone2.page.evaluate(() => {
    const el = document.querySelector('.panel-alert-indicator'); const r = el?.getBoundingClientRect()
    return Boolean(el && r && r.width >= 48 && r.height >= 48 && el.textContent?.trim() === '1')
  }))
  const handsettile = await phone2.page.evaluate(() => document.querySelector('[data-device="phone"]')?.textContent)
  check('Phone 2: Phone tile shows the phone report honestly', /PhoneOnline · battery 57%/.test(handsettile ?? ''), handsettile)
  await shot(phone2, 'day')

  // Device-bound personalization: draft preview is non-writing; Save persists one document,
  // reload reads it once, and Reset deletes it without touching HA.
  await phone2.page.click('button[aria-label="Düzeni düzenle"]'); await waitSel(phone2.page, '.panel-layout-editor')
  await phone2.page.click('button[aria-label="Ana Oda Işıkları aşağı taşı"]')
  await phone2.page.click('button[aria-label="Rutinler kartını gizle"]')
  await phone2.page.click('button[aria-label="Kompakt düzen"]')
  check('Phone 2 editor: preview reflects order, visibility and compact density before writing', await phone2.page.evaluate(() => {
    const preview = document.querySelector('[data-layout-preview]'); const text = preview?.textContent ?? ''
    return preview?.getAttribute('data-density') === 'compact' && text.indexOf('Ambiyans Işığı') < text.indexOf('Ana Oda Işıkları') && !text.includes('Rutinler')
  }))
  check('Phone 2 editor portrait: no overflow and all touch targets ≥48 px', (await overflow(phone2)) <= 0 && (await small(phone2)).length === 0, { overflow: await overflow(phone2), small: await small(phone2) })
  await shot(phone2, 'layout-editor')
  const writesBeforeSave = result.nonGet.length
  await phone2.page.click('[data-save-layout]'); await waitSel(phone2.page, 'dialog[open]', { hidden: true })
  let personalized = await panelState(phone2)
  check('Phone 2 layout save: one PUT applies ordered hidden compact layout', result.nonGet.length === writesBeforeSave + 1 && personalized.compact && JSON.stringify(personalized.cards) === JSON.stringify(['ambient', 'main_lights', 'scenes', 'system']), personalized)
  await phone2.page.reload({ waitUntil: 'domcontentloaded' })
  personalized = await waitFor(async () => { const state = await panelState(phone2); return state.compact && JSON.stringify(state.cards) === JSON.stringify(['ambient', 'main_lights', 'scenes', 'system']) ? state : null }, 10_000)
  check('Phone 2 layout reload: saved device layout returns', personalized.compact && JSON.stringify(personalized.cards) === JSON.stringify(['ambient', 'main_lights', 'scenes', 'system']), personalized)
  await phone2.page.click('button[aria-label="Düzeni düzenle"]'); await waitSel(phone2.page, '.panel-layout-editor')
  await phone2.page.click('[data-reset-layout]'); await waitSel(phone2.page, 'dialog[open]', { hidden: true })
  const reset = await panelState(phone2)
  check('Phone 2 layout reset: one DELETE restores defaults', !reset.compact && JSON.stringify(reset.cards) === JSON.stringify(['main_lights', 'ambient', 'scenes', 'routines', 'system']), reset)
  steadyWs = phone2.ws.length

  // 2. Other viewports (each with its own session).
  const land = await client('phone2Land', 'phone2view')
  const handset = await client('handset', 'handsetpwa')
  const tab = await client('tab', 'tabpwa')
  const desk = await client('desktop', 'handsetpwa')
  await sleep(6000)
  for (const c of [land, handset, tab, desk]) {
    const st = await panelState(c)
    check(`${c.name}: panel renders, no overflow, grouped controls enabled`, st.mainGroups === 1 && st.ambientCards === 1 && st.individualRoomCards === 0 && st.enabledPower === 2 && (await overflow(c)) <= 0, { overflow: await overflow(c), enabled: st.enabledPower })
    check(`${c.name}: controls ≥ 48 px`, (await small(c)).length === 0, await small(c))
    await shot(c, 'day')
  }
  await land.page.click('button[aria-label="Show clock only"]')
  check('Phone 2 landscape Ambient: visible clock and no horizontal overflow', await land.page.evaluate(() => Boolean(document.querySelector('.ambient-clock time')) && document.documentElement.scrollWidth <= document.documentElement.clientWidth))
  await shot(land, 'ambient-landscape', false)
  await land.page.click('.ambient-screen')

  // 2b. Smart Alerts center on every target class. One initial read, the shared socket,
  // no polling, and the same 48 px/overflow gates as the panel.
  const phone2Notice = await client('phone2', 'phone2view', '/notifications')
  const noticeClients = [phone2Notice, land, handset, tab, desk]
  const noticeWsBaseline = new Map(noticeClients.slice(1).map((c) => [c, c.ws.length]))
  await Promise.all(noticeClients.slice(1).map((c) => c.page.goto(base + '/notifications', { waitUntil: 'domcontentloaded' })))
  await Promise.all(noticeClients.map((c) => waitSel(c.page, '.notification-item', { timeout: 10_000 })))
  const noticeSteady = now()
  await sleep(3000)
  for (const c of noticeClients) {
    const state = await c.page.evaluate(() => ({ items: document.querySelectorAll('.notification-item').length,
      active: document.querySelector('.notification-signal strong')?.textContent,
      nav: Boolean(document.querySelector('.navigation')) }))
    check(`${c.name}: Notification Center renders two lifecycles without overflow`, state.items === 2 && state.active === '1' && (await overflow(c)) <= 0, state)
    check(`${c.name}: Notification Center controls ≥ 48 px`, (await smallNotifications(c)).length === 0, await smallNotifications(c))
    const noticeWs = c === phone2Notice ? c.ws.length : c.ws.length - noticeWsBaseline.get(c)
    check(`${c.name}: one notifications GET, one shared WebSocket, no polling`, count(c, '/api/v1/notifications') === 1 && noticeWs === 1 && rest(c, noticeSteady) === 0,
      { get: count(c, '/api/v1/notifications'), notificationWs: noticeWs, totalOpenedWs: c.ws.length, steadyRest: rest(c, noticeSteady) })
    await shot(c, 'notification-center')
  }
  await phone2Notice.page.close()
  await Promise.all([land, handset, tab, desk].map((c) => c.page.goto(base + '/panel', { waitUntil: 'domcontentloaded' })))

  // 3. Steady state: two minutes, no polling (only the 1/min PWA telemetry POST).
  const t1 = now()
  await sleep(120_000)
  check('Phone 2: 0 REST GETs in 120 s steady state', rest(phone2, t1) === 0, { rest: rest(phone2, t1) })
  check('Phone 2: telemetry at most 1/min alongside the panel (no burst)', phone2.posts.filter((t) => t >= t1).length <= 3, { posts: phone2.posts.filter((t) => t >= t1).length })
  check('Phone 2: no new WebSocket during 120 s steady state', phone2.ws.length === steadyWs, { createdSinceReload: phone2.ws.length - steadyWs, totalCreated: phone2.ws.length })

  // 4. Wake Lock: opt-in, survives hide/show, released on exit.
  await phone2.page.click('button[aria-label="Panel settings"]')
  await waitSel(phone2.page, '.panel-settings')
  await phone2.page.evaluate(() => [...document.querySelectorAll('.panel-toggle')].find((b) => b.textContent.includes('Keep screen on')).click())
  await sleep(500)
  let w = (await panelState(phone2)).wake
  check('Wake Lock: acquired once when enabled', w.requests === 1 && w.active === 1, w)
  await phone2.page.evaluate(() => { window.__hidden = true; document.dispatchEvent(new Event('visibilitychange')) })
  await sleep(300); w = (await panelState(phone2)).wake
  check('Wake Lock: lost while hidden (no re-request in background)', w.active === 0 && w.requests === 1, w)
  await phone2.page.evaluate(() => { window.__hidden = false; document.dispatchEvent(new Event('visibilitychange')) })
  await sleep(500); w = (await panelState(phone2)).wake
  check('Wake Lock: re-acquired when visible again', w.active === 1 && w.requests === 2, w)
  await shot(phone2, 'settings')
  await phone2.page.click('.panel-settings .sheet-close')
  const noWake = await client('phone2', 'phone2view', '/panel', { wake: false, settings: { theme: 'night', wake: true, launch: false, orientation: 'auto', saverMinutes: 5 } })
  await sleep(5000)
  const nw = await panelState(noWake)
  check('No Wake Lock API: panel fully works, night theme from settings', nw.enabledPower === 2 && nw.theme === 'night', nw)
  await noWake.page.click('button[aria-label="Panel settings"]'); await waitSel(noWake.page, '.panel-settings')
  check('No Wake Lock API: toggle disabled and explained', await noWake.page.evaluate(() => { const b = [...document.querySelectorAll('.panel-toggle')].find((x) => x.textContent.includes('Keep screen on')); return b.disabled && b.textContent.includes('Not supported') }))
  await noWake.page.click('.panel-settings .sheet-close')
  await shot(noWake, 'night')
  await noWake.page.close()

  // 5. Reconnect: stale state is visibly last-known and not controllable.
  phone2.expectErrors = true
  await phone2.cdp.send('Network.emulateNetworkConditions', { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 })
  const offline = await waitFor(async () => { const st = await panelState(phone2); return st.stale && st.enabledPower === 0 && st.scenesEnabled === 0 ? st : null }, 15_000)
  check('Offline: last-known banner, all light and scene controls disabled', Boolean(offline), offline)
  await shot(phone2, 'offline')
  const t2 = now()
  await phone2.cdp.send('Network.emulateNetworkConditions', { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 })
  const back = await waitFor(async () => { const st = await panelState(phone2); return !st.stale && st.enabledPower === 2 && st.scenesEnabled === 8 ? st : null }, 30_000)
  check('Reconnect: live again, controls back', Boolean(back))
  await sleep(3000)
  check('Reconnect: one bounded resync (≤ 12 GETs), one replacement socket', rest(phone2, t2) <= 12 && phone2.ws.length <= steadyWs + 1, { rest: rest(phone2, t2), createdSockets: phone2.ws.length - steadyWs })
  phone2.expectErrors = false

  // 6. Group command failure: one authenticated RoomOS request fans out to the exact four
  // main-room members; partial failure is shown and the UI re-reads authoritative state.
  phone2.expectErrors = true
  const roomMain = () => phone2.page.evaluate(() => document.querySelector('[data-group="main_room_lights"]')?.className)
  const stateBefore = await roomMain()
  await phone2.page.click('[data-group="main_room_lights"] .panel-power')
  const fb = await waitFor(() => phone2.page.evaluate(() => document.querySelector('[data-group="main_room_lights"] .panel-feedback')?.textContent), 15_000)
  const after = await haStats()
  check('Main group: four HA calls, partial failure reported, state not faked', (after.stats.service_calls ?? 0) - (before.stats.service_calls ?? 0) === 4 && Boolean(fb) && (await roomMain()) === stateBefore,
    { calls: (after.stats.service_calls ?? 0) - (before.stats.service_calls ?? 0), feedback: fb })
  check('Main group: one POST from the panel', result.nonGet.filter((x) => x.includes('/lights/main_room_lights/power')).length === 1, result.nonGet)
  await phone2.page.click('[data-group="main_room_lights"] .panel-feedback button').catch(() => undefined)
  const sceneBefore = (await haStats()).stats.service_calls ?? 0
  await phone2.page.evaluate(() => [...document.querySelectorAll('.panel-scenes button')].find((b) => b.textContent.includes('Movie')).click())
  const sceneFb = await waitFor(() => phone2.page.evaluate(() => { const t = document.querySelector('.panel-scene-feedback')?.textContent; return t && !t.includes('Applying') ? t : null }), 20_000)
  const sceneCalls = ((await haStats()).stats.service_calls ?? 0) - sceneBefore
  check('Scene: one activation request, failure reported, no fake "Active"', result.nonGet.filter((x) => x.includes('/scenes/movie/activate')).length === 1 && Boolean(sceneFb) && sceneCalls >= 1
    && !(await phone2.page.evaluate(() => [...document.querySelectorAll('.panel-scenes button')].find((b) => b.textContent.includes('Movie'))?.classList.contains('is-active'))), { sceneFb, sceneCalls })
  phone2.expectErrors = false

  // 7. Ambient detail sheet (effects and advanced controls) opens and closes.
  await phone2.page.click('[data-light="desk_ambient"] .panel-more')
  await waitSel(phone2.page, 'dialog[open] .light-detail')
  const detail = await phone2.page.evaluate(() => ({ title: document.querySelector('dialog[open] h2')?.textContent, power: Boolean(document.querySelector('dialog[open] .light-power')), note: document.querySelector('dialog[open] .light-note')?.textContent }))
  // The synthetic lights are off: the reused LightDetail asks to turn on before brightness/colour (Lights tests cover the sliders).
  check('Ambient sheet: reused LightDetail with power and honest off-state note', Boolean(detail.title) && detail.power && /Turn on to adjust/.test(detail.note ?? ''), detail)
  await shot(phone2, 'light-sheet')
  await phone2.page.click('dialog[open] .sheet-close'); await waitSel(phone2.page, 'dialog[open]', { hidden: true })

  // 8. Read-only device: sees state, cannot control.
  const ro = await client('phone2', 'readonly')
  await sleep(5000)
  const rs = await panelState(ro)
  check('Read-only session: grouped lights visible, every control disabled', rs.mainGroups === 1 && rs.ambientCards === 1 && rs.enabledPower === 0 && rs.scenesEnabled === 0, rs)
  await ro.page.close()

  // 9. Ambient Screen 2.0 and launch-in-panel preference (no new REST channel).
  const ambientFrom = now()
  await phone2.page.click('button[aria-label="Show clock only"]')
  check('Clock view covers the panel', await phone2.page.evaluate(() => { const r = document.querySelector('.panel-saver')?.getBoundingClientRect(); return r && r.width >= 360 && r.height >= 790 }))
  check('Phone 2 Ambient: large clock, at most two live info cards, no HA controls', await phone2.page.evaluate(() => Boolean(document.querySelector('.ambient-screen .ambient-clock time')) && document.querySelectorAll('.ambient-screen .ambient-info').length <= 2 && document.querySelectorAll('.ambient-screen button, .ambient-screen input').length === 0))
  check('Phone 2 Ambient: Istanbul 24-hour clock without AM/PM wrapping', await phone2.page.evaluate(() => /^\d{2}:\d{2}$/.test(document.querySelector('.ambient-clock time')?.textContent ?? '')))
  await shot(phone2, 'clock', false)
  await phone2.page.click('.panel-saver')
  check('Tap returns to the panel', await phone2.page.evaluate(() => !document.querySelector('.panel-saver')))
  await phone2.page.click('button[aria-label="Panel settings"]'); await waitSel(phone2.page, '.ambient-settings')
  await phone2.page.click('[aria-label="Move Light status down"]')
  check('Phone 2 Ambient card order is browser-local', await phone2.page.evaluate(() => JSON.parse(localStorage.getItem('roomos:ambient:v1'))?.cards?.join(',') === 'system,lights'))
  await phone2.page.click('.panel-settings .sheet-close')
  await phone2.page.click('button[aria-label="Show clock only"]')
  check('Phone 2 Ambient reordered card rendered first', await phone2.page.evaluate(() => document.querySelector('.ambient-info:first-child span')?.textContent === 'Sistem'))
  await phone2.page.click('.panel-saver')
  check('Phone 2 Ambient uses existing store, no new API read on open', rest(phone2, ambientFrom) === 0, { requests: rest(phone2, ambientFrom) })
  const launch = await client('phone2', 'phone2view', '/home', { settings: { theme: 'auto', wake: false, launch: true, orientation: 'auto', saverMinutes: 5 } })
  const opened = await waitFor(() => launch.page.evaluate(() => location.pathname === '/panel'), 8000)
  check('Launch preference: a fresh start on /home opens /panel', opened)
  await launch.page.click('button[aria-label="Exit panel mode"]')
  await sleep(1500)
  check('Exit returns to normal RoomOS and stays there', await launch.page.evaluate(() => location.pathname === '/home' && Boolean(document.querySelector('.navigation'))))
  await launch.page.close()

  // 10. Memory: 20 sheet open/close cycles and 10 panel↔lights navigations.
  const m0 = await metrics(phone2); const wake0 = (await panelState(phone2)).wake.requests
  for (let i = 0; i < 20; i++) {
    await phone2.page.click('button[aria-label="Panel settings"]'); await waitSel(phone2.page, '.panel-settings'); await phone2.page.click('.panel-settings .sheet-close'); await waitSel(phone2.page, 'dialog[open]', { hidden: true })
    await phone2.page.click('[data-light="desk_ambient"] .panel-more'); await waitSel(phone2.page, 'dialog[open] .light-detail'); await phone2.page.click('dialog[open] .sheet-close'); await waitSel(phone2.page, 'dialog[open]', { hidden: true })
  }
  for (let i = 0; i < 10; i++) {
    try {
      await phone2.page.click('a[aria-label="All light controls"]')
      if (!(await waitSel(phone2.page, '.lights-v2', { timeout: 3_000 }).catch(() => null))) {
        result.metrics.navRetries = (result.metrics.navRetries ?? 0) + 1 // synthetic click lost right after history.back()
        await phone2.page.click('a[aria-label="All light controls"]'); await waitSel(phone2.page, '.lights-v2', { timeout: 10_000 })
      }
      await phone2.page.goBack(); await waitSel(phone2.page, '.panel-page', { timeout: 10_000 }); await sleep(500)
    } catch (error) {
      result.metrics.navFailure = { iteration: i, error: String(error).slice(0, 120), state: await phone2.page.evaluate(() => { const a = document.querySelector('a[aria-label="All light controls"]'); const r = a?.getBoundingClientRect(); const el = r && document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2); return { path: location.pathname, y: r && Math.round(r.y), hit: el?.outerHTML.slice(0, 140), saver: Boolean(document.querySelector('.panel-saver')), dialog: document.querySelector('dialog')?.open, main: document.querySelector('main')?.className } }) }
      await phone2.page.screenshot({ path: `${out}/shots/${label}-nav-failure.png` })
      throw error
    }
  }
  await sleep(3000)
  const m1 = await metrics(phone2); const wakeAdds = (await phone2.page.evaluate(() => window.__wake.requests)) - wake0
  result.metrics.memory = { before: m0, after: m1, stubWakeAcquires: wakeAdds }
  check('Memory: DOM nodes and listeners unchanged after 50 interactions, heap < 6 MB, no extra socket', Math.abs(m1.listeners - m0.listeners - wakeAdds) <= 5 /* the test's Wake Lock stub adds one listener per acquire */ && Math.abs(m1.nodes - m0.nodes) <= 20 && m1.heapKB < 6144 && phone2.ws.length <= steadyWs + 1, result.metrics.memory)

  // 11. System page feedback loop with three panel-capable clients.
  for (const c of [phone2, handset, desk]) c.req = {}
  const t4 = now()
  await Promise.all([phone2, handset, desk].map((c) => c.page.goto(base + '/system', { waitUntil: 'domcontentloaded' })))
  await sleep(90_000)
  for (const c of [phone2, handset, desk]) {
    const triggers = c.frames.filter((f) => f.t >= t4 && f.type.startsWith('system.')).length
    const sys = count(c, '/api/v1/system', t4)
    // 7D.1-S2 model: bootstrap (2 today: pre-existing double load, unchanged by 7E) plus at most one per system.* event.
    check(`${c.name}: System 90 s, /system ≤ 2 + system events (no loop)`, sys >= 1 && sys <= 2 + triggers, { system: sys, systemEvents: triggers, otherEvents: c.frames.filter((f) => f.t >= t4 && !f.type.startsWith('system.')).length })
  }

  // 12. Regressions: Device Center, Lights, Routines still render.
  for (const [path, sel] of [['/devices', '[data-device="phone_2"]'], ['/lights', '.lights-v2'], ['/routines', 'main'], ['/home', '.dash-header a[href="/panel"]']]) {
    await desk.page.goto(base + path, { waitUntil: 'networkidle2' })
    check(`desktop: ${path} renders`, Boolean(await desk.page.$(sel)))
  }
  const phone2name = await desk.page.goto(base + '/devices', { waitUntil: 'networkidle2' }).then(() => desk.page.evaluate(() => document.querySelector('[data-device="phone_2"] h2')?.textContent))
  check('Device Center: Phone 2 named correctly', phone2name === 'Second phone', phone2name)
} catch (error) {
  result.errors.push(`harness: ${error.stack}`)
} finally {
  const rateLimited = execFileSync('docker', ['logs', coreContainer], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).split('\n').filter((l) => l.includes('rate_limited')).length
  check('Telemetry: 0 × 429 during the whole run', rateLimited === 0, { rateLimited })
  check('no page errors / console errors', result.errors.length === 0 && result.console.length === 0, { errors: result.errors.slice(0, 5), console: result.console.slice(0, 5) })
  check('only expected writes (layout save/reset + one deliberate group command + one scene)', result.nonGet.every((x) => x.endsWith('/panel/layout') || x.endsWith('/lights/main_room_lights/power') || x.endsWith('/scenes/movie/activate')) && result.nonGet.filter((x) => x.endsWith('/panel/layout')).length === 2 && result.nonGet.length === 4, result.nonGet)
  const failed = Object.entries(result.checks).filter(([, v]) => !v.ok).map(([k]) => k)
  result.summary = { passed: Object.keys(result.checks).length - failed.length, failed }
  await fs.writeFile(`${out}/accept-${label}.json`, JSON.stringify(result, null, 2))
  console.log('SUMMARY', JSON.stringify(result.summary))
  await browser.close()
}
return result
}

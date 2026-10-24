import fs from 'node:fs/promises'
import path from 'node:path'
import puppeteer from 'puppeteer-core'

const base = process.env.ROOMOS_WEB_BASE ?? 'http://127.0.0.1:4173'
const chrome = process.env.ROOMOS_ACCEPT_CHROME
const out = process.env.ROOMOS_ACCEPT_OUT ?? '/tmp/roomos-smart-automation-accept'
if (!chrome) throw new Error('ROOMOS_ACCEPT_CHROME is required')
await fs.mkdir(out, { recursive: true })

const now = '2026-10-19T10:00:00Z'
const routine = { id: `rt_${'1'.repeat(32)}`, name: 'Weeknight', enabled: true, managed: false, trigger: { type: 'time', time: '22:30', days: ['mon','tue','wed','thu','fri'] }, action: { type: 'activate_scene', scene_id: 'night' }, created_at: now, updated_at: now, last_run: null, last_result: null }
const automation = { schema_version: 1, id: `au_${'1'.repeat(32)}`, name: 'Workstation heat guard', description: 'Dim the room when Workstation stays hot.', enabled: false, trigger: { type: 'device_health', device_id: 'workstation', metric: 'temperature', operator: 'above', value: 78, for_seconds: 300 }, conditions: null, actions: [{ type: 'scene', scene_id: 'night' }], cooldown_seconds: 900, failure_policy: 'stop_on_failure', created_at: now, updated_at: now, last_triggered: null, last_outcome: null }
const catalog = { automations: [automation], timezone: 'Europe/Istanbul', storage_status: 'ok', can_write: true, can_enable: true, limits: { condition_depth: 3, conditions: 20, actions: 8, execution_seconds: 15 }, targets: { lights: [{ id: 'desk_ambient', name: 'Desk ambient' }, { id: 'main_room_lights', name: 'Room lights' }], devices: [{ id: 'workstation', name: 'Workstation', metrics: ['online','cpu','ram','disk','temperature'] }] } }
const responses = {
  '/api/v1/auth/status': { authenticated: true, device: { id: 'acceptance', friendly_name: 'Synthetic browser', scopes: ['lights:read','lights:write','system:read','activity:read','routines:read','routines:write'] } },
  '/api/v1/health': { status: 'ok', home_assistant: 'connected', live_connection_enabled: true, control_enabled: true, control_policy: 'production', writable_device_ids: [], writable_actions: [], writable_permissions: {}, alerts: { status: 'operational', active_count: 0, highest_severity: null }, notifications: { status: 'healthy', configured_providers: 0, configured_destinations: 0, pending_count: 0, failed_count: 0, external_attempts: 0, external_deliveries: 0, last_delivery: null } },
  '/api/v1/system/health': { overall: 'healthy', checked_at: now, components: [{ id: 'automations', name: 'Automations', status: 'healthy', critical: true, summary: 'Scheduler running', details: [], checked_at: now }] },
  '/api/v1/routines': { routines: [routine], timezone: 'Europe/Istanbul', storage_status: 'ok', can_write: true, can_run: true, ha_trigger_ids: [], scenes: [{ id: 'normal', name: 'Normal' }, { id: 'night', name: 'Night' }, { id: 'all_off', name: 'All off' }] },
  '/api/v1/automations': catalog,
  '/api/v1/automations/history': { items: [], next_cursor: null },
  '/api/v1/activity': { activity: [], execution_storage_status: 'ok', total: 0, limit: 100, offset: 0, storage_status: 'ok' },
}

const browser = await puppeteer.launch({ executablePath: chrome, headless: true, acceptInsecureCerts: true, args: ['--no-sandbox', '--ignore-certificate-errors'] })
const results = []
try {
  for (const viewport of [{ name: 'desktop', width: 1440, height: 1000 }, { name: 'phone2', width: 360, height: 800 }]) {
    const page = await browser.newPage(); await page.setViewport(viewport)
    await page.evaluateOnNewDocument(() => {
      class AcceptanceSocket {
        readyState = 0; onopen = null; onmessage = null; onclose = null; onerror = null
        constructor() { setTimeout(() => { this.readyState = 1; this.onopen?.(new Event('open')) }, 0) }
        close() { this.readyState = 3 }
      }
      Object.assign(AcceptanceSocket, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 })
      window.WebSocket = AcceptanceSocket
    })
    const consoleErrors = [], unknownApi = []; page.on('console', (message) => { if (message.type() === 'error' && !message.text().includes('WebSocket')) consoleErrors.push(message.text()) })
    await page.setRequestInterception(true)
    page.on('request', (request) => {
      const url = new URL(request.url()); const key = Object.keys(responses).find((item) => url.pathname === item)
      if (key) return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify(responses[key]) })
      if (url.pathname === '/api/v1/events') return request.abort()
      if (url.pathname.startsWith('/api/')) { unknownApi.push(url.pathname); return request.respond({ status: 404, contentType: 'application/json', body: '{}' }) }
      request.continue()
    })
    await page.goto(`${base}/routines`, { waitUntil: 'networkidle0' })
    await page.waitForSelector('.smart-section', { timeout: 10_000 }).catch(async () => { throw new Error(`Smart section missing; unknown=${unknownApi.join(',')}; body=${(await page.$eval('body', (body) => body.innerText)).slice(0, 800)}; console=${consoleErrors.join(' | ')}`) })
    await page.click('button.routine-primary')
    await page.waitForSelector('.smart-builder')
    const metrics = await page.evaluate(() => ({
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      steps: document.querySelectorAll('.smart-rail-step').length,
      minButton: Math.min(...[...document.querySelectorAll('.smart-builder button')].map((item) => item.getBoundingClientRect().height)),
      builderColumns: getComputedStyle(document.querySelector('.smart-builder-grid')).gridTemplateColumns.split(' ').length,
      rawEntityVisible: document.body.textContent.includes('sensor.') || document.body.textContent.includes('light.living'),
    }))
    await page.keyboard.press('Tab'); const focusVisible = await page.evaluate(() => document.activeElement !== document.body)
    await page.screenshot({ path: path.join(out, `${viewport.name}.png`), fullPage: true })
    results.push({ viewport, ...metrics, focusVisible, consoleErrors })
    await page.close()
  }
} finally { await browser.close() }
const failures = results.flatMap((result) => [
  result.overflow > 0 && `${result.viewport.name}: horizontal overflow ${result.overflow}px`,
  result.steps !== 3 && `${result.viewport.name}: expected 3 decision steps`,
  result.minButton < 44 && `${result.viewport.name}: control target ${result.minButton}px`,
  result.rawEntityVisible && `${result.viewport.name}: raw entity identifier visible`,
  !result.focusVisible && `${result.viewport.name}: keyboard focus unavailable`,
  result.consoleErrors.length && `${result.viewport.name}: console errors ${result.consoleErrors.join(' | ')}`,
].filter(Boolean))
const report = { generated_at: new Date().toISOString(), base, results, failures }
await fs.writeFile(path.join(out, 'smart-automation-browser.json'), JSON.stringify(report, null, 2))
console.log(JSON.stringify(report, null, 2))
if (failures.length) process.exitCode = 1

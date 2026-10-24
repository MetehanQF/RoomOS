// One-command Panel Mode acceptance: start the isolated stack, run Chromium checks, always tear down.
//
//   cd tools/panel-accept && npm ci && npm run accept:panel
//   npm run accept:panel -- --tag sha-1234567 --label my-run
//   npm run probe:system | npm run probe:heap
//
// Requires Docker access, python3, openssl and a Chromium binary (ROOMOS_ACCEPT_CHROME).
// The images roomos-core:<tag> and roomos-web:<tag> must already exist (scripts/build-release-images.sh).
import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync } from 'node:fs'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const repo = path.resolve(here, '..', '..')
const args = process.argv.slice(2)
const option = (name, fallback) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : fallback }
const probe = option('probe', null)
const tag = option('tag', process.env.ROOMOS_ACCEPT_TAG ?? `sha-${execFileSync('git', ['-C', repo, 'rev-parse', '--short=7', 'HEAD'], { encoding: 'utf8' }).trim()}`)
const label = option('label', probe ? `probe-${probe}-${tag}` : `accept-${tag}`)
const stateHome = process.env.XDG_STATE_HOME ?? path.join(os.homedir(), '.local', 'state')
const outDir = path.resolve(option('out', process.env.ROOMOS_ACCEPT_OUT ?? path.join(stateHome, 'roomos-panel-accept', label)))
const port = process.env.ROOMOS_ACCEPT_PORT ?? '5551'

function findChrome() {
  if (process.env.ROOMOS_ACCEPT_CHROME) return process.env.ROOMOS_ACCEPT_CHROME
  const cache = path.join(os.homedir(), '.cache', 'cc-v2-browser')
  const candidates = existsSync(cache)
    ? readdirSync(cache).filter((d) => /^chromium-\d+$/.test(d)).sort().reverse().map((d) => path.join(cache, d, 'chrome-linux', 'chrome'))
    : []
  candidates.push('/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome')
  const found = candidates.find((candidate) => existsSync(candidate))
  if (!found) throw new Error('No Chromium found; set ROOMOS_ACCEPT_CHROME to a Chromium/Chrome executable')
  return found
}

const stack = (...rest) => execFileSync('python3', [path.join(here, 'stack.py'), ...rest], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] })

// The stack serves a disposable self-signed loopback certificate; only this process trusts it.
process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0'
await fs.mkdir(outDir, { recursive: true, mode: 0o700 })
const chrome = findChrome()
console.log(`panel-accept: tag=${tag} out=${outDir} chrome=${chrome}`)
let result = null
let cleanup = null
try {
  const info = JSON.parse(stack('up', tag, outDir, '--harness').trim().split('\n').pop())
  if (!info.egress_blocked) throw new Error('acceptance Core unexpectedly has network egress')
  const sessions = JSON.parse(await fs.readFile(path.join(outDir, 'sessions.json'), 'utf8'))
  const context = { label, base: `https://127.0.0.1:${port}`, haIp: info.fake_ha, sessions, outDir, chrome, coreContainer: info.core_container, tag }
  if (probe) {
    const probes = await import('./probes.mjs')
    if (!probes[probe]) throw new Error(`unknown probe ${probe}`)
    result = await probes[probe](context, args)
  } else {
    const { accept } = await import('./panel-accept.mjs')
    result = await accept(context)
  }
} finally {
  cleanup = JSON.parse(stack('down', outDir).trim())
  const clean = !cleanup.containers.length && !cleanup.networks.length && !cleanup.volumes.length && !cleanup.forwarder_listening
  console.log(`panel-accept: cleanup ${clean ? 'complete' : 'INCOMPLETE'} ${JSON.stringify(cleanup)}`)
  if (result) {
    result.cleanup = { ...cleanup, clean }
    await fs.writeFile(path.join(outDir, `${label}.json`), JSON.stringify(result, null, 2))
  }
  if (!clean) process.exitCode = 2
}
const failed = result?.summary?.failed ?? []
if (!probe && (!result || failed.length)) process.exitCode = process.exitCode || 1
console.log(`panel-accept: ${probe ? 'probe finished' : failed.length ? `FAILED ${failed.length}` : `PASSED ${result.summary.passed}`} → ${outDir}`)

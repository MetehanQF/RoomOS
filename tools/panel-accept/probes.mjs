// Diagnostic probes for the isolated stack (not pass/fail gates). Started by run.mjs --probe <name>.
import puppeteer from 'puppeteer-core'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function open({ chrome, sessions }, who = 'phone2view') {
  const browser = await puppeteer.launch({ executablePath: chrome, headless: true, acceptInsecureCerts: true, args: ['--no-sandbox', '--ignore-certificate-errors'] })
  const ctx = await browser.createBrowserContext(); const page = await ctx.newPage()
  await page.setViewport({ width: 360, height: 800 })
  await ctx.setCookie({ name: 'roomos_session', value: sessions[who].token, domain: '127.0.0.1', path: '/api', secure: true, httpOnly: true, sameSite: 'Strict' })
  return { browser, page }
}

/** Logs every API GET and WebSocket frame while /system is open for 90 s (feedback-loop diagnosis). */
export async function system(context, args) {
  const mode = args.includes('--direct') ? 'direct' : 'panel-first'
  const { browser, page } = await open(context)
  const t0 = Date.now(); const at = () => ((Date.now() - t0) / 1000).toFixed(1)
  const log = []
  const cdp = await page.createCDPSession(); await cdp.send('Network.enable')
  cdp.on('Network.webSocketFrameReceived', (e) => { try { const x = JSON.parse(e.response.payloadData); log.push([at(), 'ws', x.type]) } catch { /* not JSON */ } })
  page.on('request', (r) => { const p = new URL(r.url()).pathname; if (p.startsWith('/api/')) log.push([at(), 'GET', p]) })
  if (mode === 'panel-first') { await page.goto(`${context.base}/panel`, { waitUntil: 'networkidle2' }); await sleep(3000); log.push([at(), 'goto', '/system']) }
  await page.goto(`${context.base}/system`, { waitUntil: 'domcontentloaded' })
  await sleep(90_000)
  await browser.close()
  for (const line of log) console.log(line.join(' '))
  return { probe: 'system', mode, log }
}

/** Takes a heap snapshot after repeated interactions and prints the retainer path of one detached DOM node. */
export async function heap(context, args) {
  const page_ = args.includes('--lights') ? 'lights' : 'panel'
  const { browser, page } = await open(context)
  await page.goto(`${context.base}/${page_}`, { waitUntil: 'networkidle2' }); await sleep(1500)
  for (let i = 0; i < 5; i++) {
    if (page_ === 'panel') { await page.click('button[aria-label="Panel settings"]'); await page.waitForSelector('.panel-settings'); await page.click('.panel-settings .sheet-close'); await page.waitForSelector('dialog[open]', { hidden: true }) }
    else { await page.click('a[href="/home"]'); await page.waitForSelector('.home-panel-link'); await page.goBack(); await page.waitForSelector('.lights-v2'); await sleep(300) }
  }
  const cdp = await page.createCDPSession()
  const chunks = []
  cdp.on('HeapProfiler.addHeapSnapshotChunk', (e) => chunks.push(e.chunk))
  await cdp.send('HeapProfiler.collectGarbage'); await sleep(1000)
  await cdp.send('HeapProfiler.takeHeapSnapshot', { reportProgress: false })
  const snap = JSON.parse(chunks.join(''))
  await browser.close()
const m = snap.snapshot.meta, NF = m.node_fields.length, EF = m.edge_fields.length
const nodes = snap.nodes, edges = snap.edges, strings = snap.strings
const nType = m.node_fields.indexOf('type'), nName = m.node_fields.indexOf('name'), nEdges = m.node_fields.indexOf('edge_count')
const eType = m.edge_fields.indexOf('type'), eName = m.edge_fields.indexOf('name_or_index'), eTo = m.edge_fields.indexOf('to_node')
const nodeTypes = m.node_types[0], edgeTypes = m.edge_types[0]
const count = nodes.length / NF
const firstEdge = new Uint32Array(count + 1)
for (let i = 0, e = 0; i < count; i++) { firstEdge[i] = e; e += nodes[i * NF + nEdges] * EF }
firstEdge[count] = edges.length
const retainers = Array.from({ length: count }, () => [])
for (let i = 0; i < count; i++) for (let e = firstEdge[i]; e < firstEdge[i + 1]; e += EF) {
  const to = edges[e + eTo] / NF, t = edgeTypes[edges[e + eType]]
  if (t === 'weak') continue
  retainers[to].push([i, t, t === 'element' || t === 'hidden' ? edges[e + eName] : strings[edges[e + eName]]])
}
const name = (i) => strings[nodes[i * NF + nName]]
const nDet = m.node_fields.indexOf('detachedness')
const detached = []
for (let i = 0; i < count; i++) if ((nDet >= 0 && nodes[i * NF + nDet] === 2) || name(i).startsWith('Detached ')) detached.push(i)
const byName = {}; for (const i of detached) byName[name(i)] = (byName[name(i)] || 0) + 1
const top = Object.entries(byName).sort((a, b) => b[1] - a[1]).slice(0, 8)
  console.log('detached', detached.length, top)
// BFS from one detached element up to a GC root, preferring non-DOM retainers.
const target = detached.find((i) => /^(HTMLButtonElement|HTMLDivElement|HTMLElement|Detached HTML)/.test(name(i))) ?? detached[0]
if (target !== undefined) {
  const prev = new Map([[target, null]]); const queue = [target]; let root = null
  while (queue.length && root === null) {
    const n = queue.shift()
    for (const [from, t, label] of retainers[n]) {
      if (prev.has(from)) continue
      prev.set(from, [n, t, label]); queue.push(from)
      const nm = name(from)
      if (nodeTypes[nodes[from * NF + nType]] === 'synthetic' || nm.startsWith('Window') || nm === '(GC roots)') { root = from; break }
    }
  }
  let cur = root, path = []
  while (cur !== null && cur !== undefined) { const p = prev.get(cur); path.push(`${name(cur).slice(0, 60)} [${nodeTypes[nodes[cur * NF + nType]]}]` + (p ? ` --${p[1]}:${String(p[2]).slice(0, 40)}-->` : '')); cur = p ? p[0] : null }
  console.log(path.slice(0, 30).join('\n'))
}
return { probe: 'heap', page: page_, detached: detached.length, top }
}

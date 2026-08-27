/**
 * Screenshot harness for the owner-not-configured mutation guidance.
 *
 * Runs the REAL built SPA (website/dist) against the shared static server,
 * with every /api/** call intercepted by Playwright and answered from
 * fixtures. No gateway, no dashboard token, no provider calls.
 *
 * Scene: a standalone local install (no configured owner) loads a pull
 * request in the Changes panel — reads pass — and the user clicks
 * Enable auto-merge, then Confirm. The mutation is refused with the coded
 * 403 this change introduces, and the panel must render the localized
 * guidance (what to configure, where) plus the Slack-settings link instead
 * of a bare "forbidden".
 *
 * Usage: node scripts/capture-owner-not-configured.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/owner-not-configured-hint'
const PREFIX = process.argv[3] || 'after'
if (!/^[A-Za-z0-9._-]+$/.test(PREFIX)) {
  console.error(`prefix must match [A-Za-z0-9._-]+, got: ${PREFIX}`)
  process.exit(2)
}
const SLOT = 'chat-owner-hint'
const PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/6420'

mkdirSync(OUT, { recursive: true })

const slots = () => [{
  key: SLOT,
  title: 'Owner-not-configured guidance',
  running: false,
  last_message: 'Opened the PR in the Changes panel…',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  modified: Math.floor(Date.now() / 1000),
  source_links: [
    { provider: 'github', number: 6420, url: PR_URL, state: 'open', ci: 'passed' },
  ],
  source_links_total: 1,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  messages: [
    { role: 'user', content: 'Open the PR', ts: Date.now() / 1000 - 600 },
    { role: 'assistant', ts: Date.now() / 1000 - 60, content: `Working on ${PR_URL}.` },
  ],
}

const source = {
  provider: 'github',
  url: PR_URL,
  number: 6420,
  title: 'fix(dashboard): name the remedy when a provider mutation needs a configured owner',
  description: 'Owner-gated mutations on a local install now explain what to configure.',
  state: 'OPEN',
  draft: false,
  mergedAt: '',
  updatedAt: new Date().toISOString(),
  headBranch: 'fix/owner-not-configured-hint',
  baseBranch: 'main',
  headSha: 'abc1234',
  author: 'kirocrew',
  additions: 40,
  deletions: 4,
  changedFiles: 4,
  mergeable: 'mergeable',
  mergeStateStatus: 'clean',
  autoMerge: false,
  commits: [{
    sha: 'abc1234', message: 'fix(dashboard): owner-not-configured guidance',
    author: 'kirocrew', committedAt: new Date().toISOString(), url: PR_URL,
  }],
  checks: [{ name: 'ci', bucket: 'success', status: 'completed', conclusion: 'success', url: '' }],
  comments: [],
  files: [
    { path: 'src/kiro_crew/dashboard/handlers/source_providers.py', status: 'modified', additions: 20, deletions: 2, patch: '' },
  ],
  partialSections: [],
}

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

const FIXED_ROUTES = {
  '/api/kiro-prerequisite': { ready: true },
  '/api/status': { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' },
  '/api/notifications': { notifications: [], unread: 0 },
  '/api/config': {},
  '/api/kirocrew-config': {},
  '/api/dashboard/branding': { bot_name: 'Kiro', avatar: '' },
  '/api/auth/me': { user: 'local-app', app: '' },
  '/api/models': { models: [], default: 'auto' },
  '/api/themes': { themes: [], installed: [] },
  '/api/theme/boot': { mode: 'dark', theme: '' },
  '/api/chat/nav/resolve-links': { summaries: [] },
}

async function main() {
  const { srv: server, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/source/pull-request/auto-merge') {
      // The refusal under test: a signed local session with no configured
      // owner gets the coded, actionable 403 instead of a bare forbidden.
      return json(route, {
        error: 'this action needs a configured owner, which Kiro Crew identifies by Slack member ID',
        code: 'owner_not_configured',
      }, 403)
    }
    if (path in FIXED_ROUTES) return json(route, FIXED_ROUTES[path])
    if (path === '/api/chat/slots') return json(route, slots())
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    if (path === '/api/source/pull-request') return json(route, source)
    if (path === '/api/source/pull-request/status') {
      return json(route, { statuses: { [PR_URL]: { state: 'open', ci: 'passed' } }, refreshing: [], ttlSecs: 60 })
    }
    if (path === '/api/source/pull-request/checks') return json(route, { checks: source.checks })
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    return json(route, objectish ? {} : [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 200)))

  await page.addInitScript(() => {
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', 'chat-owner-hint')
  })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  const opener = page.getByRole('button', { name: 'Open activity panel' })
  if (await opener.count()) {
    await opener.first().click().catch(() => {})
    await page.waitForTimeout(1200)
  }
  const changes = page.getByRole('button', { name: /^Changes/ })
  if (await changes.count()) {
    await changes.first().click().catch(() => {})
    await page.waitForTimeout(2000)
  }

  // Drive the two-click auto-merge flow into the refusal.
  await page.getByRole('button', { name: /enable auto-merge/i }).first().click()
  await page.waitForTimeout(400)
  await page.getByRole('button', { name: /confirm auto-merge/i }).first().click()
  await page.waitForTimeout(1200)

  await page.screenshot({ path: `${OUT}/${PREFIX}-guidance-full.png` })
  console.log('wrote', `${OUT}/${PREFIX}-guidance-full.png`)

  // Crop around the action row so the guidance and the settings link read
  // at review size.
  const link = page.getByRole('link', { name: /slack settings/i }).first()
  const box = await link.boundingBox().catch(() => null)
  const clip = box
    ? (() => { const x = Math.max(0, box.x - 620); return { x, y: Math.max(0, box.y - 170), width: 1600 - x, height: box.height + 240 } })()
    : { x: 240, y: 100, width: 1360, height: 320 }
  await page.screenshot({ path: `${OUT}/${PREFIX}-guidance-crop.png`, clip })
  console.log('wrote', `${OUT}/${PREFIX}-guidance-crop.png`)

  await browser.close()
  server.close()
}

main().catch(err => { console.error(err); process.exit(1) })

// Real-browser evidence for KiroCrew PR #13541.
// Drives a Chromium page against a harness gateway. The file drag is produced
// with CDP Input.dispatchDragEvent, which goes through the renderer's real
// hit-testing (unlike a synthetic DragEvent), so body `pointer-events: none`
// under a modal Radix menu is exercised exactly as an OS drag would.
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'

const url = process.argv[2]
const outDir = process.argv[3]
if (!url || !outDir) { console.error('usage: evidence.mjs <url> <outDir>'); process.exit(2) }
fs.mkdirSync(outDir, { recursive: true })

const filePath = path.join(outDir, 'hello.txt')
fs.writeFileSync(filePath, 'hello from the OS\n')

const browser = await chromium.launch({ headless: true })
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } })
const log = (...a) => console.log('[evidence]', ...a)

await page.goto(url, { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)

// 1. Open the sidebar "More options" dropdown (a modal Radix DropdownMenu).
const more = page.getByRole('button', { name: 'More options' }).first()
await more.waitFor({ state: 'visible', timeout: 20000 })
await more.click()
const menu = page.getByRole('menu').first()
await menu.waitFor({ state: 'visible', timeout: 5000 })
const bodyPE1 = await page.evaluate(() => getComputedStyle(document.body).pointerEvents)
log('menu open; body pointer-events =', bodyPE1)
await page.screenshot({ path: path.join(outDir, '1-menu-open.png') })

// 2. Locate the composer textarea (the drop zone covers the chat pane).
const composer = page.locator('textarea').first()
await composer.waitFor({ state: 'visible', timeout: 10000 })
const box = await composer.boundingBox()
const x = box.x + box.width / 2, y = box.y + box.height / 2

const cdp = await page.context().newCDPSession(page)
const data = { items: [{ mimeType: 'Files', data: '' }], files: [filePath], dragOperationsMask: 1 }
// Enter from outside the window at the top-left edge, then move over the composer.
await cdp.send('Input.dispatchDragEvent', { type: 'dragEnter', x: 5, y: 5, data })
await page.waitForTimeout(150)
const menuGoneAfterEnter = await page.getByRole('menu').count()
const bodyPE2 = await page.evaluate(() => getComputedStyle(document.body).pointerEvents)
log('after dragEnter: menus mounted =', menuGoneAfterEnter, '; body pointer-events =', bodyPE2)
await page.screenshot({ path: path.join(outDir, '2-after-dragenter.png') })

await cdp.send('Input.dispatchDragEvent', { type: 'dragOver', x, y, data })
await page.waitForTimeout(150)
await cdp.send('Input.dispatchDragEvent', { type: 'dragOver', x, y, data })
await page.waitForTimeout(300)
const overlay = await page.getByTestId('chat-drop-overlay').count()
log('over composer: drop overlay mounted =', overlay)
await page.screenshot({ path: path.join(outDir, '3-over-composer.png') })

await cdp.send('Input.dispatchDragEvent', { type: 'drop', x, y, data })
await page.waitForTimeout(1500)
await page.screenshot({ path: path.join(outDir, '4-after-drop.png') })
const attached = await page.getByText('hello.txt').count()
log('after drop: "hello.txt" visible in composer =', attached)

const result = { bodyPointerEventsWithMenuOpen: bodyPE1, menusMountedAfterDragEnter: menuGoneAfterEnter, bodyPointerEventsAfterDragEnter: bodyPE2, dropOverlayMounted: overlay, attachmentVisible: attached }
fs.writeFileSync(path.join(outDir, 'result.json'), JSON.stringify(result, null, 2))
log(JSON.stringify(result))
await browser.close()
const pass = bodyPE1 === 'none' && menuGoneAfterEnter === 0 && overlay >= 1 && attached >= 1
process.exit(pass ? 0 : 1)

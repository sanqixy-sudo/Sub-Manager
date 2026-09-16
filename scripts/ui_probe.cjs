/* Standalone isolated-browser regression/probe. No production credentials. */
const fs = require('node:fs/promises')
const path = require('node:path')
const assert = require('node:assert/strict')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')

async function main() {
  const base = 'http://127.0.0.1:17778'
  const output = path.resolve('.ui-verification', process.env.PROBE_RUN || 'stability')
  await fs.mkdir(output, { recursive: true })
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const context = await browser.newContext({ reducedMotion: 'reduce', serviceWorkers: 'block' })
  const login = await context.request.post(base + '/api/auth/login', { data: { username: 'admin', password: 'test-only-password' } })
  assert.equal(login.status(), 200)
  const page = await context.newPage()
  const log = { errors: [], warnings: [], failed: [] }
  page.on('pageerror', e => log.errors.push(e.message))
  page.on('console', m => { if (m.type() === 'error' || m.type() === 'warning') log.warnings.push(m.text()) })
  page.on('requestfailed', r => log.failed.push({ url: new URL(r.url()).pathname, reason: r.failure()?.errorText }))
  const measurements = []
  for (const width of [1920, 1366, 390]) {
    await page.setViewportSize({ width, height: 900 })
    for (const route of ['/', '/health', '/groups/1/detail?tab=nodes', '/settings']) {
      await page.goto(base + '/#' + route)
      const heading = { '/': '运行概览', '/health': '节点测活', '/settings': '系统设置', '/groups/1/detail?tab=nodes': '测试订阅组' }[route]
      await page.getByRole('heading', { name: heading, exact: true }).waitFor()
      await page.waitForLoadState('networkidle')
      assert.equal(new URL(page.url()).hash, '#' + route)
      if (route === '/health') {
        await page.locator('.health-card').first().waitFor()
        await page.locator('.health-card .el-checkbox').first().click()
      }
      if (route.includes('tab=nodes')) {
        await page.locator('.node-panel .el-table__body tr').first().waitFor()
        await page.locator('.node-panel .el-table__body .el-checkbox').first().click()
      }
      const dimensions = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, viewport: innerWidth,
        culprits: [...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > innerWidth + 1 && getComputedStyle(e).position !== 'fixed').slice(-10).map(e => ({ tag: e.tagName, cls: String(e.className), right: e.getBoundingClientRect().right })) }))
      const slug = route.replace(/[^a-z0-9]/gi, '_') || 'overview'
      const screenshot = path.join(output, `${slug}-${width}.png`)
      await page.screenshot({ path: screenshot, fullPage: true })
      measurements.push({ route, width, ...dimensions, screenshot, rule: 'layout-long-content-safety', result: dimensions.scroll <= width + 1 ? 'not-reproduced' : 'reproduced' })
    }
  }
  let injected = 0
  await page.route('**/api/node-health/nodes?*', route => { injected++; return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'secret.internal.invalid' }) }) })
  await page.goto(base + '/#/health')
  await page.getByText('服务器开小差了（500），请稍后重试').waitFor()
  assert(injected > 0)
  assert(!(await page.locator('body').innerText()).includes('secret.internal.invalid'))
  await page.screenshot({ path: path.join(output, 'health-injection-500.png'), fullPage: true })
  await page.unroute('**/api/node-health/nodes?*')
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await page.locator('.health-card').first().waitFor()
  const report = { session: { driver: 'playwright', browser: await browser.version(), buildMode: 'production', baseUrl: base,
    auth: 'seeded-test-session', dataFixture: 'scripts/ui_fixture.py', themes: ['light'], viewports: [1920,1366,390],
    probesRun: ['viewport-stress','console-network','failure-injection'], probesSkipped: ['axe-scan','focus-walk','target-size','layout-shift','web-vitals','theme-locale-matrix'].map(probe => ({ probe, reason: 'Outside this targeted regression probe; no pass claimed' })) },
    measurements, log, failureInjection: { injected, retry: 'passed' } }
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  await browser.close()
  console.log(JSON.stringify({ measurements: measurements.map(({route,width,scroll,result}) => ({route,width,scroll,result})), errors: log.errors, output }))
  assert(!measurements.some(x => x.scroll > x.width + 1), 'Document overflow')
  assert.equal(log.errors.length, 0, 'Unexpected page errors')
}
main().catch(e => { console.error(e.message); process.exit(1) })

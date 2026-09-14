import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'

const require = createRequire(resolve('../AiCollabHub_User/package.json'))
const { chromium, expect } = require('@playwright/test')
const baseURL = process.env.FRAMEWORK_URL || 'http://127.0.0.1:18880'
const directory = resolve('.runtime/framework-mock-20260913')
await mkdir(directory, { recursive: true })
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const results = []
try {
  for (const [name, viewport] of [['desktop', { width: 1440, height: 960 }], ['mobile', { width: 390, height: 844 }]]) {
    const context = await browser.newContext({ viewport })
    const page = await context.newPage()
    const errors = [], failedResponses = [], authRequests = [], instances = new Set()
    page.on('pageerror', error => errors.push(error.message))
    page.on('request', request => {
      const path = new URL(request.url()).pathname
      if (path.startsWith('/api/v1/auth/') && path !== '/api/v1/auth/config') authRequests.push(path)
    })
    page.on('response', response => {
      if (response.status() >= 400) failedResponses.push({ path: new URL(response.url()).pathname, status: response.status() })
      const instance = response.headers()['x-app-instance']
      if (instance) instances.add(instance)
    })
    for (const path of ['/', '/datasets', '/models', '/applications', '/knowledge', '/tools', '/feedback', '/publish', '/admin/', '/admin/services']) {
      await page.goto(baseURL + path)
      await page.waitForLoadState('networkidle')
      expect(page.url()).not.toContain('/login')
      await expect(page.getByText('演示环境', { exact: true }).first()).toBeVisible()
      if (['/datasets', '/models', '/applications', '/tools'].includes(path)) {
        expect(await page.locator('.catalog-card').count()).toBeGreaterThan(0)
      }
      if (path === '/knowledge') expect(await page.locator('.knowledge-card').count()).toBeGreaterThan(0)
      if (path === '/') await expect(page.locator('.home-content-grid')).toBeVisible()
      if (path === '/admin/') expect(await page.locator('tbody tr').count()).toBeGreaterThan(0)
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)
      expect(overflow, path).toBe(false)
      if (['/', '/datasets', '/knowledge', '/admin/'].includes(path)) {
        await page.screenshot({ path: resolve(directory, `${name}-${path.replaceAll('/', '') || 'home'}.png`), fullPage: true })
      }
    }
    await page.goto(baseURL + '/feedback')
    await page.locator('input[maxlength="60"]').fill('Mock 框架连通测试')
    await page.locator('textarea').fill('仅验证前后端请求响应，不创建真实反馈。')
    await page.locator('input[type="file"]').setInputFiles({ name: 'demo.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-demo') })
    const submitted = page.waitForResponse(response => response.url().endsWith('/api/v1/feedback') && response.request().method() === 'POST')
    await page.getByRole('button', { name: '提交反馈', exact: true }).click()
    expect((await submitted).status()).toBe(201)
    await expect(page.getByText('模拟提交完成', { exact: true })).toBeVisible()
    await page.goto(baseURL + '/admin/')
    await page.locator('.row-title').first().click()
    await page.getByLabel('处理说明', { exact: true }).fill('Mock 请求已收到')
    await page.getByRole('button', { name: '保存处理结果', exact: true }).click()
    await expect(page.getByText('模拟处理完成', { exact: true })).toBeVisible()
    expect(failedResponses).toEqual([])
    expect(errors).toEqual([])
    expect(authRequests).toEqual([])
    expect([...instances].sort()).toEqual(['A', 'B'])
    results.push({ viewport: name, routes: 10, dataMode: 'backend mock over HTTP', authenticated: false, errors: 0, instances: [...instances].sort() })
    await context.close()
  }
  await writeFile(resolve(directory, 'browser-results.json'), JSON.stringify(results, null, 2))
  console.log(JSON.stringify(results))
} finally { await browser.close() }

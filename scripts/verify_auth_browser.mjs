import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'

const require = createRequire(resolve('../AiCollabHub_User/package.json'))
const { chromium, expect } = require('@playwright/test')
const baseURL = process.env.FRAMEWORK_URL || 'http://127.0.0.1:18880'
const output = resolve('.runtime/auth-verification')
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const results = []
try {
  for (const [name, viewport] of [['desktop', { width: 1440, height: 960 }], ['mobile', { width: 390, height: 844 }]]) {
    const context = await browser.newContext({ viewport })
    const page = await context.newPage()
    const errors = [], failures = [], instances = new Set()
    page.on('pageerror', error => errors.push(error.message))
    page.on('response', response => {
      const path = new URL(response.url()).pathname
      if (response.status() >= 400 && response.status() !== 401) failures.push({ path, status: response.status() })
      const instance = response.headers()['x-app-instance']
      if (instance) instances.add(instance)
    })
    const checkLayout = async (label) => {
      await page.waitForLoadState('networkidle')
      expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), label).toBe(false)
      await page.screenshot({ path: resolve(output, `${name}-${label}.png`), fullPage: true })
    }
    await page.goto(baseURL + '/applications?q=AI')
    await expect(page.getByRole('heading', { name: '登录平台' })).toBeVisible()
    expect(new URL(page.url()).searchParams.get('redirect')).toBe('/applications?q=AI')
    await checkLayout('user-login')
    await page.getByRole('button', { name: '统一身份登录', exact: true }).click()
    await expect(page.getByRole('heading', { name: '模拟统一认证' })).toBeVisible()
    await page.getByLabel('模拟管理员', { exact: true }).check()
    await checkLayout('mock-iam')
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.waitForURL(baseURL + '/applications?q=AI')
    await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
    await expect(page.getByRole('link', { name: '管理工作台', exact: true })).toBeVisible()
    await checkLayout('user-session')
    for (const port of [18000, 18001]) {
      const response = await context.request.get(`http://127.0.0.1:${port}/api/v1/auth/session`)
      expect(response.status()).toBe(200)
      expect((await response.json()).user.id).toBe('mock-admin')
    }
    const cookie = (await context.cookies()).find(cookie => cookie.name === 'ach_session_user')
    expect(cookie.httpOnly).toBe(true)
    expect(cookie.sameSite).toBe('Lax')
    await page.goto(baseURL + '/feedback')
    await page.locator('input[maxlength="60"]').fill('单点登录联通测试')
    await page.locator('textarea').fill('验证登录后的 Mock 请求与 CSRF，不创建真实业务记录。')
    const submitted = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/feedback' && response.request().method() === 'POST')
    await page.getByRole('button', { name: '提交反馈', exact: true }).click()
    expect((await submitted).status()).toBe(201)
    await page.goto(baseURL + '/admin/services')
    await expect(page.getByRole('heading', { name: '管理工作台' })).toBeVisible()
    await checkLayout('admin-login')
    await page.getByRole('button', { name: '统一身份登录', exact: true }).click()
    // IAM 已有登录会话，管理端直接完成回调，无需再次选择账号。
    await page.waitForURL(baseURL + '/admin/services')
    await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
    await checkLayout('admin-session')
    const logoutResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/auth/logout')
    await page.getByRole('button', { name: '退出登录', exact: true }).click()
    expect((await logoutResponse).status()).toBe(200)
    await expect(page.getByText('已退出登录', { exact: true })).toBeVisible()
    for (const scope of ['user', 'admin']) {
      for (const port of [18000, 18001]) {
        expect((await context.request.get(`http://127.0.0.1:${port}/api/v1/auth/session?scope=${scope}`)).status()).toBe(401)
      }
    }
    await page.goto(baseURL + '/applications')
    await expect(page.getByRole('heading', { name: '登录平台' })).toBeVisible()
    // 普通用户无法通过直接访问管理路由绕过测试准入规则。
    await page.getByRole('button', { name: '统一身份登录', exact: true }).click()
    await page.getByLabel('模拟普通用户', { exact: true }).check()
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.waitForURL(baseURL + '/applications')
    await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
    await expect(page.getByRole('link', { name: '管理工作台', exact: true })).toHaveCount(0)
    await checkLayout('user-no-admin-entry')
    await page.goto(baseURL + '/admin/feedback')
    await page.getByRole('button', { name: '统一身份登录', exact: true }).click()
    await expect(page.getByRole('alert')).toContainText('当前账号无管理端访问权限')
    await checkLayout('admin-denied')
    expect(errors).toEqual([])
    expect(failures).toEqual([])
    expect([...instances].sort()).toEqual(['A', 'B'])
    results.push({ viewport: name, crossInstance: true, globalLogout: true, csrfSubmission: true, adminDenied: true, adminEntryByIdentity: true, errors: 0 })
    await context.close()
  }
  await writeFile(resolve(output, 'browser-results.json'), JSON.stringify(results, null, 2))
  console.log(JSON.stringify(results))
} finally { await browser.close() }

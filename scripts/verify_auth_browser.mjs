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
    const errors = [], failures = [], instances = new Set(), loginRequests = []
    context.on('page', page => page.on('pageerror', error => errors.push(error.message)))
    context.on('request', request => {
      const path = new URL(request.url()).pathname
      if (['/api/v1/auth/login', '/api/v1/auth/callback', '/authn-api/v5/cas/platform/login'].includes(path)) loginRequests.push(path)
    })
    context.on('response', response => {
      const url = new URL(response.url()), path = url.pathname
      const expectedDenial = response.status() === 403 && path === '/api/v1/auth/session' && url.searchParams.get('scope') === 'admin'
      if (response.status() >= 400 && response.status() !== 401 && !expectedDenial) failures.push({ path, status: response.status() })
      const instance = response.headers()['x-app-instance']
      if (instance) instances.add(instance)
    })
    let page = await context.newPage()
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
      for (const scope of ['user', 'admin']) {
        const response = await context.request.get(`http://127.0.0.1:${port}/api/v1/auth/session?scope=${scope}`)
        expect(response.status()).toBe(200)
        expect((await response.json()).user.id).toBe('mock-admin')
      }
    }
    const cookie = (await context.cookies()).find(cookie => cookie.name === 'ach_session')
    expect(cookie).toBeTruthy()
    expect(cookie.httpOnly).toBe(true)
    expect(cookie.sameSite).toBe('Lax')
    await page.goto(baseURL + '/feedback')
    await page.locator('input[maxlength="60"]').fill('单点登录联通测试')
    await page.locator('textarea').fill('验证登录后的 Mock 请求与 CSRF，不创建真实业务记录。')
    const submitted = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/feedback' && response.request().method() === 'POST')
    await page.getByRole('button', { name: '提交反馈', exact: true }).click()
    expect((await submitted).status()).toBe(201)
    const userPage = page, initialLoginRequests = loginRequests.length
    const adminPopup = page.waitForEvent('popup')
    await page.getByRole('link', { name: '管理工作台', exact: true }).click()
    page = await adminPopup
    await page.waitForURL(baseURL + '/admin/applications')
    await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '统一身份登录', exact: true })).toHaveCount(0)
    // 已登录时即便打开收藏的登录地址，也应直接返回原管理页面。
    await page.goto(baseURL + '/admin/login?redirect=' + encodeURIComponent('/admin/services'))
    await page.waitForURL(baseURL + '/admin/services')
    await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
    await checkLayout('admin-session')
    expect(loginRequests.length).toBe(initialLoginRequests)
    expect((await context.cookies()).filter(cookie => cookie.name.startsWith('ach_session')).map(cookie => cookie.name)).toEqual(['ach_session'])
    const logoutResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/auth/logout')
    await page.getByRole('button', { name: '退出登录', exact: true }).click()
    expect((await logoutResponse).status()).toBe(200)
    await expect(page.getByText('已退出登录', { exact: true })).toBeVisible()
    for (const scope of ['user', 'admin']) {
      for (const port of [18000, 18001]) {
        expect((await context.request.get(`http://127.0.0.1:${port}/api/v1/auth/session?scope=${scope}`)).status()).toBe(401)
      }
    }
    await userPage.bringToFront()
    await userPage.evaluate(() => window.dispatchEvent(new Event('focus')))
    await expect(userPage.getByRole('heading', { name: '登录平台' })).toBeVisible()
    await userPage.close()

    // 反向验证：直接在管理端登录，再通过实际入口打开用户端。
    await page.goto(baseURL + '/admin/services')
    await page.getByRole('button', { name: '统一身份登录', exact: true }).click()
    await page.getByLabel('模拟管理员', { exact: true }).check()
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.waitForURL(baseURL + '/admin/services')
    await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
    const reverseLoginRequests = loginRequests.length, adminPage = page
    if (name === 'mobile') await page.getByRole('button', { name: '打开管理导航', exact: true }).click()
    const userPopup = page.waitForEvent('popup')
    await page.getByRole('link', { name: '打开用户端', exact: true }).filter({ visible: true }).click()
    page = await userPopup
    await page.waitForURL(baseURL + '/')
    await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
    await page.goto(baseURL + '/login?redirect=' + encodeURIComponent('/models?q=AI'))
    await page.waitForURL(baseURL + '/models?q=AI')
    await checkLayout('admin-to-user')
    expect(loginRequests.length).toBe(reverseLoginRequests)
    await page.getByRole('button', { name: '退出登录', exact: true }).click()
    await expect(page.getByText('已退出登录', { exact: true })).toBeVisible()
    await adminPage.bringToFront()
    await adminPage.evaluate(() => window.dispatchEvent(new Event('focus')))
    await expect(adminPage.getByRole('heading', { name: '管理工作台', exact: true })).toBeVisible()
    await adminPage.close()

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
    const ordinaryLoginRequests = loginRequests.length
    await page.goto(baseURL + '/admin/feedback')
    await expect(page.getByRole('alert')).toContainText('当前账号无管理端访问权限')
    await checkLayout('admin-denied')
    expect(loginRequests.length).toBe(ordinaryLoginRequests)
    // 在另一端切换到管理员后，原无权限页面重新获得焦点即可恢复原目标。
    const deniedPage = page
    page = await context.newPage()
    await page.goto(baseURL + '/api/v1/auth/login?scope=user&renew=true&return_to=/models')
    await page.getByLabel('模拟管理员', { exact: true }).check()
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.waitForURL(baseURL + '/models')
    const switchedLoginRequests = loginRequests.length
    await deniedPage.bringToFront()
    await deniedPage.evaluate(() => window.dispatchEvent(new Event('focus')))
    await deniedPage.waitForURL(baseURL + '/admin/feedback')
    await expect(deniedPage.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
    expect(loginRequests.length).toBe(switchedLoginRequests)
    expect(errors).toEqual([])
    expect(failures).toEqual([])
    expect([...instances].sort()).toEqual(['A', 'B'])
    results.push({ viewport: name, sharedLoginBothDirections: true, loginReturnPath: true, crossInstance: true, logoutBothDirections: true, csrfSubmission: true, adminDenied: true, accountSwitchShared: true, errors: 0 })
    await context.close()
  }
  await writeFile(resolve(output, 'browser-results.json'), JSON.stringify(results, null, 2))
  console.log(JSON.stringify(results))
} finally { await browser.close() }

import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'

const require = createRequire(resolve('../AiCollabHub_User/package.json'))
const { chromium, expect } = require('@playwright/test')
const baseURL = process.env.FRAMEWORK_URL || 'http://127.0.0.1:18880'
const apiURL = process.env.API_URL || `http://${new URL(baseURL).hostname}:18000`
const expectedWorkers = Number(process.env.EXPECTED_API_WORKERS || 2)
if (!Number.isInteger(expectedWorkers) || expectedWorkers < 1) throw new Error('EXPECTED_API_WORKERS must be positive')
const output = resolve('.runtime/auth-verification')
const verifyIdentitySync = process.env.VERIFY_IDENTITY_SYNC === '1'
// 模拟来源密钥只留在测试进程内，不注入页面、不写截图或验证结果。
const syncKey = verifyIdentitySync ? execFileSync(resolve('.venv/Scripts/python.exe'), [
  '-c', 'from app.core.config import Settings; print(Settings().auth_mock_secret.get_secret_value(), end="")',
], { encoding: 'utf8' }) : ''
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const results = []
try {
  for (const [name, viewport] of [['desktop', { width: 1440, height: 960 }], ['mobile', { width: 390, height: 844 }]]) {
    const context = await browser.newContext({ viewport })
    // 模拟内嵌浏览器接管未处理的普通链接，验证两端切换不依赖默认链接行为。
    await context.addInitScript(() => {
      document.addEventListener('click', event => {
        if (event.defaultPrevented || !(event.target instanceof Element)) return
        const link = event.target.closest('a[href]')
        if (!link) return
        event.preventDefault()
        window.open(link.href, '_blank', 'noopener')
      })
    })
    const errors = [], failures = [], workers = new Set(), loginRequests = []
    let adminAccessRevoked = false
    // 同一端口新建连接，验证会话在实际 worker 间共享，避免网关长连接固定落在一个进程。
    const verifyWorkerSessions = async status => {
      for (const scope of ['user', 'admin']) {
        const observed = new Set()
        for (let attempt = 0; attempt < 40 && observed.size < expectedWorkers; attempt++) {
          const response = await context.request.get(`${apiURL}/api/v1/auth/session?scope=${scope}`, {
            headers: { Connection: 'close' },
          })
          expect(response.status()).toBe(status)
          if (status === 200) expect((await response.json()).user.id).toBe('mock-admin')
          const worker = response.headers()['x-app-worker']
          expect(worker).toBeTruthy()
          observed.add(worker)
          workers.add(worker)
        }
        expect(observed.size, `${scope} session across workers`).toBe(expectedWorkers)
      }
    }
    context.on('page', page => page.on('pageerror', error => errors.push(error.message)))
    context.on('request', request => {
      const path = new URL(request.url()).pathname
      if (['/api/v1/auth/login', '/api/v1/auth/callback', '/authn-api/v5/cas/platform/login'].includes(path)) loginRequests.push(path)
    })
    context.on('response', response => {
      const url = new URL(response.url()), path = url.pathname
      const expectedDenial = response.status() === 403 && (
        (path === '/api/v1/auth/session' && url.searchParams.get('scope') === 'admin') ||
        (adminAccessRevoked && path.startsWith('/api/v1/admin/'))
      )
      if (response.status() >= 400 && response.status() !== 401 && !expectedDenial) failures.push({ path, status: response.status() })
      const worker = response.headers()['x-app-worker']
      if (worker) workers.add(worker)
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
    await expect(page.getByRole('button', { name: '管理工作台', exact: true })).toBeVisible()
    await checkLayout('user-session')
    await verifyWorkerSessions(200)
    const cookie = (await context.cookies()).find(cookie => cookie.name === 'ach_session')
    expect(cookie).toBeTruthy()
    expect(cookie.httpOnly).toBe(true)
    expect(cookie.sameSite).toBe('Lax')
    await page.goto(baseURL + '/feedback')
    // 反馈现已持久化，完整提交与 CSRF 验收移至 verify_feedback_browser.mjs。
    await expect(page.getByRole('button', { name: '提交反馈', exact: true })).toBeEnabled()
    const initialLoginRequests = loginRequests.length
    await page.getByRole('button', { name: '管理工作台', exact: true }).click()
    await page.waitForURL(baseURL + '/admin/applications')
    await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '统一身份登录', exact: true })).toHaveCount(0)
    expect(context.pages()).toHaveLength(1)
    // 已登录时即便打开收藏的登录地址，也应直接返回原管理页面。
    await page.goto(baseURL + '/admin/login?redirect=' + encodeURIComponent('/admin/services'))
    await page.waitForURL(baseURL + '/admin/services')
    await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
    await expect(page.locator('.el-table__body tbody tr')).toHaveCount(3)
    const dependencies = await context.request.get(baseURL + '/api/v1/health/dependencies')
    expect(dependencies.status()).toBe(200)
    expect((await dependencies.json()).dependencies).toEqual({ database: 'available', redis: 'available', minio: 'available' })
    await checkLayout('admin-session')
    expect(loginRequests.length).toBe(initialLoginRequests)
    expect((await context.cookies()).filter(cookie => cookie.name.startsWith('ach_session')).map(cookie => cookie.name)).toEqual(['ach_session'])
    // 切换入口共用当前标签页；另行打开页面仅用于验证多个标签页的退出同步。
    const userPage = await context.newPage()
    await userPage.goto(baseURL + '/feedback')
    await expect(userPage.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
    await page.bringToFront()
    const logoutResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/auth/logout')
    await page.getByRole('button', { name: '退出登录', exact: true }).click()
    expect((await logoutResponse).status()).toBe(200)
    await expect(page.getByText('已退出登录', { exact: true })).toBeVisible()
    await verifyWorkerSessions(401)
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
    const reverseLoginRequests = loginRequests.length
    if (name === 'mobile') await page.getByRole('button', { name: '打开管理导航', exact: true }).click()
    await page.getByRole('button', { name: '打开用户端', exact: true }).filter({ visible: true }).click()
    await page.waitForURL(baseURL + '/')
    await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
    expect(context.pages()).toHaveLength(1)
    await page.goto(baseURL + '/login?redirect=' + encodeURIComponent('/models?q=AI'))
    await page.waitForURL(baseURL + '/models?q=AI')
    await checkLayout('admin-to-user')
    expect(loginRequests.length).toBe(reverseLoginRequests)
    const adminPage = await context.newPage()
    await adminPage.goto(baseURL + '/admin/services')
    await expect(adminPage.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
    await page.bringToFront()
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
    await expect(page.getByRole('button', { name: '管理工作台', exact: true })).toHaveCount(0)
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
    if (verifyIdentitySync) {
      let syncVersion = Date.now(), roleChanged = false, userChanged = false
      const deliver = async payload => {
        const response = await context.request.post(`${apiURL}/api/v1/auth/mock/identity-sync`, {
          data: payload, headers: { 'X-Mock-IAM-Key': syncKey, Connection: 'close' },
        })
        expect(response.status()).toBe(200)
        return response.json()
      }
      const event = (resType, status) => ({
        eventId: randomUUID(), appId: 'platform', action: 'push', resType, syncTime: ++syncVersion,
        data: [{ subject: 'mock-admin', status, ...(resType === 'userlinkedrole' ? { roleCode: 'platform_admin' } : {}) }],
      })
      const refresh = async target => {
        // 上次路由或焦点检查完成后再触发，避免复用身份事件提交前已发出的会话请求。
        await target.waitForLoadState('networkidle')
        await target.bringToFront()
        await expect.poll(() => target.evaluate(() => document.visibilityState)).toBe('visible')
        const checked = target.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/auth/session')
        await target.evaluate(() => window.dispatchEvent(new Event('focus')))
        await checked
        await target.waitForLoadState('networkidle')
      }
      try {
        const revoke = event('userlinkedrole', 'unauthorized')
        // 撤权期间，在途管理请求返回 403 正是预期的后端权限拦截。
        adminAccessRevoked = true
        const revoked = await deliver(revoke)
        roleChanged = revoked.applied === 1
        expect(roleChanged).toBe(true)
        expect((await deliver(revoke)).duplicate).toBe(true)
        await refresh(page)
        await expect(page.getByRole('button', { name: '管理工作台', exact: true })).toHaveCount(0)
        await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
        await refresh(deniedPage)
        await expect(deniedPage.getByRole('alert')).toContainText('当前账号无管理端访问权限')
        await deniedPage.screenshot({ path: resolve(output, `${name}-identity-admin-revoked.png`), fullPage: true })
        const stale = { ...event('userlinkedrole', 'authorized'), syncTime: revoke.syncTime - 1 }
        expect((await deliver(stale)).ignored).toBe(1)
        expect((await context.request.get(baseURL + '/api/v1/auth/session?scope=admin')).status()).toBe(403)
        expect((await deliver(event('userlinkedrole', 'authorized'))).applied).toBe(1)
        roleChanged = false
        await refresh(deniedPage)
        await deniedPage.waitForURL(baseURL + '/admin/feedback')
        await expect(deniedPage.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
        await deniedPage.waitForLoadState('networkidle')
        adminAccessRevoked = false
        const disabled = await deliver(event('user', 'disabled'))
        userChanged = disabled.applied === 1
        expect(userChanged).toBe(true)
        for (const target of [page, deniedPage]) {
          await refresh(target)
          await expect(target.getByRole('alert')).toContainText('账号已停用')
          await expect(target.getByRole('button', { name: '退出登录', exact: true })).toHaveCount(0)
        }
        await checkLayout('identity-user-disabled')
        expect((await deliver(event('user', 'active'))).applied).toBe(1)
        userChanged = false
        // 恢复账号不会复活已撤销的旧会话，需要重新完成登录。
        for (const scope of ['user', 'admin']) {
          expect((await context.request.get(baseURL + '/api/v1/auth/session?scope=' + scope)).status()).toBe(401)
        }
        await page.getByRole('button', { name: '统一身份登录', exact: true }).click()
        await page.waitForURL(baseURL + '/models')
        await expect(page.getByRole('button', { name: '管理工作台', exact: true })).toBeVisible()
        await refresh(deniedPage)
        await deniedPage.waitForURL(baseURL + '/admin/feedback')
        await expect(deniedPage.getByRole('button', { name: '退出登录', exact: true })).toBeVisible()
      } finally {
        // 即使断言中途失败，也恢复本次改动的模拟账号，保证演示入口继续可用。
        if (userChanged) expect((await deliver(event('user', 'active'))).applied).toBe(1)
        if (roleChanged) expect((await deliver(event('userlinkedrole', 'authorized'))).applied).toBe(1)
        adminAccessRevoked = false
      }
    }
    expect(errors).toEqual([])
    expect(failures).toEqual([])
    expect(workers.size).toBe(expectedWorkers)
    results.push({ viewport: name, nativeLinkInterception: true, sameTabSwitchBothDirections: true, sharedLoginBothDirections: true, loginReturnPath: true, workers: workers.size, logoutBothDirections: true, csrfSubmission: true, adminDenied: true, accountSwitchShared: true, identitySync: verifyIdentitySync, errors: 0 })
    await context.close()
  }
  await writeFile(resolve(output, 'browser-results.json'), JSON.stringify(results, null, 2))
  console.log(JSON.stringify(results))
} finally { await browser.close() }

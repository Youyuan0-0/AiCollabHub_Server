import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { randomUUID } from 'node:crypto'

// 本脚本创建明确标记的联调数据；记录编号用于验证后的定向清理。
const require = createRequire(resolve('../AiCollabHub_User/package.json'))
const { chromium, expect } = require('@playwright/test')
const base = process.env.FEEDBACK_VERIFY_URL || 'http://127.0.0.1:18980'
const output = resolve('.runtime/feedback-preview/verification')
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || undefined, headless: true })
const errors = []
const recordIds = []
const results = []
const runId = randomUUID().slice(0, 8)
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB1kAAAAASUVORK5CYII=', 'base64')
const title = `反馈联调验收-${runId}`

/** 使用现有模拟认证真实登录，不绕过业务权限。 */
async function login(context, path, administrator) {
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  page.on('response', async response => {
    if (response.url().includes('/api/v1/feedback') && response.status() >= 500)
      errors.push(`feedback HTTP ${response.status()}: ${await response.text()}`)
  })
  await page.goto(base + path)
  await expect(page.getByRole('heading', { name: '模拟统一认证' })).toBeVisible()
  await page.getByLabel(administrator ? '模拟管理员A' : '模拟用户A', { exact: true }).check()
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL(base + path)
  return page
}

/** 保存布局截图，同时检查页面没有超出可视区域。 */
async function screenshot(page, name) {
  await page.waitForLoadState('networkidle')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), name).toBe(true)
  const dialog = page.getByRole('dialog', { name: '反馈详情' })
  const hasDialog = await dialog.isVisible()
  if (hasDialog) {
    await expect.poll(async () => {
      const box = await dialog.boundingBox()
      return box && box.x >= -1 && box.x + box.width <= page.viewportSize().width + 1
    }).toBe(true)
  }
  await page.screenshot({ path: resolve(output, name + '.png'), fullPage: !hasDialog, animations: 'disabled' })
}

/** 提前写入已创建编号，失败后仍可以只清理本次测试资料。 */
async function remember(id) {
  recordIds.push(id)
  await writeFile(resolve(output, `created-records-${runId}.json`), JSON.stringify({ runId, recordIds }, null, 2))
}

try {
  const user = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const page = await login(user, '/feedback', false)
  await expect(page.getByRole('button', { name: '提交反馈', exact: true })).toBeEnabled()
  await page.getByPlaceholder('概括您遇到的问题').fill(title)
  await page.getByPlaceholder('请说明操作过程、预期结果和实际情况').fill('联调验收记录：验证反馈提交、三个附件、处理历史和状态同步。')
  await page.getByRole('button', { name: '提交反馈', exact: true }).click()
  expect(await page.getByPlaceholder('手机号、邮箱或内网分机').evaluate(input => input.validity.valueMissing)).toBe(true)
  await page.getByPlaceholder('手机号、邮箱或内网分机').fill('测试分机 8001')
  const files = [1, 2, 3, 4].map(index => ({ name: `验收附件-${index}.png`, mimeType: 'image/png', buffer: png }))
  await page.locator('input[type=file]').setInputFiles(files)
  await expect(page.getByRole('alert')).toContainText('最多 3 个')
  await page.locator('input[type=file]').setInputFiles(files.slice(0, 3))
  const posted = page.waitForResponse(response => response.url() === base + '/api/v1/feedback' && response.request().method() === 'POST')
  await page.getByRole('button', { name: '提交反馈', exact: true }).click()
  const submitted = await posted
  expect(submitted.status(), await submitted.text()).toBe(201)
  const record = await submitted.json()
  await remember(record.id)
  expect(record.attachments.length).toBe(3)
  await expect(page.getByRole('status').filter({ hasText: '反馈已提交' })).toBeVisible()
  await expect(page.getByPlaceholder('概括您遇到的问题')).toHaveValue('')
  await page.reload()
  await page.getByRole('button', { name: title, exact: true }).click()
  const drawer = page.getByRole('dialog', { name: '反馈详情' })
  await expect(drawer.getByRole('link')).toHaveCount(3)
  const downloadUrl = await drawer.getByRole('link').first().getAttribute('href')
  const downloaded = await user.request.get(base + downloadUrl)
  expect(downloaded.status()).toBe(200)
  expect(await downloaded.body()).toEqual(png)
  const downloadEvent = page.waitForEvent('download')
  await drawer.getByRole('link').first().click()
  expect((await downloadEvent).suggestedFilename()).toMatch(/^验收附件-/)
  await screenshot(page, 'user-submitted-detail')
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  results.push('用户必填校验、3 个 MinIO 附件、提交、刷新和下载通过')

  const admin = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const adminPage = await login(admin, '/admin/feedback', true)
  await adminPage.getByRole('textbox', { name: '搜索反馈标题' }).fill(title)
  await adminPage.getByRole('button', { name: '查询', exact: true }).click()
  await adminPage.getByRole('button', { name: title, exact: true }).click()
  const adminDrawer = adminPage.getByRole('dialog', { name: '反馈详情' })
  await expect(adminDrawer.getByRole('link')).toHaveCount(3)
  await adminDrawer.getByRole('textbox', { name: '处理说明' }).fill('已收到反馈，正在核查问题。')
  await adminDrawer.getByRole('button', { name: '保存处理结果', exact: true }).click()
  await expect(adminDrawer.getByRole('status').filter({ hasText: '处理结果已保存' })).toBeVisible()
  await adminDrawer.getByRole('combobox', { name: '处理状态' }).focus()
  await adminDrawer.getByRole('combobox', { name: '处理状态' }).press('Enter')
  await adminPage.getByRole('option', { name: '已解决', exact: true }).click()
  await adminDrawer.getByRole('textbox', { name: '处理说明' }).fill('已完成处理，验收通过。')
  const processed = adminPage.waitForResponse(response => response.request().method() === 'PATCH' && response.url().endsWith('/' + record.id))
  await adminDrawer.getByRole('button', { name: '保存处理结果', exact: true }).click()
  expect((await processed).status()).toBe(200)
  await expect(adminDrawer.locator('.feedback-event-list li')).toHaveCount(3)
  await screenshot(adminPage, 'admin-resolved-detail')
  await page.getByRole('button', { name: '刷新记录', exact: true }).click()
  const card = page.locator('.feedback-history-list article').filter({ hasText: title })
  await expect(card).toContainText('已解决')
  await expect(card).toContainText('已完成处理，验收通过。')
  await screenshot(page, 'user-resolved-list')
  await page.getByRole('button', { name: title, exact: true }).click()
  await expect(drawer.locator('.feedback-events li')).toHaveCount(3)
  results.push('后台查询、追加回复、解决问题，以及用户处理历史同步通过')

  const adminUserRecords = await admin.request.get(base + '/api/v1/feedback')
  expect((await adminUserRecords.json()).items.some(item => item.id === record.id)).toBe(false)
  expect((await admin.request.get(base + downloadUrl)).status()).toBe(404)
  expect((await user.request.get(base + '/api/v1/admin/feedback')).status()).toBe(403)
  const anon = await browser.newContext()
  expect((await anon.request.get(base + downloadUrl)).status()).toBe(401)
  await anon.close()
  results.push('用户记录隔离、下载权限和后台权限通过')

  const userSession = await (await user.request.get(base + '/api/v1/auth/session?scope=user')).json()
  const adminSession = await (await admin.request.get(base + '/api/v1/auth/session?scope=admin')).json()
  const headers = { 'X-CSRF-Token': userSession.csrfToken, Origin: base }
  const adminHeaders = { 'X-CSRF-Token': adminSession.csrfToken, Origin: base }
  const payload = { type: 'function_error', module: 'home', title: `反馈并发验收-${runId}`, description: '并发重复提交及版本冲突验收', contact: '测试分机 8001', clientRequestId: randomUUID(), attachmentIds: [] }
  const duplicates = await Promise.all([1, 2].map(() => user.request.post(base + '/api/v1/feedback', { data: payload, headers })))
  expect(duplicates.map(response => response.status()).sort()).toEqual([200, 201])
  const duplicateValues = await Promise.all(duplicates.map(response => response.json()))
  expect(duplicateValues[0].id).toBe(duplicateValues[1].id)
  await remember(duplicateValues[0].id)
  const updates = [1, 2].map(index => ({ status: 'resolved', reply: `并发处理 ${index}`, expectedVersion: 1, clientRequestId: randomUUID() }))
  const updateUrl = base + '/api/v1/admin/feedback/' + duplicateValues[0].id
  const conflicts = await Promise.all(updates.map(data => admin.request.patch(updateUrl, { data, headers: adminHeaders })))
  expect(conflicts.map(response => response.status()).sort()).toEqual([200, 409])
  const successful = conflicts.findIndex(response => response.status() === 200)
  const replay = await admin.request.patch(updateUrl, { data: updates[successful], headers: adminHeaders })
  expect(replay.status()).toBe(200)
  expect((await replay.json()).events.length).toBe(2)
  results.push('真实 PostgreSQL 并发重复提交、版本冲突和处理重试通过')

  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: title, exact: true }).click()
  await screenshot(page, 'user-mobile-detail')
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await screenshot(page, 'user-mobile-form')
  await adminPage.keyboard.press('Escape')
  await expect(adminDrawer).toBeHidden()
  await adminPage.setViewportSize({ width: 390, height: 844 })
  await adminPage.getByRole('button', { name: title, exact: true }).click()
  await screenshot(adminPage, 'admin-mobile-detail')
  expect(errors).toEqual([])
  results.push('桌面、390px 移动端布局及页面运行无异常')
  await writeFile(resolve(output, 'result.json'), JSON.stringify({ passed: true, runId, recordIds, results }, null, 2))
  console.log(JSON.stringify({ passed: true, results, evidence: output }))
} catch (error) {
  for (const [index, context] of browser.contexts().entries()) {
    const page = context.pages()[0]
    if (page) {
      await page.screenshot({ path: resolve(output, `failure-${index}.png`), fullPage: true })
      console.log(JSON.stringify({ index, alerts: await page.getByRole('alert').allTextContents(), pageErrors: errors }))
    }
  }
  await writeFile(resolve(output, 'result.json'), JSON.stringify({ passed: false, runId, recordIds, results, error: String(error), pageErrors: errors }, null, 2))
  throw error
} finally {
  await browser.close()
}

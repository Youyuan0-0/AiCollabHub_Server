import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'

// 登录使用本地模拟 IAM；反馈响应仅在浏览器内模拟，不创建或修改数据库记录。
const require = createRequire(resolve('../AiCollabHub_User/package.json'))
const { chromium, expect } = require('@playwright/test')
const base = process.env.FEEDBACK_VERIFY_URL || 'http://127.0.0.1:18980'
const output = resolve('.runtime/feedback-ui-fixes-20260917')
await mkdir(output, { recursive: true })
const report = { checks: [], layouts: [], pageErrors: [], completed: false }
const browser = await chromium.launch({ headless: true })
const title = '反馈界面修复验证'
const longName = 'feedback_' + 'x'.repeat(175) + '.png'
const contact = 'a'.repeat(188) + '@example.com'
const record = {
  id: '00000000-0000-4000-8000-000000000001',
  title, contact, ownerName: '界面测试用户',
  type: 'function_error', typeLabel: '功能异常', typeOther: null,
  module: 'home', moduleLabel: '首页 / 导航', moduleOther: null,
  description: '检查长联系方式、附件名、未保存提醒和固定保存按钮。',
  status: 'processing', version: 3, reply: '',
  createdAt: '2026-09-17T04:00:00Z', updatedAt: '2026-09-17T04:00:00Z',
  attachments: [{ id: 'ui-file', name: longName, size: 68, state: 'attached', expiresAt: null }],
  events: [3, 2, 1].map(version => ({
    id: `ui-event-${version}`, actorName: '界面测试用户', action: 'submitted',
    content: '反馈处理历史验证。'.repeat(10), fromStatus: null, toStatus: 'processing',
    version, createdAt: '2026-09-17T04:00:00Z',
  })),
  nextEventVersion: null,
}
let failNextSave = false
let submissionCount = 0
let updateCount = 0

/** 安装固定长字段和可控制的保存响应，隔离真实反馈数据。 */
async function installFixture(page) {
  await page.route('**/api/v1/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v1/admin/overview')
      return route.fulfill({ json: { total: 1, open: record.status === 'processing' ? 1 : 0, resolved: record.status === 'resolved' ? 1 : 0 } })
    if (!/^\/api\/v1\/(admin\/)?feedback(\/|$)/.test(path)) return route.continue()
    if (path.endsWith('/config')) return route.fulfill({ json: {
      types: [{ value: 'function_error', label: '功能异常' }, { value: 'other', label: '其他' }],
      modules: [{ value: 'home', label: '首页 / 导航' }, { value: 'other', label: '其他' }],
      maxAttachments: 3, maxFileBytes: 1024 ** 3,
      extensions: ['.png', '.jpg', '.jpeg', '.pdf', '.docx'],
    } })
    if (path.includes('/submissions/') || path.includes('/uploads/'))
      return route.fulfill({ status: 404, json: { detail: '尚未查到' } })
    if (request.method() === 'POST' && path === '/api/v1/feedback') {
      submissionCount++
      return route.fulfill({ status: 201, json: { ...record, ...request.postDataJSON(), attachments: [] } })
    }
    if (request.method() === 'PATCH') {
      updateCount++
      if (failNextSave) {
        failNextSave = false
        return route.fulfill({ status: 422, json: { detail: '模拟保存失败，请重试' } })
      }
      const input = request.postDataJSON()
      record.status = input.status
      record.reply = input.reply
      record.version++
      record.events.unshift({ ...record.events[0], id: `ui-event-${record.version}`, version: record.version, action: 'resolved', content: input.reply })
      return route.fulfill({ json: record })
    }
    if (request.method() !== 'GET') throw new Error(`未预期的写入：${request.method()} ${path}`)
    if (path.endsWith('/feedback')) return route.fulfill({ json: { items: [record], total: 1, page: 1, pageSize: 10 } })
    return route.fulfill({ json: record })
  })
}

/** 分别登录用户端和管理端，加载重新构建的网页。 */
async function login(path, admin) {
  const context = await browser.newContext({ viewport: { width: 1366, height: 768 } })
  const page = await context.newPage()
  page.setDefaultTimeout(15000)
  page.on('pageerror', error => report.pageErrors.push(error.message))
  await installFixture(page)
  await page.goto(base + path)
  await page.getByRole('button', { name: '统一身份登录', exact: true }).click()
  await page.getByLabel(admin ? '模拟管理员A' : '模拟用户A', { exact: true }).check()
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL(base + path)
  return page
}

/** 真实触发浏览器确认，检查取消与确认各自的行为。 */
async function confirmAction(page, action, accept, type = 'confirm') {
  const result = page.waitForEvent('dialog').then(async dialog => {
    const actualType = dialog.type()
    if (accept) await dialog.accept()
    else await dialog.dismiss()
    expect(actualType, page.url()).toBe(type)
  })
  await Promise.all([result, action()])
}

/** 等待详情渲染完成，避免把加载态当作布局检查结果。 */
async function open(page) {
  await page.getByRole('button', { name: title, exact: true }).click()
  const drawer = page.getByRole('dialog', { name: '反馈详情' })
  await expect(drawer.getByRole('heading', { name: title, exact: true })).toBeVisible()
  return drawer
}

/** 检查抽屉内部宽度、文字边界及保存按钮的可见范围，并保存截图。 */
async function layout(page, drawer, name, admin) {
  await drawer.locator('.el-drawer__body').evaluate(el => { el.scrollTop = 0; el.scrollLeft = 0 })
  await expect.poll(() => drawer.evaluate(el => {
    const rect = el.getBoundingClientRect()
    return rect.left >= -1 && rect.right <= innerWidth + 1
  })).toBe(true)
  const data = await drawer.evaluate((el) => {
    const body = el.querySelector('.el-drawer__body')
    return { viewport: innerWidth, bodyWidth: body.clientWidth, scrollWidth: body.scrollWidth }
  })
  expect(data.scrollWidth, name).toBeLessThanOrEqual(data.bodyWidth + 1)
  if (admin) {
    const save = await drawer.getByRole('button', { name: '保存处理结果', exact: true }).boundingBox()
    expect(save.y).toBeGreaterThanOrEqual(0)
    expect(save.y + save.height).toBeLessThanOrEqual(page.viewportSize().height)
    await drawer.locator('.el-drawer__body').evaluate(el => { el.scrollTop = el.scrollHeight })
    const after = await drawer.getByRole('button', { name: '保存处理结果', exact: true }).boundingBox()
    expect(Math.abs(after.y - save.y)).toBeLessThan(1)
    await drawer.locator('.el-drawer__body').evaluate(el => { el.scrollTop = 0 })
  }
  report.layouts.push({ name, ...data })
  await page.screenshot({ path: resolve(output, name + '.png'), animations: 'disabled' })
}

try {
  const user = await login('/feedback', false)
  await expect(user.getByRole('button', { name: '提交反馈', exact: true })).toBeEnabled()
  const home = () => user.getByRole('link', { name: '首页', exact: true }).first().click()
  const titleInput = user.getByPlaceholder('概括您遇到的问题')
  await titleInput.fill('未提交的反馈')
  await user.getByPlaceholder('请说明操作过程、预期结果和实际情况').fill('取消离开应保留这些内容。')
  await user.getByPlaceholder('手机号、邮箱或内网分机').fill('分机8001')
  await confirmAction(user, home, false)
  await expect(user).toHaveURL(base + '/feedback')
  await expect(titleInput).toHaveValue('未提交的反馈')
  await user.screenshot({ path: resolve(output, 'user-cancel-leave-keeps-input.png'), fullPage: true })
  await confirmAction(user, () => user.reload().catch(() => {}), false, 'beforeunload')
  await expect(titleInput).toHaveValue('未提交的反馈')
  await confirmAction(user, home, true)
  await user.waitForURL(base + '/')
  await user.goBack()
  await expect(titleInput).toHaveValue('')
  report.checks.push('用户路由离开和刷新有提醒，取消保留输入，确认后才离开')

  await user.locator('.feedback-type-options label').filter({ hasText: '其他' }).click()
  await confirmAction(user, home, false)
  await user.locator('.feedback-type-options label').filter({ hasText: '功能异常' }).click()
  await user.locator('.platform-select .el-select__wrapper').click()
  await user.getByRole('option', { name: '其他', exact: true }).click()
  await confirmAction(user, home, false)
  await user.locator('.platform-select .el-select__wrapper').click()
  await user.getByRole('option', { name: '首页 / 导航', exact: true }).click()
  await user.locator('input[type=file]').setInputFiles({ name: 'selected.png', mimeType: 'image/png', buffer: Buffer.from('ui-only') })
  await confirmAction(user, home, false)
  await user.getByRole('button', { name: '移除 selected.png', exact: true }).click()
  await expect(user.locator('.feedback-upload-item')).toHaveCount(0)
  report.checks.push('仅修改类型、模块或仅选择附件也会触发未提交提醒')

  await titleInput.fill('成功后不再提示')
  await user.getByPlaceholder('请说明操作过程、预期结果和实际情况').fill('模拟成功提交')
  await user.getByPlaceholder('手机号、邮箱或内网分机').fill('8001')
  await user.getByRole('button', { name: '提交反馈', exact: true }).click()
  await expect(titleInput).toHaveValue('')
  expect(submissionCount).toBe(1)
  await home()
  await user.waitForURL(base + '/')
  await user.goBack()
  report.checks.push('确认提交成功后解除离开保护，未重复提交')
  const userDrawer = await open(user)
  for (const [width, height] of [[1440, 1000], [1366, 768], [390, 844], [320, 844]]) {
    await user.setViewportSize({ width, height })
    await layout(user, userDrawer, `user-detail-${width}`, false)
  }
  report.checks.push('用户详情的 200 字符联系方式及长附件名在四种尺寸下无横向溢出')

  const admin = await login('/admin/feedback', true)
  await admin.getByRole('menuitem', { name: '服务状态', exact: true }).click()
  await admin.waitForURL(base + '/admin/services')
  await admin.getByRole('menuitem', { name: '反馈处理', exact: true }).click()
  await admin.waitForURL(base + '/admin/feedback')
  let drawer = await open(admin)
  const reply = () => drawer.getByRole('textbox', { name: '处理说明' })
  await reply().fill('取消关闭后应保留的回复')
  for (const action of [
    () => drawer.locator('.el-drawer__close-btn').click(),
    () => admin.keyboard.press('Escape'),
    () => admin.mouse.click(30, 100),
    () => drawer.getByRole('button', { name: '关闭', exact: true }).click(),
  ]) {
    await confirmAction(admin, action, false)
    await expect(drawer).toBeVisible()
    await expect(reply()).toHaveValue('取消关闭后应保留的回复')
  }
  await confirmAction(admin, () => admin.goBack(), false)
  await expect(admin).toHaveURL(base + '/admin/feedback')
  await expect(reply()).toHaveValue('取消关闭后应保留的回复')
  await confirmAction(admin, () => admin.reload().catch(() => {}), false, 'beforeunload')
  await expect(reply()).toHaveValue('取消关闭后应保留的回复')
  await reply().scrollIntoViewIfNeeded()
  await admin.screenshot({ path: resolve(output, 'admin-cancel-close-keeps-reply.png'), animations: 'disabled' })
  await confirmAction(admin, () => drawer.locator('.el-drawer__close-btn').click(), true)
  await expect(drawer).toBeHidden()
  drawer = await open(admin)
  await expect(reply()).toHaveValue('')
  report.checks.push('后台关闭按钮、Escape、遮罩、底部关闭、路由返回及刷新均保护未保存回复')

  const select = drawer.getByRole('combobox', { name: '处理状态' })
  await select.focus()
  await select.press('Enter')
  await admin.getByRole('option', { name: '已解决', exact: true }).click()
  await confirmAction(admin, () => drawer.locator('.el-drawer__close-btn').click(), false)
  await expect(drawer.locator('.el-form .el-select')).toContainText('已解决')
  report.checks.push('仅修改处理状态也触发关闭提醒，取消后状态保留')

  await reply().fill('验证固定底部按钮可以保存')
  failNextSave = true
  await drawer.getByRole('button', { name: '保存处理结果', exact: true }).click()
  await expect(drawer.getByRole('alert')).toContainText('模拟保存失败')
  await expect(reply()).toHaveValue('验证固定底部按钮可以保存')
  await confirmAction(admin, () => drawer.locator('.el-drawer__close-btn').click(), false)
  await drawer.getByRole('button', { name: '保存处理结果', exact: true }).click()
  await expect(drawer.getByRole('status').filter({ hasText: '处理结果已保存' })).toBeVisible()
  await expect(reply()).toHaveValue('')
  expect(updateCount).toBe(2)
  await admin.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  report.checks.push('后台保存失败保留说明并提示；重试成功后清空输入、更新状态并解除保护')

  drawer = await open(admin)
  for (const [width, height] of [[1440, 1000], [1366, 768], [390, 844], [320, 844]]) {
    await admin.setViewportSize({ width, height })
    await layout(admin, drawer, `admin-detail-${width}`, true)
  }
  report.checks.push('后台 188 字符附件名在四种尺寸下无横向溢出，滚动时保存按钮保持可见')
  expect(report.pageErrors).toEqual([])
  report.completed = true
} catch (error) {
  report.error = String(error)
  for (const [i, context] of browser.contexts().entries()) {
    const page = context.pages()[0]
    if (page) await page.screenshot({ path: resolve(output, `failure-${i}.png`), fullPage: true }).catch(() => {})
  }
  throw error
} finally {
  await writeFile(resolve(output, 'result.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report))
  await browser.close()
}

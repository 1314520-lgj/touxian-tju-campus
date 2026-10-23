/** Real local HTTP + browser UI acceptance against explicitly labelled synthetic data only. No route fixtures. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { loadPlaywright, relativeArtifact } from './playwright.mjs';
const require = createRequire(import.meta.url);
const { chromium } = loadPlaywright();
const base = process.env.CAMPUS_E2E_URL || 'http://127.0.0.1:8015';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname), 'Live acceptance may mutate only a loopback synthetic demo');
const out = path.resolve(process.env.CAMPUS_E2E_OUTPUT || 'campus-source-live-output'); await fs.mkdir(out, { recursive: true });
const run = Date.now() + '-' + crypto.randomUUID().slice(0, 8);
const websiteName = '【合成验收】官网申请-' + run, wechatName = '【合成验收】公众号申请-' + run;
const report = { base, mode: 'Real browser and HTTP server, no route interception or mocked responses. Only explicitly synthetic source records and demo data; no network collection, WeChat authorization, SMTP or Google deployment is tested.', checks: [], pageErrors: [], failedResponses: [], screenshots: [], student_email: 'source-live-' + run + '@example.org', isolated_email: 'source-isolated-' + run + '@example.org' };
const browser = await chromium.launch(); report.browser = browser.version(); let active;
async function visible(locator) { await locator.waitFor({ state: 'visible', timeout: 15000 }); }
async function check(name, operation) { await operation(); report.checks.push(name); console.log('PASS ' + name); }
function track(page) { active = page; page.on('pageerror', (e) => report.pageErrors.push(e.message)); page.on('response', (r) => { if (r.status() >= 500) report.failedResponses.push({ path: new URL(r.url()).pathname, status: r.status() }); }); }
async function json(context, endpoint) { const response = await context.request.get(base + '/api/v1' + endpoint); assert.equal(response.status(), 200, 'GET ' + endpoint); return response.json(); }
async function ready(page) { await page.goto(base + '/campus.html'); await visible(page.locator('.demo-banner')); assert.match(await page.locator('.demo-banner').innerText(), /合成演示数据/); await visible(page.getByRole('heading', { name: '把今天留给真正重要的事' })); }
async function login(page, email) { await page.getByRole('button', { name: '登录', exact: true }).click(); const dialog = page.getByRole('dialog', { name: '登录偷闲 · 天大版' }); await dialog.getByLabel('邮箱', { exact: true }).fill(email); const waiting = page.waitForResponse((r) => r.url().endsWith('/auth/challenges') && r.request().method() === 'POST'); await dialog.getByRole('button', { name: '获取验证码' }).click(); const response = await waiting; assert.equal(response.status(), 201, 'Development login challenge (admin repeats require cooldown)'); await visible(dialog.locator('.dev-note strong')); const code = (await dialog.locator('.dev-note strong').innerText()).trim(); assert.match(code, /^\d{6}$/); await dialog.getByLabel('六位验证码').fill(code); await dialog.getByRole('button', { name: '登录', exact: true }).click(); await dialog.waitFor({ state: 'hidden' }); await visible(page.getByRole('button', { name: '打开我的设置' })); }
async function nav(page, name) { if (await page.getByRole('button', { name: '打开菜单' }).isVisible()) await page.getByRole('button', { name: '打开菜单' }).click(); await page.getByRole('navigation').getByRole('button', { name, exact: true }).click(); }
async function clickHTTP(page, locator, endpoint, method, expected = 200) { const pending = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/v1' + endpoint && r.request().method() === method); await locator.click(); const response = await pending; assert.equal(response.status(), expected, method + ' ' + endpoint + ': ' + await response.text()); return response.json(); }
async function shot(page, name, fullPage = false, preserveScroll = false) { if (!preserveScroll && !(await page.getByRole('dialog').count())) { await page.evaluate(() => window.scrollTo(0, 0)); await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))); } const file = path.join(out, name + '.png'); await page.screenshot({ path: file, fullPage }); report.screenshots.push(relativeArtifact(file)); }
async function noOverflow(page) { assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, 'No horizontal overflow'); }
try {
  const studentContext = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' }); const student = await studentContext.newPage(); track(student);
  const health = await json(studentContext, '/health/ready'); assert.equal(health.demo_data, true, 'Do not mutate any server without an explicit demo marker');
  const sources = (await json(studentContext, '/sources')).items;
  const readySource = (kind) => sources.find((s) => s.kind === kind && s.name.includes('合成验收') && s.enabled && s.health === 'healthy' && s.last_success_at && s.verification !== 'unverified');
  const website = readySource('website'), wechat = readySource('wechat'); assert.ok(website && wechat, 'Requires explicitly synthetic, already-ready website and WeChat records; this test never fetches real data'); report.synthetic_source_ids = [website.id, wechat.id];
  await check('Real development login and selected-empty subscriptions persist over page reload', async () => {
    await ready(student); await login(student, report.student_email); await nav(student, '信息来源');
    await student.getByRole('radio', { name: '只看我选的来源（可 0 个）', exact: true }).check();
    const saved = await clickHTTP(student, student.getByRole('button', { name: '保存来源订阅', exact: true }), '/me/subscriptions', 'PUT'); assert.equal(saved.source_mode, 'selected'); assert.deepEqual(saved.source_ids, []); report.original_event_types = saved.event_types;
    await student.reload(); await nav(student, '信息来源'); await visible(student.getByText('已选 0 个来源', { exact: true })); assert.equal(await student.getByRole('radio', { name: '只看我选的来源（可 0 个）', exact: true }).isChecked(), true);
  });
  await check('Website and name-only WeChat requests reach the real API and show pending status', async () => {
    const form = student.locator('.source-request-form'); await form.getByLabel('来源名称', { exact: true }).fill(websiteName); await form.getByLabel('网站网址', { exact: true }).fill('https://jwc.tju.edu.cn/');
    const site = await clickHTTP(student, form.getByRole('button', { name: '提交接入申请', exact: true }), '/me/source-requests', 'POST', 201); assert.equal(site.status, 'pending'); assert.equal(site.auto_subscribe, true); report.website_request_id = site.id;
    await visible(student.locator('.source-requests .source-request').filter({ hasText: websiteName }).getByText('待接入', { exact: true }));
    await form.getByLabel('来源类型', { exact: true }).selectOption('wechat'); await form.getByLabel('来源名称', { exact: true }).fill(wechatName); assert.equal(await form.getByLabel('公众号文章链接（选填）', { exact: true }).inputValue(), '');
    const account = await clickHTTP(student, form.getByRole('button', { name: '提交接入申请', exact: true }), '/me/source-requests', 'POST', 201); assert.equal(account.kind, 'wechat'); assert.equal(account.url, null); assert.equal(account.status, 'pending'); report.wechat_request_id = account.id;
    await visible(student.locator('.source-requests .source-request').filter({ hasText: wechatName }).getByText('待接入', { exact: true })); await shot(student, 'live-student-pending-desktop', true);
  });
  await check('A different authenticated student cannot list or view another student source requests', async () => {
    const otherContext = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' }); const other = await otherContext.newPage(); track(other); await ready(other); await login(other, report.isolated_email); await nav(other, '信息来源');
    const own = await json(otherContext, '/me/source-requests'); assert.ok(!own.items.some((r) => [report.website_request_id, report.wechat_request_id].includes(r.id))); await visible(other.getByText('尚未提交接入申请。', { exact: true })); assert.equal(await other.getByRole('heading', { name: websiteName, exact: true }).count(), 0);
    const denied = await otherContext.request.get(base + '/api/v1/admin/source-requests'); assert.equal(denied.status(), 403); await otherContext.close();
  });
  const adminContext = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' }); const admin = await adminContext.newPage(); track(admin);
  await check('An administrator connects the website request to the synthetic ready website', async () => {
    await ready(admin); await login(admin, process.env.CAMPUS_E2E_ADMIN || 'demo-admin@tju.edu.cn'); await nav(admin, '管理工作台'); await admin.getByRole('button', { name: '接入申请', exact: true }).click();
    const card = admin.locator('.admin-source.source-request').filter({ hasText: websiteName }); await card.getByRole('button', { name: '处理申请', exact: true }).click(); const dialog = admin.getByRole('dialog', { name: '处理来源接入申请' }); await visible(dialog); await dialog.getByLabel('连接的来源', { exact: true }).selectOption(website.id); await dialog.getByLabel('处理说明', { exact: true }).fill('【合成验收】仅用于本机HTTP和界面验证，连接合成就绪网站；未执行网络采集。');
    const connected = await clickHTTP(admin, dialog.getByRole('button', { name: '确认处理', exact: true }), '/admin/source-requests/' + report.website_request_id + '/resolve', 'POST'); assert.equal(connected.status, 'connected'); assert.equal(connected.source_id, website.id); await dialog.waitFor({ state: 'hidden' }); await visible(card.getByText('已接通', { exact: true }));
  });
  await check('Mobile administrator rejects the WeChat request with an explanation and bounded keyboard focus', async () => {
    await admin.setViewportSize({ width: 390, height: 844 }); const card = admin.locator('.admin-source.source-request').filter({ hasText: wechatName }); await card.getByRole('button', { name: '处理申请', exact: true }).click(); const dialog = admin.getByRole('dialog', { name: '处理来源接入申请' }); await visible(dialog); await admin.waitForFunction(() => Boolean(document.activeElement.closest('.modal')));
    for (const key of ['Shift+Tab', 'Shift+Tab', 'Tab', 'Tab', 'Tab', 'Tab', 'Tab']) { await admin.keyboard.press(key); assert.equal(await admin.evaluate(() => Boolean(document.activeElement.closest('.modal'))), true); }
    await dialog.getByLabel('处理结果', { exact: true }).selectOption('reject'); await dialog.getByLabel('处理说明', { exact: true }).fill('【合成验收】未配置该公众号的授权订阅渠道，本次不接入；未执行微信采集。'); await noOverflow(admin); await shot(admin, 'live-admin-rejection-mobile');
    const rejected = await clickHTTP(admin, dialog.getByRole('button', { name: '确认处理', exact: true }), '/admin/source-requests/' + report.wechat_request_id + '/resolve', 'POST'); assert.equal(rejected.status, 'rejected'); assert.equal(rejected.source_id, null); await dialog.waitFor({ state: 'hidden' }); await visible(card.getByText('未接通', { exact: true }));
  });
  await check('Refreshing the student page shows resolved statuses and automatically selected website', async () => {
    active = student; await student.reload(); await nav(student, '信息来源'); const connectedCard = student.locator('.source-requests .source-request').filter({ hasText: websiteName }), rejectedCard = student.locator('.source-requests .source-request').filter({ hasText: wechatName });
    await visible(connectedCard.getByText('已接通', { exact: true })); await visible(rejectedCard.getByText('未接通', { exact: true })); assert.match(await rejectedCard.innerText(), /未配置该公众号的授权订阅渠道/);
    const subscriptions = await json(studentContext, '/me/subscriptions'); assert.equal(subscriptions.source_mode, 'selected'); assert.deepEqual(subscriptions.source_ids, [website.id]); assert.deepEqual(subscriptions.event_types, report.original_event_types); assert.equal(await student.getByRole('checkbox', { name: '关注' + website.name, exact: true }).isChecked(), true); assert.equal(await student.getByRole('checkbox', { name: '关注' + wechat.name, exact: true }).isChecked(), false);
    await shot(student, 'live-student-resolved-desktop', true); await student.setViewportSize({ width: 390, height: 844 }); await noOverflow(student); await shot(student, 'live-student-resolved-mobile');
  });
  await check('Administrator sees bounded backlog and coverage warning through the real API on mobile', async () => {
    active = admin; await admin.getByRole('button', { name: '信息来源', exact: true }).click();
    const card = admin.locator('.admin-source').filter({ hasText: website.name });
    await visible(card.getByText('当前采集窗口：已完成 4 条 · 待补采 2 条 · 失败待处理 0 条', { exact: true }));
    await visible(card.getByText('列表超过单次发现上限，尚未覆盖全部历史通知，请查看原站。', { exact: true }));
    const summary = (await json(adminContext, '/admin/sources')).items.find((s) => s.id === website.id).ingestion;
    assert.equal(summary.completed_count, 4); assert.equal(summary.pending_count, 2); assert.equal(summary.window_truncated, true);
    assert.ok((await json(studentContext, '/sources')).items.every((s) => !Object.hasOwn(s, 'ingestion')));
    await card.scrollIntoViewIfNeeded(); await noOverflow(admin); await shot(admin, 'live-admin-backlog-mobile', false, true);
  });
  await check('No browser exceptions or HTTP 5xx occurred during real API acceptance', async () => { assert.deepEqual(report.pageErrors, []); assert.deepEqual(report.failedResponses, []); });
  report.status = 'passed';
} catch (error) { report.status = 'failed'; report.error = error.stack; console.error(error.stack); if (active) await shot(active, 'live-failure', false).catch(() => {}); process.exitCode = 1; }
finally { await fs.writeFile(path.join(out, 'source-live-report.json'), JSON.stringify(report, null, 2)); await browser.close(); }

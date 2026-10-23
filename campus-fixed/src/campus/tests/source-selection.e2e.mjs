/** Explicit route fixtures for source selection UI. No claims about real crawling, auth or deployment. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { loadPlaywright, relativeArtifact } from './playwright.mjs';
const require = createRequire(import.meta.url);
const { chromium } = loadPlaywright();
const base = process.env.CAMPUS_E2E_URL || 'http://127.0.0.1:8014';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname), 'Fixture test must use a local preview');
const out = path.resolve(process.env.CAMPUS_E2E_OUTPUT || 'campus-source-selection-output'); await fs.mkdir(out, { recursive: true });
const report = { fixture_mode: 'Explicit route fixtures; verifies UI behavior only. Real source feeds, authorization and Cloud Run are not exercised.', checks: [], errors: [], screenshots: [] };
const browser = await chromium.launch(); let active;
const pageResponse = (items) => ({ items, next_cursor: null });
const uid = (n) => '10000000-0000-4000-8000-' + String(n).padStart(12, '0');
const timestamp = '2026-10-08T08:00:00Z';
const makeSource = (n, name, kind, origin_url, ready = true) => ({ id: uid(n), name, kind, origin_url, verification: ready ? 'verified_official' : 'unverified', enabled: true, health: ready ? 'healthy' : 'never_checked', last_success_at: ready ? timestamp : null, last_checked_at: timestamp, adapter: kind, last_error_code: null });
const sources = [makeSource(1, '天津大学教务处', 'website', 'https://jwc.tju.edu.cn/'), makeSource(2, '天津大学公众号', 'wechat', 'https://mp.weixin.qq.com/s/synthetic'), makeSource(3, '校园 RSS 通知', 'rss', 'https://rss.tju.edu.cn/'), makeSource(4, '待授权公众号', 'wechat', 'https://mp.weixin.qq.com/s/unready', false), makeSource(5, '其他授权渠道', 'authorized_feed', 'https://feed.example.org/')];
let role = 'student', subscriptions = { source_ids: [], event_types: ['scholarship'] }, failResolution = true;
const requests = [], writes = [], unexpected = [];
const user = () => ({ id: uid(99), email: role + '@example.org', role });
async function visible(locator) { await locator.waitFor({ state: 'visible' }); }
async function check(name, operation) { await operation(); report.checks.push(name); console.log('PASS ' + name); }
async function shot(page, name) { if (!(await page.getByRole('dialog').count())) { await page.evaluate(() => window.scrollTo(0, 0)); await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))); } const file = path.join(out, name + '.png'); await page.screenshot({ path: file, fullPage: page.viewportSize().width > 640 }); report.screenshots.push(relativeArtifact(file)); }
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' });
  await context.route(base + '/api/v1/**', async (route) => {
    const req = route.request(), url = new URL(req.url()), p = url.pathname.slice('/api/v1'.length), method = req.method();
    const json = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (!['GET', 'HEAD'].includes(method)) { assert.equal(req.headers()['x-csrf-token'], 'source-ui-csrf'); writes.push({ path: p, body: req.postDataJSON(), key: req.headers()['idempotency-key'] }); }
    if (p === '/me') return json({ user: user(), csrf_token: 'source-ui-csrf' });
    if (p === '/catalog') return json({ colleges: [], education_levels: [], campuses: [] });
    if (p === '/health/ready') return json({ status: 'ready', demo_data: true });
    if (p === '/sources' || p === '/admin/sources') return json(pageResponse(sources));
    if (p === '/me/subscriptions') { if (method === 'PUT') { subscriptions = req.postDataJSON(); } return json(subscriptions); }
    if (p === '/me/profile') return json({ college_id: null, major: null, entry_year: null, education_level: 'unknown', campus: 'unknown', interests: [] });
    if (p === '/me/preferences') return json({ daily_digest_enabled: false, digest_time: '08:00', date_only_reminder_time: '09:00', reminders_enabled: true, reminder_lead_minutes: 1440, change_reminders_enabled: true, quiet_start: '22:00', quiet_end: '08:00', channels: ['in_app'], calendar_enabled: false });
    if (['/me/feed', '/me/items', '/events', '/admin/reviews', '/admin/jobs'].includes(p)) return json(pageResponse([]));
    if (p === '/me/source-requests' && method === 'POST') { assert.ok(req.headers()['idempotency-key']); const value = { ...req.postDataJSON(), id: uid(100 + requests.length), user_id: uid(99), status: 'pending', source_id: null, resolution_note: null, created_at: timestamp, updated_at: timestamp }; requests.unshift(value); return json(value, 201); }
    if (p === '/me/source-requests' || p === '/admin/source-requests') return json(pageResponse(requests));
    const resolution = p.match(/^\/admin\/source-requests\/([^/]+)\/resolve$/);
    if (resolution) { assert.ok(req.headers()['idempotency-key']); const item = requests.find((r) => r.id === resolution[1]), body = req.postDataJSON(); if (failResolution && body.decision === 'connect') { failResolution = false; return json({ code: 'SOURCE_NOT_READY', message: '该来源尚未就绪，请重新核对采集状态。', request_id: uid(500) }, 409); } Object.assign(item, { source_id: body.source_id, status: body.decision === 'connect' ? 'connected' : 'rejected', resolution_note: body.note, updated_at: timestamp }); return json(item); }
    unexpected.push(method + ' ' + p); return json({ code: 'UNEXPECTED_FIXTURE_ROUTE', message: p }, 500);
  });
  const page = active = await context.newPage(); page.on('pageerror', (e) => report.errors.push(e.message));
  async function navigate(name) { if ((await page.viewportSize()).width <= 640) await page.getByRole('button', { name: '打开菜单' }).click(); await page.getByRole('navigation').getByRole('button', { name, exact: true }).click(); }
  await page.goto(base + '/campus.html'); await visible(page.getByRole('heading', { name: '把今天留给真正重要的事' })); await navigate('信息来源');
  await check('Source directory searches name and URL, separates website/WeChat/other and shows actual health', async () => {
    const search = page.getByRole('searchbox', { name: '搜索来源名称或网址' });
    await search.fill('jwc.tju'); assert.equal(await page.locator('.source-choice').count(), 1); await visible(page.getByRole('heading', { name: '天津大学教务处', exact: true }));
    await search.fill('天津大学公众号'); assert.equal(await page.locator('.source-choice').count(), 1); await search.fill('');
    await page.getByRole('button', { name: '网站', exact: true }).click(); assert.equal(await page.locator('.source-choice').count(), 2);
    await page.getByRole('button', { name: '微信公众号', exact: true }).click(); assert.equal(await page.locator('.source-choice').count(), 2); await visible(page.locator('.source-choice').filter({ hasText: '待授权公众号' }).getByText('尚未检查', { exact: true }));
    await page.getByRole('button', { name: '其他来源', exact: true }).click(); assert.equal(await page.locator('.source-choice').count(), 1);
    await page.getByRole('button', { name: '全部类型', exact: true }).click();
  });
  await check('Legacy empty subscriptions mean all; explicit zero saves correctly and preserves newest event categories', async () => {
    assert.equal(await page.getByRole('radio', { name: '全部来源', exact: true }).isChecked(), true);
    assert.equal(await page.getByRole('checkbox', { name: '关注天津大学教务处', exact: true }).isDisabled(), true);
    await page.getByRole('radio', { name: '只看我选的来源（可 0 个）', exact: true }).check();
    subscriptions.event_types = ['activity', 'academic'];
    const firstSave = page.waitForResponse((r) => r.url().endsWith('/me/subscriptions') && r.request().method() === 'PUT'); await page.getByRole('button', { name: '保存来源订阅', exact: true }).click(); await firstSave; await visible(page.locator('.toast').filter({ hasText: '来源订阅已保存' }));
    assert.deepEqual(subscriptions, { source_ids: [], source_mode: 'selected', event_types: ['activity', 'academic'] });
    await visible(page.getByText('已选 0 个来源', { exact: true }));
    await page.getByRole('checkbox', { name: '关注天津大学教务处', exact: true }).check(); const nextSave = page.waitForResponse((r) => r.url().endsWith('/me/subscriptions') && r.request().method() === 'PUT'); await page.getByRole('button', { name: '保存来源订阅', exact: true }).click(); await nextSave;
    assert.deepEqual(subscriptions.source_ids, [sources[0].id]);
  });
  await check('Account subscription settings retain explicit mode while editing categories', async () => {
    await navigate('我的设置'); await page.getByRole('button', { name: '信息订阅', exact: true }).click();
    await visible(page.getByRole('radio', { name: '只看我选的来源（可 0 个）', exact: true })); assert.equal(await page.getByRole('radio', { name: '只看我选的来源（可 0 个）', exact: true }).isChecked(), true);
    await page.getByRole('checkbox', { name: '教务', exact: true }).uncheck(); const save = page.waitForResponse((r) => r.url().endsWith('/me/subscriptions') && r.request().method() === 'PUT'); await page.getByRole('button', { name: '保存订阅', exact: true }).click(); await save; await visible(page.locator('.toast').filter({ hasText: '订阅已保存' }));
    assert.equal(subscriptions.source_mode, 'selected'); assert.deepEqual(subscriptions.source_ids, [sources[0].id]); assert.deepEqual(subscriptions.event_types, ['activity']);
    await navigate('信息来源');
  });
  await check('Students submit website and name-only WeChat requests and see pending state without fabricated crawling', async () => {
    const form = page.locator('.source-request-form');
    await form.getByLabel('来源名称', { exact: true }).fill('希望接入的学院官网');
    await form.getByRole('button', { name: '提交接入申请' }).click(); assert.equal(requests.length, 0);
    await form.getByLabel('网站网址', { exact: true }).fill('https://college.tju.edu.cn/'); await form.getByRole('button', { name: '提交接入申请' }).click();
    await visible(page.locator('.source-requests .source-request').filter({ hasText: '希望接入的学院官网' })); assert.equal(requests[0].url, 'https://college.tju.edu.cn/');
    await form.getByLabel('来源类型', { exact: true }).selectOption('wechat'); await form.getByLabel('来源名称', { exact: true }).fill('尚未提供授权的公众号');
    await form.getByLabel('公众号文章链接（选填）', { exact: true }).fill('https://example.org/wrong'); await form.getByRole('button', { name: '提交接入申请' }).click();
    await visible(form.getByRole('alert')); assert.equal(requests.length, 1);
    await form.getByLabel('公众号文章链接（选填）', { exact: true }).fill(''); await form.getByRole('button', { name: '提交接入申请' }).click();
    const card = page.locator('.source-requests .source-request').filter({ hasText: '尚未提供授权的公众号' }); await visible(card); await visible(card.getByText('待接入', { exact: true }));
    assert.equal(requests[0].url, null); assert.equal(requests[0].auto_subscribe, true); await visible(card.getByText('等待管理员检查并接入；目前尚未开始采集这个申请来源。', { exact: true }));
    await shot(page, 'sources-student-desktop');
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await check('Mobile directory and requests fit viewport and remain reachable through keyboard-friendly menu', async () => {
    await navigate('我的设置'); await navigate('信息来源'); await visible(page.getByRole('heading', { name: '申请接入新来源' }));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false); await shot(page, 'sources-student-mobile');
  });
  role = 'admin'; await page.setViewportSize({ width: 1440, height: 1000 }); await page.reload(); await navigate('管理工作台'); await page.getByRole('button', { name: '接入申请', exact: true }).click();
  await check('Admin connects only ready sources and a server conflict keeps the request unresolved', async () => {
    const card = page.locator('.admin-source.source-request').filter({ hasText: '希望接入的学院官网' }); await card.getByRole('button', { name: '处理申请', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '处理来源接入申请' }); await visible(dialog); await dialog.getByLabel('连接的来源', { exact: true }).selectOption(sources[0].id); await dialog.getByLabel('处理说明', { exact: true }).fill('已核验官网并完成一次采集。');
    await dialog.getByRole('button', { name: '确认处理' }).click(); await visible(dialog.getByRole('alert')); await visible(dialog.getByText('该来源尚未就绪，请重新核对采集状态。', { exact: true })); assert.equal(requests.find((r) => r.kind === 'website').status, 'pending');
    await dialog.getByRole('button', { name: '确认处理' }).click(); await dialog.waitFor({ state: 'hidden' }); await visible(card.getByText('已接通', { exact: true }));
    const item = requests.find((r) => r.kind === 'website'); assert.equal(item.source_id, sources[0].id); assert.equal(item.resolution_note, '已核验官网并完成一次采集。');
  });
  await check('Admin rejects an unavailable WeChat request with a visible note and mobile modal focus containment', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    const card = page.locator('.admin-source.source-request').filter({ hasText: '尚未提供授权的公众号' }); await card.getByRole('button', { name: '处理申请', exact: true }).click(); const dialog = page.getByRole('dialog', { name: '处理来源接入申请' });
    const unready = dialog.getByRole('option', { name: '待授权公众号（尚未就绪）', exact: true }); assert.equal(await unready.getAttribute('disabled'), '', await unready.evaluate((e) => e.outerHTML)); assert.equal(await dialog.getByRole('option', { name: '天津大学教务处', exact: true }).count(), 0);
    await page.waitForFunction(() => Boolean(document.activeElement.closest('.modal')));
    for (let n = 0; n < 10; n++) { await page.keyboard.press(n < 5 ? 'Shift+Tab' : 'Tab'); assert.equal(await page.evaluate(() => Boolean(document.activeElement.closest('.modal'))), true, 'focus escaped at key ' + n + ': ' + await page.evaluate(() => document.activeElement.outerHTML)); }
    await dialog.getByLabel('处理结果', { exact: true }).selectOption('reject'); await dialog.getByLabel('处理说明', { exact: true }).fill('暂未取得可用授权订阅渠道，请补充后重新申请。');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false); await shot(page, 'sources-admin-mobile-resolution');
    await dialog.getByRole('button', { name: '确认处理' }).click(); await dialog.waitFor({ state: 'hidden' }); await visible(card.getByText('未接通', { exact: true })); assert.equal(requests.find((r) => r.kind === 'wechat').source_id, null);
  });
  await check('Student status history shows connected and rejected outcomes with operator explanation', async () => {
    role = 'student'; await page.reload(); await navigate('信息来源');
    await visible(page.locator('.source-requests .source-request').filter({ hasText: '希望接入的学院官网' }).getByText('已接通', { exact: true }));
    await visible(page.locator('.source-requests .source-request').filter({ hasText: '尚未提供授权的公众号' }).getByText('处理说明：暂未取得可用授权订阅渠道，请补充后重新申请。', { exact: true }));
  });
  assert.deepEqual(unexpected, []); assert.deepEqual(report.errors, []); report.status = 'passed'; report.writes = writes;
} catch (error) { report.status = 'failed'; report.error = error.stack; console.error(error.stack); if (active) await shot(active, 'sources-failure').catch(() => {}); process.exitCode = 1; }
finally { await fs.writeFile(path.join(out, 'source-selection-report.json'), JSON.stringify(report, null, 2)); await browser.close(); }

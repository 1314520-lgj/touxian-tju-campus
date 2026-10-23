/** UI-only regression fixtures, separate from the live-backend browser.e2e.mjs suite. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { loadPlaywright, relativeArtifact } from './playwright.mjs';
const require = createRequire(import.meta.url);
const { chromium } = loadPlaywright();
const base = process.env.CAMPUS_E2E_URL || 'http://127.0.0.1:8000';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
const out = path.resolve(process.env.CAMPUS_E2E_OUTPUT || 'campus-ui-review-output');
await fs.mkdir(out, { recursive: true });
const browser = await chromium.launch();
const report = { fixture_mode: 'Explicit browser route fixtures: UI behavior only; not a backend acceptance claim.', checks: [], pageErrors: [], screenshots: [] };
let active;
async function check(name, operation) { await operation(); report.checks.push(name); console.log('PASS ' + name); }
async function shot(page, name) { await page.evaluate(() => window.scrollTo(0, 0)); await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))); const file = path.join(out, name + '.png'); await page.screenshot({ path: file, fullPage: true }); report.screenshots.push(relativeArtifact(file)); }
async function visible(locator) { await locator.waitFor({ state: 'visible' }); }
const user = { id: '11111111-1111-4111-8111-111111111111', email: 'ui-review@example.org', role: 'student' };
const makeEvent = (n, title, deadline, status = 'active') => ({ id: `20000000-0000-4000-8000-${String(n).padStart(12, '0')}`, version: 1, title, event_type: 'scholarship', status, action_required: true, action_steps: ['核对通知并提交材料'], materials: ['申请表'], entry_urls: [], deadlines: deadline === null ? [{ kind: 'school_deadline', precision: 'unknown', raw_text: '下周五' }] : [{ kind: 'school_deadline', precision: 'date', date: deadline, at: null, raw_text: deadline }], audience: { education_levels: [], college_ids: [], campus_ids: [], enrollment_years: [] }, evidence: [], updated_at: '2026-10-08T08:00:00Z', change_kind: 'new', changed_fields: [] });
const events = [makeEvent(1, '当前合成申请', '2099-12-30'), makeEvent(2, '待确认日期的合成申请', null), makeEvent(3, '已截止合成申请', '2020-01-01'), makeEvent(4, '已取消合成活动', '2099-12-30', 'cancelled'), makeEvent(5, '后页合成申请', '2099-12-31')];
const states = new Map();
const makeItem = (n, title, date) => ({ id: `30000000-0000-4000-8000-${String(n).padStart(12, '0')}`, user_id: user.id, revision: 1, state: 'todo', draft: { title, action_steps: ['提交材料'], materials: [], entry_urls: [], deadline_at: null, deadline_date: date, timezone: 'Asia/Shanghai', evidence_excerpt: null, excerpt_sync_consent: false }, created_at: '2026-10-08T08:00:00Z', updated_at: '2026-10-08T08:00:00Z' });
const items = [makeItem(1, '当前个人合成待办', '2099-12-31'), makeItem(2, '过期个人合成待办', '2020-01-01'), makeItem(3, '后页个人合成待办', null)];
try {
  const probe = await browser.newContext(); const health = await probe.request.get(base + '/api/v1/health/ready'); assert.equal(health.status(), 200); assert.equal((await health.json()).demo_data, true); await probe.close();
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' });
  let loggedIn = true;
  await context.route(base + '/api/v1/**', async (route) => {
    const req = route.request(), url = new URL(req.url()), pathname = url.pathname.slice('/api/v1'.length);
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (pathname === '/me') return json(loggedIn ? { user, csrf_token: 'ui-review-only' } : { code: 'UNAUTHENTICATED', message: '需要登录' }, loggedIn ? 200 : 401);
    if (pathname === '/auth/session' && req.method() === 'DELETE') { loggedIn = false; return route.fulfill({ status: 204 }); }
    if (pathname === '/me/feed') { const first = !url.searchParams.has('cursor'); return json({ items: (first ? events.slice(0, 4) : events.slice(4)).map((event) => ({ event, state: states.get(event.id) || null, classification: event.deadlines[0].precision === 'unknown' ? 'needs_review' : 'required', match: 'matched', match_reasons: [], source_stale: false, deadline_label: null })), next_cursor: first ? 'second' : null }); }
    if (pathname === '/me/items') { const first = !url.searchParams.has('cursor'); return json({ items: first ? items.slice(0, 2) : items.slice(2), next_cursor: first && items.length > 2 ? 'second' : null }); }
    const itemMatch = pathname.match(/^\/me\/items\/([^/]+)$/);
    if (itemMatch) { const index = items.findIndex((item) => item.id === itemMatch[1]); assert.ok(index >= 0); if (req.method() === 'DELETE') { items.splice(index, 1); return route.fulfill({ status: 204 }); } if (req.method() === 'PATCH') { const body = req.postDataJSON(); assert.equal(body.expected_revision, items[index].revision); items[index] = { ...items[index], ...body, revision: items[index].revision + 1 }; return json(items[index]); } }
    const stateMatch = pathname.match(/^\/me\/events\/([^/]+)\/state$/);
    if (stateMatch) { if (req.method() === 'GET') return json({ state: states.get(stateMatch[1]) || null }); const body = req.postDataJSON(), before = states.get(stateMatch[1]); assert.equal(body.expected_state_revision, before?.state_revision || 0); const next = { user_id: user.id, event_id: stateMatch[1], state: 'todo', saved: false, read: false, needs_attention: false, ...before, ...body, state_revision: (before?.state_revision || 0) + 1 }; states.set(stateMatch[1], next); return json(next); }
    const eventMatch = pathname.match(/^\/events\/([^/]+)(\/versions)?$/);
    if (eventMatch) { const event = events.find((event) => event.id === eventMatch[1]); if (event) return json(eventMatch[2] ? { items: [event], next_cursor: null } : event); }
    return route.continue();
  });
  const page = active = await context.newPage(); page.on('pageerror', (error) => report.pageErrors.push(error.message));
  await page.goto(base + '/campus.html'); await visible(page.getByRole('heading', { name: '把今天留给真正重要的事' }));
  await check('Today keeps future and uncertain deadlines visible while expired/cancelled remain expandable', async () => {
    await visible(page.getByRole('button', { name: '当前合成申请', exact: true }));
    await page.getByRole('button', { name: '全部相关信息', exact: true }).click();
    await visible(page.getByRole('button', { name: '待确认日期的合成申请', exact: true }));
    assert.equal(await page.getByRole('button', { name: '已截止合成申请', exact: true }).isVisible(), false);
    assert.equal(await page.getByRole('button', { name: '已取消合成活动', exact: true }).isVisible(), false);
    assert.equal(await page.getByRole('heading', { name: '过期个人合成待办', exact: true }).isVisible(), false);
    await page.locator('summary').filter({ hasText: '已截止或取消' }).click();
    await visible(page.getByRole('button', { name: '已截止合成申请', exact: true }));
    await page.locator('summary').filter({ hasText: '已过期个人待办' }).click();
    await visible(page.getByRole('heading', { name: '过期个人合成待办', exact: true }));
    await shot(page, 'review-desktop-history');
    await page.locator('summary').filter({ hasText: '已截止或取消' }).click();
    await page.locator('summary').filter({ hasText: '已过期个人待办' }).click();
  });
  await check('Actions on later pages preserve loaded results and show truthful save feedback', async () => {
    await page.getByRole('button', { name: '加载更多', exact: true }).first().click();
    await visible(page.getByRole('button', { name: '后页合成申请', exact: true }));
    await page.getByRole('button', { name: '加载更多', exact: true }).click();
    await visible(page.getByRole('heading', { name: '后页个人合成待办', exact: true }));
    const card = page.locator('.event-card').filter({ hasText: '后页合成申请' });
    await card.getByRole('button', { name: '收藏', exact: true }).click();
    await visible(page.locator('.toast').filter({ hasText: '已收藏' }));
    await visible(card.getByRole('button', { name: '已收藏', exact: true }));
    await visible(page.getByRole('heading', { name: '后页个人合成待办', exact: true }));
  });
  await check('Private deletion has explicit feedback and removes only the selected item', async () => {
    const card = page.locator('.personal-card').filter({ hasText: '后页个人合成待办' });
    await card.getByRole('button', { name: '删除', exact: true }).click();
    await card.getByRole('button', { name: '确认删除', exact: true }).click();
    await visible(page.locator('.toast').filter({ hasText: '个人事项及后续提醒已删除' }));
    await page.getByRole('heading', { name: '后页个人合成待办', exact: true }).waitFor({ state: 'detached' });
    await visible(page.getByRole('heading', { name: '当前个人合成待办', exact: true }));
  });
  await check('Private editor preserves opt-in excerpt and clearing consent removes the field before save', async () => {
    const item = items[0]; item.draft.evidence_excerpt = '本人明确选择同步的摘录'; item.draft.excerpt_sync_consent = true;
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await page.locator('.personal-card').filter({ hasText: '当前个人合成待办' }).getByRole('button', { name: '编辑', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '编辑个人事项' }); await visible(dialog);
    assert.equal(await dialog.getByRole('checkbox').isChecked(), true);
    assert.equal(await dialog.getByLabel('需同步的摘录').inputValue(), item.draft.evidence_excerpt);
    await dialog.getByRole('checkbox').uncheck();
    const response = page.waitForResponse((r) => r.url().endsWith('/me/items/' + item.id) && r.request().method() === 'PATCH');
    await dialog.getByRole('button', { name: '确认并保存事项' }).click();
    const result = await response; const sent = result.request().postDataJSON(); assert.equal(sent.draft.evidence_excerpt, null); assert.equal(sent.draft.excerpt_sync_consent, false);
    await dialog.waitFor({ state: 'hidden' });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await check('Mobile menu traps keyboard, closes with Escape and hidden sidebar cannot receive Tab', async () => {
    const button = page.getByRole('button', { name: '打开菜单' }); await button.click(); await visible(page.getByRole('dialog', { name: '导航菜单' }));
    for (let n = 0; n < 14; n++) { await page.keyboard.press('Tab'); assert.equal(await page.evaluate(() => Boolean(document.activeElement.closest('.sidebar.open'))), true); }
    await page.keyboard.press('Escape'); assert.equal(await button.getAttribute('aria-expanded'), 'false'); assert.equal(await button.evaluate((el) => el === document.activeElement), true);
    // Wait for the drawer to actually leave the tab order. The close is a 200ms
    // slide, and asserting the closed-drawer property at t=0 samples a frame in
    // which the drawer is still sliding out; that made this check racy.
    await page.waitForFunction(() => {
      const sidebar = document.querySelector('.sidebar');
      return sidebar.hasAttribute('inert') || getComputedStyle(sidebar).visibility === 'hidden';
    });
    await page.keyboard.press('Shift+Tab'); assert.equal(await page.evaluate(() => Boolean(document.activeElement.closest('.sidebar'))), false);
    // Backwards from the menu button the next stop is the document's first
    // focusable element, the skip link. It is pinned over the header while
    // focused, so release it before clicking the menu button again.
    assert.equal(await page.evaluate(() => document.activeElement.classList.contains('skip-link')), true);
    await page.evaluate(() => document.activeElement.blur());
    await button.click(); await page.getByRole('navigation').getByRole('button', { name: '今天', exact: true }).click(); assert.equal(await button.evaluate((el) => el === document.activeElement), true);
  });
  await check('Mobile edit dialog cycles both directions and restores trigger focus', async () => {
    const trigger = page.getByRole('button', { name: '添加个人事项', exact: true }); await trigger.click(); const dialog = page.getByRole('dialog', { name: '把群通知变成个人待办' }); await visible(dialog);
    for (const key of [...Array(10).fill('Shift+Tab'), ...Array(10).fill('Tab')]) { await page.keyboard.press(key); assert.equal(await page.evaluate(() => Boolean(document.activeElement.closest('.modal'))), true); }
    await page.keyboard.press('Escape'); assert.equal(await trigger.evaluate((el) => el === document.activeElement), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    await shot(page, 'review-mobile-current');
  });
  await check('Logout removes personal content and active success messages', async () => {
    await page.getByRole('button', { name: '打开菜单' }).click(); await page.getByRole('button', { name: '退出登录', exact: true }).click();
    await visible(page.getByRole('button', { name: '登录', exact: true })); assert.equal(await page.locator('.personal-card').count(), 0); assert.equal(await page.locator('.toast').count(), 0);
  });
  assert.deepEqual(report.pageErrors, []); report.status = 'passed';
} catch (error) { report.status = 'failed'; report.error = error.stack; console.error(error.stack); if (active) await shot(active, 'review-failure').catch(() => {}); process.exitCode = 1; }
finally { await fs.writeFile(path.join(out, 'ui-review-report.json'), JSON.stringify(report, null, 2)); await browser.close(); }

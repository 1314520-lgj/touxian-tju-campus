/** Real local UI/API acceptance. Requires a synthetic demo with an explicitly trusted fixture origin. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';

import { loadPlaywright, relativeArtifact } from './playwright.mjs';
const require = createRequire(import.meta.url);
const { chromium } = loadPlaywright();
const base = process.env.CAMPUS_E2E_URL || 'http://127.0.0.1:8015';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname), 'Only loopback synthetic demos may be mutated');
assert.ok(process.env.CAMPUS_E2E_FEED_ORIGIN, 'Set CAMPUS_E2E_FEED_ORIGIN to the already-authorized local fixture HTTP origin');
const feedOrigin = new URL(process.env.CAMPUS_E2E_FEED_ORIGIN);
assert.ok(['127.0.0.1', 'localhost'].includes(feedOrigin.hostname) && feedOrigin.protocol === 'http:', 'Only the explicit loopback feed fixture is supported');
assert.ok(feedOrigin.pathname === '/' && !feedOrigin.username && !feedOrigin.password && !feedOrigin.search && !feedOrigin.hash);
const out = path.resolve(process.env.CAMPUS_E2E_OUTPUT || '.campus-feed-mode-output');
await fs.mkdir(out, { recursive: true });
const run = Date.now() + '-' + crypto.randomUUID().slice(0, 8);
const name = '【合成验收】供稿模式-' + run;
const report = { base, mode: 'Real loopback browser + API; no route interception. Synthetic configuration only, no external fetch, provider authorization or production deployment.', checks: [], pending_checks: [], pageErrors: [], failedResponses: [], screenshots: [] };
const browser = await chromium.launch();
report.browser = browser.version();
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' });
const page = await context.newPage();
page.on('pageerror', (error) => report.pageErrors.push(error.message));
page.on('response', (response) => { if (response.status() >= 500) report.failedResponses.push({ path: new URL(response.url()).pathname, status: response.status() }); });
const visible = (locator) => locator.waitFor({ state: 'visible', timeout: 15000 });
async function check(label, action) { await action(); report.checks.push(label); console.log('PASS ' + label); }
async function json(endpoint) { const response = await context.request.get(base + '/api/v1' + endpoint); assert.equal(response.status(), 200, endpoint); return response.json(); }
async function nav(label) { if (await page.getByRole('button', { name: '打开菜单' }).isVisible()) await page.getByRole('button', { name: '打开菜单' }).click(); await page.getByRole('navigation').getByRole('button', { name: label, exact: true }).click(); }
async function clickHTTP(locator, endpoint, method, status = 200) {
  const pending = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/v1' + endpoint && response.request().method() === method);
  await locator.click(); const response = await pending;
  assert.equal(response.status(), status, method + ' ' + endpoint + ': ' + await response.text());
  return response.json();
}
async function screenshot(label) { const file = path.join(out, label + '.png'); await page.screenshot({ path: file, fullPage: true }); report.screenshots.push(relativeArtifact(file)); }
async function login() {
  await page.getByRole('button', { name: '登录', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '登录偷闲 · 天大版' });
  await dialog.getByLabel('邮箱', { exact: true }).fill(process.env.CAMPUS_E2E_ADMIN || 'demo-admin@tju.edu.cn');
  await clickHTTP(dialog.getByRole('button', { name: '获取验证码' }), '/auth/challenges', 'POST', 201);
  await visible(dialog.locator('.dev-note strong'));
  await dialog.getByLabel('六位验证码').fill((await dialog.locator('.dev-note strong').innerText()).trim());
  await dialog.getByRole('button', { name: '登录', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
}
async function verify(card, ident) {
  await card.getByRole('button', { name: '核验来源', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '核验信息来源' });
  await dialog.getByLabel('核验结果').selectOption('verified_organization');
  await dialog.getByLabel('核验依据').fill('【合成验收】测试供稿身份及完整正文授权记录；不代表真实公众号或网站授权。');
  const result = await clickHTTP(dialog.getByRole('button', { name: '保存核验结果' }), '/admin/sources/' + ident + '/verification', 'POST');
  assert.equal(result.verification, 'verified_organization');
  await dialog.waitFor({ state: 'hidden' });
  await visible(card.getByText(/组织来源/));
}

try {
  assert.equal((await json('/health/ready')).demo_data, true, 'Explicit synthetic demo marker required');
  await page.goto(base + '/campus.html'); await visible(page.locator('.demo-banner')); await login(); await nav('管理工作台');
  let sourceID;
  const card = () => page.locator('.admin-source').filter({ hasText: name });
  await check('WeRSS source defaults to original-page retrieval and creates through the real UI/API', async () => {
    await page.getByRole('button', { name: '接入来源', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '添加信息来源' });
    assert.equal(await dialog.getByLabel('正文获取方式').inputValue(), 'link');
    await dialog.getByLabel('来源名称', { exact: true }).fill(name);
    await dialog.getByLabel('来源类型').selectOption('wechat');
    assert.equal(await dialog.getByLabel('适配器').inputValue(), 'werss');
    await dialog.getByLabel('原始地址', { exact: true }).fill(feedOrigin.origin + '/rss');
    const result = await clickHTTP(dialog.getByRole('button', { name: '保存来源', exact: true }), '/admin/sources', 'POST', 201);
    sourceID = result.id; report.source_id = sourceID;
    assert.equal(result.feed_content_mode, 'link'); assert.equal(result.verification, 'unverified');
    await dialog.waitFor({ state: 'hidden' }); await visible(card().getByText(/正文：原网页/));
  });
  await check('Changing a verified source to authorized full text revokes verification and persists on reload', async () => {
    await verify(card(), sourceID); await card().getByRole('button', { name: '修改来源' }).click();
    const dialog = page.getByRole('dialog', { name: '修改信息来源' });
    await dialog.getByLabel('正文获取方式').selectOption('authorized_fulltext');
    await visible(dialog.getByText(/摘要不能当作全文/));
    const result = await clickHTTP(dialog.getByRole('button', { name: '保存来源', exact: true }), '/admin/sources/' + sourceID, 'PATCH');
    assert.equal(result.feed_content_mode, 'authorized_fulltext'); assert.equal(result.verification, 'unverified');
    await dialog.waitFor({ state: 'hidden' }); await visible(card().getByText(/正文：已授权订阅全文 · 尚未核验/));
    await page.reload(); await nav('管理工作台'); await visible(card().getByText(/正文：已授权订阅全文 · 尚未核验/));
    await card().getByRole('button', { name: '修改来源' }).click();
    const reopened = page.getByRole('dialog', { name: '修改信息来源' });
    assert.equal(await reopened.getByLabel('正文获取方式').inputValue(), 'authorized_fulltext');
    await reopened.getByRole('button', { name: '取消', exact: true }).click();
  });
  await check('Switching back to original-page mode revokes the new verification', async () => {
    await verify(card(), sourceID); await card().getByRole('button', { name: '修改来源' }).click();
    const dialog = page.getByRole('dialog', { name: '修改信息来源' });
    await dialog.getByLabel('正文获取方式').selectOption('link');
    const result = await clickHTTP(dialog.getByRole('button', { name: '保存来源', exact: true }), '/admin/sources/' + sourceID, 'PATCH');
    assert.equal(result.feed_content_mode, 'link'); assert.equal(result.verification, 'unverified');
    await dialog.waitFor({ state: 'hidden' }); await visible(card().getByText(/正文：原网页 · 尚未核验/));
    await screenshot('feed-mode-admin-desktop');
  });
  await check('Website adapter forces original-page mode and the selector fits a mobile dialog', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: '接入来源', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '添加信息来源' });
    await dialog.getByLabel('正文获取方式').selectOption('authorized_fulltext');
    await dialog.getByLabel('来源类型').selectOption('website');
    assert.equal(await dialog.getByLabel('正文获取方式').inputValue(), 'link');
    assert.equal(await dialog.getByLabel('正文获取方式').isDisabled(), true);
    await dialog.getByLabel('来源名称', { exact: true }).fill(name + '-公开网站');
    await dialog.getByLabel('原始地址', { exact: true }).fill(feedOrigin.origin + '/website');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    await screenshot('feed-mode-website-mobile');
    const result = await clickHTTP(dialog.getByRole('button', { name: '保存来源', exact: true }), '/admin/sources', 'POST', 201);
    assert.equal(result.adapter, 'website'); assert.equal(result.feed_content_mode, 'link');
    await dialog.waitFor({ state: 'hidden' });
  });
  if (process.env.CAMPUS_E2E_CAPTURE_EVENT) {
    await check('Published authorized-feed evidence and its snapshot label their acquisition method', async () => {
      await page.setViewportSize({ width: 1440, height: 1000 });
      const event = await json('/events/' + process.env.CAMPUS_E2E_CAPTURE_EVENT);
      assert.ok(event.evidence.some((item) => item.capture?.method === 'authorized_feed'), 'Requires an explicitly synthetic authorized-feed event');
      await nav('今天'); await page.getByRole('button', { name: '全部相关信息', exact: true }).click();
      await page.getByRole('button', { name: event.title, exact: true }).first().click();
      const dialog = page.getByRole('dialog', { name: '事项详情' });
      await dialog.getByRole('button', { name: /原文依据/ }).click();
      const evidence = dialog.locator('.evidence').filter({ hasText: '订阅供稿 · 原网页未直接核验' }).first();
      await visible(evidence); await evidence.getByRole('button', { name: '打开留存快照' }).click();
      await visible(dialog.locator('.panel').getByText('订阅供稿 · 原网页未直接核验', { exact: true }));
      await screenshot('feed-mode-evidence-provenance');
    });
  } else report.pending_checks.push('Capture annotation needs CAMPUS_E2E_CAPTURE_EVENT pointing to a seeded synthetic authorized-feed event; no provenance E2E claim.');
  await check('No browser exceptions or server 5xx occurred', async () => { assert.deepEqual(report.pageErrors, []); assert.deepEqual(report.failedResponses, []); });
  report.status = 'passed';
} catch (error) {
  report.status = 'failed'; report.error = error.stack; console.error(error.stack); process.exitCode = 1;
  await screenshot('feed-mode-failure').catch(() => {});
} finally {
  await fs.writeFile(path.join(out, 'feed-mode-report.json'), JSON.stringify(report, null, 2));
  await browser.close();
}

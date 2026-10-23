/**
 * Automated WCAG acceptance with axe-core, against the labelled loopback demo.
 *
 * The engine is the real axe-core build vendored under `vendor/`, injected into
 * the page and executed there — no route interception, no stubbed results, and
 * no network at run time. A missing or unreadable bundle aborts the run rather
 * than reporting an empty pass, because "zero violations" must never mean
 * "zero checks".
 *
 * Policy: `serious` and `critical` violations fail the run. `moderate` and
 * `minor` ones are recorded in the report and printed, so a regression is
 * visible without pretending every advisory finding is a blocker.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { loadPlaywright, relativeArtifact } from './playwright.mjs';
import { randomUUID } from 'node:crypto';

const require = createRequire(import.meta.url);
const { chromium } = loadPlaywright();

const here = path.dirname(fileURLToPath(import.meta.url));
const axePath = path.resolve(process.env.CAMPUS_AXE_PATH || path.join(here, 'vendor', 'axe.min.js'));
const axeSource = await fs.readFile(axePath, 'utf8').catch(() => null);
assert.ok(axeSource, 'Missing vendored axe-core at ' + axePath + '; an accessibility run must not skip its checks');
const axeVersion = (axeSource.match(/axe v(\d+\.\d+\.\d+)/) || [])[1];
assert.ok(axeVersion, 'Vendored axe-core does not look like a real build');

const base = (process.env.CAMPUS_E2E_URL || 'http://127.0.0.1:8015').replace(/\/$/, '');
const address = new URL(base);
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(address.hostname), 'Accessibility acceptance requires a loopback demo');
assert.ok(['http:', 'https:'].includes(address.protocol) && !address.username && !address.password && !address.search && !address.hash);

const out = path.resolve(process.env.CAMPUS_E2E_OUTPUT || '.campus-a11y-output');
await fs.mkdir(out, { recursive: true });
const report = {
  base, axe: axeVersion, mode: 'Real axe-core in a real browser against the labelled synthetic demo. No intercepted requests.',
  audits: [], screenshots: [], pageErrors: [], failedResponses: [], viewports: [],
};
const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice'];
const BLOCKING = ['serious', 'critical'];
// Rules whose result depends on rendered pixels rather than markup.
const DEFERRED = new Set(['color-contrast', 'color-contrast-enhanced']);

const browser = await chromium.launch({ headless: true });
report.browser = browser.version();
let active;

function track(page) {
  active = page;
  page.on('pageerror', (error) => report.pageErrors.push(error.message));
  page.on('response', (response) => {
    if (response.status() >= 500) report.failedResponses.push({ path: new URL(response.url()).pathname, status: response.status() });
  });
}
async function visible(locator) { await locator.waitFor({ state: 'visible', timeout: 15000 }); }
async function stable(page) {
  await page.waitForFunction(() => !document.querySelector('.loading'), null, { timeout: 15000 });
  // Entrance animations run for 280ms, so two animation frames (~32ms) used to
  // be enough only while there were none. Auditing a half-faded card reads its
  // blended colour and reports a contrast failure that no reader ever sees, so
  // wait for every finite animation to settle first. Infinite animations (the
  // spinner, the skeleton shimmer) never finish and are excluded.
  await page.evaluate(() => Promise.all(
    document.getAnimations()
      .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
      .map((animation) => animation.finished.catch(() => {}))));
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}
async function screenshot(page, name) {
  await stable(page);
  const file = path.join(out, name + '.png');
  await page.screenshot({ path: file, animations: 'disabled' });
  report.screenshots.push(relativeArtifact(file));
}

async function audit(page, label, slug) {
  await stable(page);
  const outcome = await page.evaluate(async (tags) => {
    const simplify = (items) => items.map((item) => ({
      id: item.id, impact: item.impact, help: item.help, count: item.nodes.length,
      nodes: item.nodes.slice(0, 5).map((node) => ({ target: node.target.join(' '), summary: (node.failureSummary || '').slice(0, 300) })),
    }));
    const results = await window.axe.run(document, { runOnly: { type: 'tag', values: tags } });
    return { violations: simplify(results.violations), incomplete: simplify(results.incomplete) };
  }, TAGS);
  const blocking = outcome.violations.filter((item) => BLOCKING.includes(item.impact));
  // Contrast over a gradient cannot be resolved from computed styles, so axe
  // defers it; ui-polish.e2e.mjs measures those areas from rendered pixels.
  // Every ARIA rule, by contrast, is always determinable — an undetermined one
  // means the markup is ambiguous, which is a finding, not a pass.
  const undetermined = outcome.incomplete.filter((item) => item.id.startsWith('aria-') && !DEFERRED.has(item.id));
  report.audits.push({ label, tags: TAGS, ...outcome });
  for (const item of outcome.violations) {
    console.log((BLOCKING.includes(item.impact) ? 'FAIL ' : 'WARN ') + label + ' · ' + item.id +
      ' (' + item.impact + ' × ' + item.count + ')' + (item.nodes[0] ? ' · ' + item.nodes[0].target : ''));
  }
  for (const item of undetermined) {
    console.log('FAIL ' + label + ' · undetermined ' + item.id + ' × ' + item.count +
      (item.nodes[0] ? ' · ' + item.nodes[0].target : ''));
  }
  console.log('PASS ' + label + ' · ' + outcome.violations.length + ' violation(s), ' +
    outcome.incomplete.length + ' deferred to the pixel check');
  await screenshot(page, 'a11y-' + String(report.audits.length).padStart(2, '0') + '-' + slug);
  assert.deepEqual(blocking, [], label + ': serious or critical WCAG violations: ' + JSON.stringify(blocking));
  assert.deepEqual(undetermined, [], label + ': ARIA rules left undetermined: ' + JSON.stringify(undetermined));
  return outcome;
}

async function ready(page) {
  await page.goto(base + '/campus.html');
  await visible(page.locator('.demo-banner'));
  assert.match(await page.locator('.demo-banner').innerText(), /合成演示数据/);
  await visible(page.locator('main h1').first());
  await stable(page);
}
async function nav(page, name) {
  const opener = page.getByRole('button', { name: '打开菜单', exact: true });
  if (await opener.isVisible()) await opener.click();
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name, exact: true }).click();
  await visible(page.locator('main h1').first());
  await stable(page);
}
async function login(page, email) {
  await page.getByRole('button', { name: '登录', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '登录偷闲 · 天大版' });
  await dialog.getByLabel('邮箱', { exact: true }).fill(email);
  const pending = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/v1/auth/challenges' && response.request().method() === 'POST');
  await dialog.getByRole('button', { name: '获取验证码', exact: true }).click();
  const response = await pending;
  assert.equal(response.status(), 201, 'Login challenge: run admin suites serially and allow the existing 30 second cooldown');
  await visible(dialog.locator('.dev-note strong'));
  const code = (await dialog.locator('.dev-note strong').innerText()).trim();
  await dialog.getByLabel('六位验证码').fill(code);
  await dialog.getByRole('button', { name: '登录', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await visible(page.getByRole('button', { name: '打开我的设置', exact: true }));
  await stable(page);
}

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' });
  await context.addInitScript({ content: axeSource });
  const health = await context.request.get(base + '/api/v1/health/ready');
  assert.equal(health.status(), 200);
  assert.equal((await health.json()).demo_data, true, 'Explicit demo_data=true required before any UI audit');

  const page = await context.newPage();
  track(page);
  await ready(page);

  await audit(page, 'today-anonymous', 'public-home');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await visible(page.getByRole('dialog', { name: '登录偷闲 · 天大版' }));
  await audit(page, 'login-dialog', 'login-dialog');
  await page.keyboard.press('Escape');
  assert.equal(await page.getByRole('dialog').count(), 0);

  await login(page, 'student@example.com');
  const pages = [['今天', 'today'], ['机会', 'opportunities'], ['找信息', 'search'], ['我的事项', 'my-items'], ['信息来源', 'sources'], ['我的设置', 'account']];
  for (const [label, slug] of pages) {
    await nav(page, label);
    await audit(page, 'page-' + label, 'page-' + slug);
  }

  // The event detail is where the new change history lives, so it is audited
  // with the change tab actually open rather than only in its default state.
  await nav(page, '今天');
  await page.getByRole('button', { name: '全部相关信息', exact: true }).click();
  await visible(page.locator('.event-card').first());
  await page.locator('.event-card .card-title').first().click();
  const detail = page.getByRole('dialog', { name: '事项详情' });
  await visible(detail);
  await audit(page, 'event-detail', 'event-detail');
  await detail.getByRole('button', { name: /^变化记录/ }).click();
  await audit(page, 'event-detail-changes', 'event-detail-changes');
  await page.keyboard.press('Escape');
  await detail.waitFor({ state: 'hidden' });

  await page.setViewportSize({ width: 390, height: 844 });
  report.viewports.push({ width: 390, height: 844 });
  await nav(page, '今天');
  await audit(page, 'mobile-today', 'mobile-today');
  await page.getByRole('button', { name: '打开菜单', exact: true }).click();
  await visible(page.locator('.sidebar.open'));
  await audit(page, 'mobile-navigation-drawer', 'mobile-navigation-drawer');
  await page.getByRole('button', { name: '收起导航菜单', exact: true }).click();

  const adminContext = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' });
  await adminContext.addInitScript({ content: axeSource });
  const admin = await adminContext.newPage();
  track(admin);
  await ready(admin);
  await login(admin, process.env.CAMPUS_E2E_ADMIN || 'demo-admin@tju.edu.cn');
  await nav(admin, '管理工作台');
  await audit(admin, 'admin-sources', 'admin-sources');
  const adminTabs = [['候选审核', 'reviews'], ['后台任务', 'jobs'], ['接入申请', 'requests'], ['发布与撤回', 'publication']];
  for (const [tab, slug] of adminTabs) {
    await admin.getByRole('button', { name: tab, exact: true }).click();
    await audit(admin, 'admin-' + tab, 'admin-' + slug);
  }
  await adminContext.close();

  assert.deepEqual(report.pageErrors, []);
  assert.deepEqual(report.failedResponses, []);
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = error.stack;
  process.exitCode = 1;
  console.error(error.stack);
  if (active) await screenshot(active, 'a11y-failure').catch(() => {});
} finally {
  await fs.writeFile(path.join(out, 'a11y-report.json'), JSON.stringify(report, null, 2));
  await browser.close();
}

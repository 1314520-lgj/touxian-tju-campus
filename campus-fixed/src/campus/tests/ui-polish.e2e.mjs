/** Visual and keyboard acceptance against the labelled, real loopback demo. No route interception. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { loadPlaywright, relativeArtifact } from './playwright.mjs';
import { randomUUID } from 'node:crypto';

const require = createRequire(import.meta.url);
const { chromium } = loadPlaywright();
const base = (process.env.CAMPUS_E2E_URL || 'http://127.0.0.1:8021').replace(/\/$/, '');
const address = new URL(base);
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(address.hostname), 'UI acceptance requires a loopback demo');
assert.ok(['http:', 'https:'].includes(address.protocol) && !address.username && !address.password && !address.search && !address.hash);
const out = path.resolve(process.env.CAMPUS_E2E_OUTPUT || '.campus-ui-polish-output');
await fs.mkdir(out, { recursive: true });
const run = Date.now() + '-' + randomUUID().slice(0, 8);
const report = {
  base, mode: 'Real browser_demo UI and API. Synthetic notices only; no intercepted requests, invented source data or external collection.',
  checks: [], measurements: [], screenshots: [], pageErrors: [], failedResponses: [],
  viewports: [{ width: 1440, height: 1000 }, { width: 768, height: 1024 }, { width: 390, height: 844 }, { width: 320, height: 740 }],
};
const browser = await chromium.launch({ headless: true });
report.browser = browser.version();
let active;
const visible = (locator) => locator.waitFor({ state: 'visible', timeout: 15000 });
async function check(name, action) {
  await action(); report.checks.push(name); console.log('PASS ' + name);
}
function track(page) {
  active = page;
  page.on('pageerror', (error) => report.pageErrors.push(error.message));
  page.on('response', (response) => {
    if (response.status() >= 500) report.failedResponses.push({ path: new URL(response.url()).pathname, status: response.status() });
  });
}
async function stable(page) {
  await page.waitForFunction(() => !document.querySelector('.loading'), null, { timeout: 15000 });
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}
async function screenshot(page, name) {
  await stable(page);
  const file = path.join(out, name + '.png');
  await page.screenshot({ path: file, fullPage: false, animations: 'disabled' });
  report.screenshots.push(relativeArtifact(file));
}
async function layout(page, label) {
  await stable(page);
  const measured = await page.evaluate(() => ({
    width: innerWidth, documentWidth: document.documentElement.scrollWidth,
    bodyWidth: document.body.scrollWidth,
    offenders: Array.from(document.querySelectorAll('.main-content *, .topbar *')).filter((node) => {
      const box = node.getBoundingClientRect();
      return box.width && box.height && (box.right > innerWidth + 1 || box.left < -1);
    }).slice(0, 8).map((node) => ({ tag: node.tagName, class: node.className, text: node.textContent.trim().slice(0, 50) })),
  }));
  report.measurements.push({ label, layout: measured });
  assert.ok(measured.documentWidth <= measured.width + 1 && measured.bodyWidth <= measured.width + 1,
    label + ': horizontal page overflow: ' + JSON.stringify(measured));
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
async function drawerSettled(page) {
  await page.waitForFunction(() => {
    const sidebar = document.querySelector('.sidebar.open');
    const closer = sidebar?.querySelector('[aria-label="收起导航菜单"]');
    if (!sidebar || !closer) return false;
    const side = sidebar.getBoundingClientRect(), button = closer.getBoundingClientRect();
    return Math.abs(side.left) <= .5 && button.left >= 0 && button.right <= innerWidth + .5;
  }, null, { timeout: 15000 });
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
  assert.match(code, /^\d{6}$/);
  await dialog.getByLabel('六位验证码').fill(code);
  await dialog.getByRole('button', { name: '登录', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await visible(page.getByRole('button', { name: '打开我的设置', exact: true }));
  await stable(page);
}

// Color samples are rendered CSS colors and opaque ancestor backgrounds, not a claim about all WCAG criteria.
async function textContrast(locator, name) {
  const samples = await locator.evaluateAll((nodes) => {
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
    const context = canvas.getContext('2d', { willReadFrequently: true });
    const rgba = (css) => { context.clearRect(0, 0, 1, 1); context.fillStyle = css; context.fillRect(0, 0, 1, 1); return Array.from(context.getImageData(0, 0, 1, 1).data).map((v, i) => i === 3 ? v / 255 : v); };
    const blend = (fore, back) => fore.slice(0, 3).map((v, i) => v * fore[3] + back[i] * (1 - fore[3]));
    const luminance = (rgb) => rgb.map((v) => v / 255).map((v) => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4).reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
    return nodes.filter((node) => node.getClientRects().length && node.textContent.trim()).map((node) => {
      const parents = []; for (let parent = node; parent; parent = parent.parentElement) parents.push(parent);
      let background = [255, 255, 255]; const images = [];
      for (const parent of parents.reverse()) {
        const style = getComputedStyle(parent), fill = rgba(style.backgroundColor);
        background = blend(fill, background);
        if (fill[3] === 1) images.length = 0; // An opaque card hides decorative gradients farther behind it.
        if (style.backgroundImage !== 'none') images.push(style.backgroundImage);
      }
      const style = getComputedStyle(node), foreground = blend(rgba(style.color), background);
      const values = [luminance(foreground), luminance(background)].sort((a, b) => a - b);
      return { text: node.textContent.trim().slice(0, 80), color: style.color, background, ratio: (values[1] + .05) / (values[0] + .05), fontSize: style.fontSize, images };
    });
  });
  assert.ok(samples.length, name + ': no visible actual content to measure');
  report.measurements.push({ label: name, contrast: samples });
  for (const sample of samples) {
    assert.deepEqual(sample.images, [], name + ': gradient/image backgrounds need a pixel-based contrast check');
    assert.ok(sample.ratio >= 4.5, name + ': text contrast ' + sample.ratio.toFixed(2) + ': ' + sample.text);
  }
}

try {
  const context = await browser.newContext({ viewport: report.viewports[0], locale: 'zh-CN' });
  const health = await context.request.get(base + '/api/v1/health/ready');
  assert.equal(health.status(), 200); assert.equal((await health.json()).demo_data, true, 'Explicit demo_data=true required before any UI login');
  const page = await context.newPage(); track(page); await ready(page);
  for (const viewport of report.viewports) {
    await page.setViewportSize(viewport);
    await check(viewport.width + 'px public home has real demo notices and no horizontal overflow', async () => {
      await nav(page, '今天');
      await visible(page.locator('.event-card').first());
      await layout(page, viewport.width + '-home');
      await screenshot(page, 'ui-' + viewport.width + '-home');
    });
    await check(viewport.width + 'px source directory fits the viewport', async () => {
      await nav(page, '信息来源');
      await visible(page.locator('.source-row').first());
      await layout(page, viewport.width + '-sources');
      await screenshot(page, 'ui-' + viewport.width + '-sources');
    });
    await check(viewport.width + 'px real notice search fits the viewport', async () => {
      await nav(page, '找信息');
      await page.getByLabel('搜索关键词').fill('奖学金');
      await page.getByRole('button', { name: '搜索', exact: true }).click();
      await visible(page.locator('.search-result').first());
      assert.match(await page.locator('.search-result').first().innerText(), /奖学金/);
      await layout(page, viewport.width + '-search');
      await screenshot(page, 'ui-' + viewport.width + '-search');
    });
  }
  await check('mobile navigation has a visible close control and Escape restores keyboard focus', async () => {
    const opener = page.getByRole('button', { name: '打开菜单', exact: true });
    await opener.focus(); await page.keyboard.press('Enter');
    const closer = page.getByRole('button', { name: '收起导航菜单', exact: true });
    await visible(closer); await drawerSettled(page);
    const closeBox = await closer.boundingBox(); assert.ok(closeBox && closeBox.x >= 0 && closeBox.x + closeBox.width <= 321);
    await screenshot(page, 'ui-320-navigation');
    await page.keyboard.press('Escape');
    assert.equal(await opener.getAttribute('aria-expanded'), 'false');
    assert.equal(await opener.evaluate((node) => document.activeElement === node), true, 'Escape should restore focus to the menu opener');
    const focus = await opener.evaluate((node) => {
      const style = getComputedStyle(node);
      return { visible: node.matches(':focus-visible'), outline: style.outlineStyle, width: parseFloat(style.outlineWidth), color: style.outlineColor, shadow: style.boxShadow };
    });
    report.measurements.push({ label: 'keyboard-menu-focus', focus });
    assert.equal(focus.visible, true); assert.ok((focus.outline !== 'none' && focus.width >= 2) || focus.shadow !== 'none', 'Keyboard focus indicator must remain visible');
    await page.keyboard.press('Enter'); await closer.click();
    assert.equal(await opener.getAttribute('aria-expanded'), 'false', 'Visible close control closes the menu');
  });
  await check('short-screen navigation scrolls to its bottom and desktop resizing releases the page lock', async () => {
    await page.setViewportSize({ width: 320, height: 400 });
    await page.getByRole('button', { name: '打开菜单', exact: true }).click(); await drawerSettled(page);
    const sidebar = page.locator('.sidebar');
    assert.equal(await sidebar.evaluate((node) => node.scrollHeight > node.clientHeight + 1 && ['auto', 'scroll'].includes(getComputedStyle(node).overflowY)), true,
      'Navigation must scroll internally when the screen is short');
    const bottom = sidebar.getByRole('button', { name: '邮箱登录', exact: true });
    await bottom.scrollIntoViewIfNeeded();
    const measured = await bottom.evaluate((node) => {
      const box = node.getBoundingClientRect();
      return { top: box.top, bottom: box.bottom, viewport: innerHeight, scrollTop: node.closest('.sidebar').scrollTop };
    });
    report.measurements.push({ label: '320-short-navigation', navigation: measured });
    assert.ok(measured.scrollTop > 0 && measured.top >= 0 && measured.bottom <= measured.viewport + 1,
      'Bottom navigation actions must remain reachable');
    await screenshot(page, 'ui-320-short-navigation');
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.waitForFunction(() => !document.querySelector('.sidebar.open') && document.body.style.overflow !== 'hidden');
    assert.equal(await sidebar.getAttribute('aria-modal'), null, 'Desktop navigation must not retain modal state');
    await layout(page, '1024-navigation-after-resize');
    await page.setViewportSize({ width: 320, height: 740 });
  });
  await check('resizing with a login modal above the drawer keeps its lock and releases it after the final close', async () => {
    await page.getByRole('button', { name: '打开菜单', exact: true }).click(); await drawerSettled(page);
    await page.locator('.sidebar').getByRole('button', { name: '邮箱登录', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '登录偷闲 · 天大版' }); await visible(dialog);
    assert.equal(await page.evaluate(() => document.body.style.overflow), 'hidden');
    await page.setViewportSize({ width: 900, height: 768 });
    await page.waitForFunction(() => !document.querySelector('.sidebar.open'));
    assert.equal(await page.evaluate(() => document.body.style.overflow), 'hidden', 'An open modal still owns the body scroll lock after the drawer closes');
    await page.keyboard.press('Escape'); await dialog.waitFor({ state: 'hidden' });
    await page.waitForFunction(() => document.body.style.overflow !== 'hidden');
    report.measurements.push({ label: 'drawer-modal-resize-cleanup', bodyOverflowAfterClose: await page.evaluate(() => document.body.style.overflow) });
    await page.setViewportSize({ width: 320, height: 740 });
  });
  await check('unique student login and local action form remain usable on a 320px screen', async () => {
    report.studentEmail = 'ui-polish-' + run + '@example.org';
    await login(page, report.studentEmail);
    await nav(page, '我的事项'); await page.getByRole('button', { name: '添加事项', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '把群通知变成个人待办' });
    await dialog.getByRole('button', { name: '手动填写', exact: true }).click();
    await visible(dialog.getByRole('heading', { name: '确认要保存的事项' }));
    const body = dialog.locator('.modal-body');
    assert.equal(await body.evaluate((node) => node.scrollHeight > node.clientHeight + 1), true, 'Actual long personal form should scroll inside its modal');
    await dialog.getByRole('button', { name: '确认并保存事项', exact: true }).scrollIntoViewIfNeeded();
    assert.ok(await body.evaluate((node) => node.scrollTop > 0));
    await layout(page, '320-personal-modal'); await screenshot(page, 'ui-320-personal-form');
    await page.keyboard.press('Escape'); assert.equal(await page.getByRole('dialog').count(), 0);
  });
  await check('actual deadline and action text meet 4.5:1 contrast', async () => {
    await page.setViewportSize({ width: 1440, height: 1000 }); await nav(page, '今天');
    await page.getByRole('button', { name: '全部相关信息', exact: true }).click();
    await visible(page.locator('.event-card').first()); await stable(page);
    await textContrast(page.locator('.event-card .deadline'), 'deadline-text');
    await textContrast(page.locator('.event-card .action-preview'), 'action-text');
  });
  await check('reduced-motion preference removes sidebar and control transitions', async () => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    const measured = await page.evaluate(() => ({
      matches: matchMedia('(prefers-reduced-motion: reduce)').matches,
      animated: Array.from(document.querySelectorAll('.sidebar, .event-card, .card-title, .primary, .topbar')).map((node) => {
        const style = getComputedStyle(node); return { class: node.className, transitions: style.transitionDuration, animations: style.animationDuration };
      }),
    }));
    report.measurements.push({ label: 'reduced-motion', motion: measured }); assert.equal(measured.matches, true);
    for (const sample of measured.animated) for (const value of [sample.transitions, sample.animations]) {
      assert.ok(value.split(',').every((duration) => parseFloat(duration) === 0), 'Reduced motion must suppress animation duration: ' + JSON.stringify(sample));
    }
  });
  await check('long source editor scrolls internally with reachable actions on a short mobile viewport', async () => {
    const adminContext = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' });
    const admin = await adminContext.newPage(); track(admin); await ready(admin);
    await login(admin, process.env.CAMPUS_E2E_ADMIN || 'demo-admin@tju.edu.cn');
    await nav(admin, '管理工作台'); await admin.setViewportSize({ width: 320, height: 568 });
    await admin.getByRole('button', { name: '接入来源', exact: true }).click();
    const dialog = admin.getByRole('dialog', { name: '添加信息来源' }), body = dialog.locator('.modal-body');
    await visible(dialog);
    assert.equal(await body.evaluate((node) => node.scrollHeight > node.clientHeight + 1 && ['auto', 'scroll'].includes(getComputedStyle(node).overflowY)), true);
    const cancel = dialog.getByRole('button', { name: '取消', exact: true }); await cancel.scrollIntoViewIfNeeded();
    const measured = await dialog.evaluate((node) => { const box = node.getBoundingClientRect(); return { top: box.top, bottom: box.bottom, height: box.height, viewport: innerHeight, scrollTop: node.querySelector('.modal-body').scrollTop }; });
    report.measurements.push({ label: '320-short-source-modal', modal: measured });
    assert.ok(measured.top >= -1 && measured.bottom <= measured.viewport + 1, 'Modal must fit the actual viewport');
    assert.ok(measured.scrollTop > 0, 'Form actions must be reachable by scrolling modal content');
    await layout(admin, '320-short-source-modal'); await screenshot(admin, 'ui-320-source-editor');
    await cancel.click(); await dialog.waitFor({ state: 'hidden' });
  });
  await check('no uncaught browser errors or server 5xx', async () => { assert.deepEqual(report.pageErrors, []); assert.deepEqual(report.failedResponses, []); });
  report.status = 'passed';
} catch (error) {
  report.status = 'failed'; report.error = error.stack; process.exitCode = 1; console.error(error.stack);
  if (active) await screenshot(active, 'ui-failure').catch(() => {});
} finally {
  await fs.writeFile(path.join(out, 'ui-polish-report.json'), JSON.stringify(report, null, 2));
  await browser.close();
}

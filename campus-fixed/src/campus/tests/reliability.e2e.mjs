/** Real-browser reliability proof against a fresh, explicitly labelled fixture.
 * No request interception; only the isolated fixture's admin-protected controls
 * can publish a fourth synthetic version or reconcile an accepted candidate.
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
const axePath = path.resolve(process.env.CAMPUS_AXE_PATH || path.join(path.dirname(fileURLToPath(import.meta.url)), 'vendor', 'axe.min.js'));
const axeSource = await fs.readFile(axePath, 'utf8');
const axeVersion = (axeSource.match(/axe v(\d+\.\d+\.\d+)/) || [])[1];
assert.ok(axeVersion, 'A real vendored axe-core build is required');
assert.ok(process.env.CAMPUS_E2E_FIXTURE, 'CAMPUS_E2E_FIXTURE must name a fresh reliability manifest');
const fixture = JSON.parse(await fs.readFile(process.env.CAMPUS_E2E_FIXTURE, 'utf8'));
assert.equal(fixture.synthetic, true);
assert.equal(fixture.fixture, 'reliability-v1');
const base = (process.env.CAMPUS_E2E_URL || fixture.base).replace(/\/$/, '');
assert.equal(base, fixture.base, 'Manifest must belong to the exact server being tested');
const origin = new URL(base);
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname));
assert.ok(['http:', 'https:'].includes(origin.protocol) && !origin.username && !origin.password);
assert.equal(origin.pathname, '/');
assert.ok(!origin.search && !origin.hash);
for (const key of ['attachment_event_id', 'attachment_snapshot_id', 'change_event_id']) {
  assert.match(fixture[key], /^[0-9a-f-]{36}$/);
}
for (const key of ['advance_path', 'drain_path']) {
  assert.match(fixture[key], /^\/__fixture\/[a-z-]+$/);
}

const out = path.resolve(process.env.CAMPUS_E2E_OUTPUT || '.campus-reliability-output');
await fs.mkdir(out, { recursive: true });
const report = {
  base, fixture: fixture.fixture, mode: 'Real browser and API, synthetic fixture, no intercepted requests',
  checks: [], screenshots: [], pageErrors: [], failedResponses: [], failedRequests: [],
  boundaries: ['No real school data, SMTP delivery, public-account authorization or external push is tested'],
};
const a11yReport = {
  base, fixture: fixture.fixture, axe: axeVersion,
  mode: 'Offline vendored axe-core in a real browser, no intercepted requests',
  tags: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice'], scenarios: [],
  boundaries: ['Automated checks do not establish complete accessibility; incomplete results require separate review'],
  status: 'incomplete',
};
const browser = await chromium.launch({ headless: true });
report.browser = browser.version();
let active;

function track(page) {
  active = page;
  page.on('pageerror', error => report.pageErrors.push(error.message));
  page.on('response', response => {
    if (response.status() >= 500) report.failedResponses.push({ path: new URL(response.url()).pathname, status: response.status() });
  });
  page.on('requestfailed', request => {
    if (!request.failure()?.errorText.includes('ERR_ABORTED')) {
      report.failedRequests.push({ path: new URL(request.url()).pathname, reason: request.failure()?.errorText });
    }
  });
}
async function visible(locator) { await locator.waitFor({ state: 'visible', timeout: 15000 }); }
async function stable(page) {
  await page.waitForFunction(() => !document.querySelector('.loading'), null, { timeout: 15000 });
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}
async function check(name, operation) {
  await operation();
  report.checks.push(name);
  console.log('PASS ' + name);
}
async function screenshot(page, name) {
  await stable(page);
  const file = path.join(out, name + '.png');
  await page.screenshot({ path: file, fullPage: true, animations: 'disabled' });
  report.screenshots.push(relativeArtifact(file));
}
async function audit(page, label, slug) {
  await stable(page);
  const outcome = await page.evaluate(async ({ tags, version }) => {
    if (window.axe?.version !== version) throw new Error('Expected offline axe-core is not loaded');
    const simplify = items => items.map(item => ({
      id: item.id, impact: item.impact, help: item.help, count: item.nodes.length,
      nodes: item.nodes.slice(0, 5).map(node => ({ target: node.target, summary: node.failureSummary || '' })),
    }));
    const result = await window.axe.run(document, { runOnly: { type: 'tag', values: tags } });
    return { violations: simplify(result.violations), incomplete: simplify(result.incomplete), passed_rules: result.passes.length };
  }, { tags: a11yReport.tags, version: axeVersion });
  assert.ok(outcome.passed_rules > 0, 'Accessibility scan must execute checks');
  const file = path.join(out, 'a11y-' + slug + '.png');
  await page.screenshot({ path: file, animations: 'disabled' });
  a11yReport.scenarios.push({ label, screenshot: file, ...outcome });
  console.log('AXE ' + label + ' · ' + outcome.violations.length + ' violation(s), ' + outcome.incomplete.length + ' incomplete');
}
async function json(context, pathname) {
  const response = await context.request.get(base + pathname);
  assert.equal(response.status(), 200, pathname + ' status');
  return response.json();
}
async function ready(page) {
  await page.goto(base + '/campus.html');
  await visible(page.locator('.demo-banner'));
  assert.match(await page.locator('.demo-banner').innerText(), /合成演示数据/);
  await visible(page.locator('main h1').first());
  await stable(page);
}
async function nav(page, name) {
  const menu = page.getByRole('button', { name: '打开菜单', exact: true });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name, exact: true }).click();
  await stable(page);
}
async function login(page, email) {
  await page.getByRole('button', { name: '登录', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '登录偷闲 · 天大版' });
  await dialog.getByLabel('邮箱', { exact: true }).fill(email);
  const pending = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/auth/challenges'
    && response.request().method() === 'POST');
  await dialog.getByRole('button', { name: '获取验证码', exact: true }).click();
  assert.equal((await pending).status(), 201, 'Use a fresh fixture to avoid login cooldown');
  await visible(dialog.locator('.dev-note strong'));
  await dialog.getByLabel('六位验证码').fill((await dialog.locator('.dev-note strong').innerText()).trim());
  await dialog.getByRole('button', { name: '登录', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await visible(page.getByRole('button', { name: '打开我的设置', exact: true }));
  await stable(page);
}
async function openEvent(page, event) {
  await nav(page, '今天');
  const all = page.getByRole('button', { name: '全部相关信息', exact: true });
  if (await all.isVisible()) { await all.click(); await stable(page); }
  const card = page.locator('.event-card').filter({ has: page.getByRole('button', { name: event.title, exact: true }) });
  await card.waitFor({ state: 'attached', timeout: 15000 });
  if (!await card.isVisible()) await page.locator('details.past-items').filter({ has: card }).locator('summary').click();
  await card.getByRole('button', { name: event.title, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '事项详情' });
  await visible(dialog);
  await visible(dialog.getByRole('heading', { name: event.title, exact: true }));
  await stable(page);
  return dialog;
}
async function control(context, session, pathname) {
  const response = await context.request.post(base + pathname, {
    headers: { Origin: base, 'X-CSRF-Token': session.csrf_token }, data: {},
  });
  assert.equal(response.status(), 200, 'Synthetic control status');
  const body = await response.json();
  assert.equal(body.synthetic, true);
  return body;
}
async function waitVersion(page, version) {
  await page.waitForFunction(version => {
    const detail = document.querySelector('.modal .detail-title');
    return detail && detail.textContent.includes('版本 ' + version + ' ·');
  }, version, { timeout: 15000 });
  await stable(page);
}

try {
  const student = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' });
  await student.addInitScript({ content: axeSource });
  const studentPage = await student.newPage();
  track(studentPage);
  assert.equal((await json(student, '/api/v1/health/ready')).demo_data, true);
  const attachmentBefore = await json(student, '/api/v1/events/' + fixture.attachment_event_id);
  const historyBefore = await json(student, '/api/v1/events/' + fixture.change_event_id);
  assert.match(attachmentBefore.title, /演示|合成/);
  assert.match(historyBefore.title, /演示|合成/);
  assert.equal(historyBefore.version, 3, 'Fixture must be fresh with three approved history versions');
  const changesBefore = await json(student, '/api/v1/events/' + fixture.change_event_id + '/changes');
  assert.equal(changesBefore.version, 3);
  assert.equal(changesBefore.changes.length, 2);
  await ready(studentPage);

  await check('public detail warns about the failed restricted attachment', async () => {
    const quality = await json(student, '/api/v1/events/' + fixture.attachment_event_id + '/quality');
    assert.equal(quality.complete, false);
    const failed = quality.documents.find(row => row.snapshot_id === fixture.attachment_snapshot_id);
    assert.equal(failed?.is_attachment, true);
    assert.equal(failed.state, 'failed');
    assert.ok(failed.warnings.includes('attachment_access_restricted'));
    const detail = await openEvent(studentPage, attachmentBefore);
    await visible(detail.getByText('材料可能不完整，请核对原文', { exact: true }));
    assert.match(await detail.locator('.document-quality').innerText(), /附件.*验证码或授权/s);
    assert.equal(await detail.getByRole('button', { name: '授权补充附件', exact: true }).count(), 0);
    await screenshot(studentPage, '01-public-restricted-attachment');
    await studentPage.keyboard.press('Escape');
  });

  await check('unconfigured student push is disabled and status belongs to the session', async () => {
    assert.equal((await student.request.get(base + '/api/v1/me/push-status')).status(), 401);
    await login(studentPage, 'reliability-' + randomUUID().slice(0, 8) + '@example.com');
    const status = await json(student, '/api/v1/me/push-status');
    assert.deepEqual(status, { available: false, reason: 'unconfigured' });
    const other = fixture.configured_user_id || randomUUID();
    assert.deepEqual(await json(student, '/api/v1/me/push-status?user_id=' + other), status);
    await nav(studentPage, '我的设置');
    await studentPage.getByRole('button', { name: '提醒与日历', exact: true }).click();
    const push = studentPage.getByRole('checkbox', { name: '推送通知', exact: true });
    await visible(push);
    await stable(studentPage);
    assert.equal(await push.isDisabled(), true);
    assert.equal(await push.isChecked(), false);
    assert.match(await studentPage.locator('main').innerText(), /个人推送尚未就绪/);
    await audit(studentPage, 'unconfigured student push', 'push-unconfigured');
    await screenshot(studentPage, '02-student-push-unconfigured');
  });

  const admin = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' });
  await admin.addInitScript({ content: axeSource });
  const adminPage = await admin.newPage();
  track(adminPage);
  await ready(adminPage);
  await login(adminPage, fixture.admin_email);
  const adminSession = await json(admin, '/api/v1/me');
  assert.equal(adminSession.user.role, 'admin');

  await check('three approved versions show both changes and a refresh follows version four', async () => {
    const detail = await openEvent(studentPage, historyBefore);
    await detail.getByRole('button', { name: /^变化记录/ }).click();
    await detail.locator('.version-row').nth(1).waitFor({ state: 'visible' });
    assert.deepEqual(await detail.locator('.version-row > strong').allTextContents(),
      changesBefore.changes.map(row => row.change.summary));
    await screenshot(studentPage, '03-three-version-history');
    const advanced = await control(admin, adminSession, fixture.advance_path);
    assert.equal(advanced.event_id, fixture.change_event_id);
    assert.equal(advanced.version, 4);
    await detail.getByRole('button', { name: '标记已读', exact: true }).click();
    await waitVersion(studentPage, 4);
    const current = await json(student, '/api/v1/events/' + fixture.change_event_id + '/changes');
    assert.equal(current.version, 4);
    assert.equal(current.changes.length, 3);
    await detail.locator('.version-row').nth(2).waitFor({ state: 'visible' });
    assert.deepEqual(await detail.locator('.version-row > strong').allTextContents(),
      current.changes.map(row => row.change.summary));
    const latest = current.changes.at(-1).change.summary;
    assert.notEqual(latest, changesBefore.changes.at(-1).change.summary);
    assert.ok((await detail.locator('.change-banner').innerText()).includes(latest));
    await screenshot(studentPage, '04-current-version-history-refresh');
    await studentPage.keyboard.press('Escape');
  });

  await check('nested attachment dialog isolates lower layers and Escape restores parent focus', async () => {
    const detail = await openEvent(adminPage, attachmentBefore);
    const trigger = detail.getByRole('button', { name: '授权补充附件', exact: true });
    await trigger.click();
    const child = adminPage.getByRole('dialog', { name: '授权补充附件', exact: true });
    await visible(child);
    const parentLayer = adminPage.locator('.modal-backdrop').filter({
      has: adminPage.locator('.modal[aria-label="事项详情"]'),
    });
    assert.equal(await parentLayer.evaluate(element => element.inert), true);
    assert.equal(await parentLayer.getAttribute('aria-hidden'), 'true');
    assert.equal(await adminPage.locator('.app-shell').evaluate(element => element.inert), true);
    assert.equal(await child.evaluate(element => element.closest('.modal-backdrop').inert), false);
    assert.equal(await child.evaluate(element => element.contains(document.activeElement)), true);
    await adminPage.keyboard.press('Tab');
    assert.equal(await child.evaluate(element => element.contains(document.activeElement)), true);
    await adminPage.keyboard.press('Escape');
    await child.waitFor({ state: 'hidden' });
    await visible(detail);
    assert.equal(await parentLayer.evaluate(element => element.inert), false);
    assert.equal(await parentLayer.getAttribute('aria-hidden'), null);
    assert.equal(await adminPage.locator('.app-shell').evaluate(element => element.inert), true);
    assert.equal(await trigger.evaluate(element => document.activeElement === element), true);
    await adminPage.keyboard.press('Escape');
    await detail.waitFor({ state: 'hidden' });
    assert.equal(await adminPage.locator('.app-shell').evaluate(element => element.inert), false);
    report.nested_dialog = { background_isolated: true, parent_isolated: true, keyboard_stays_in_child: true,
      escape_restores_parent: true, focus_restored_to_trigger: true, closing_parent_restores_background: true };
  });

  await check('authorized upload remains pending and public evidence stays unchanged', async () => {
    const detail = await openEvent(adminPage, attachmentBefore);
    await detail.getByRole('button', { name: '授权补充附件', exact: true }).click();
    const dialog = adminPage.getByRole('dialog', { name: '授权补充附件', exact: true });
    await visible(dialog);
    await dialog.getByLabel('已获授权的附件', { exact: true }).setInputFiles({
      name: 'authorized-reliability.txt', mimeType: 'text/plain',
      buffer: Buffer.from('全体本科生须提交授权补充证明和完整申请表。此文件仅用于合成验收。', 'utf8'),
    });
    await dialog.getByLabel('授权提供依据', { exact: true }).fill('仅合成文件，由测试运营人员主动提供；不代表任何真实授权。');
    await audit(adminPage, 'authorized attachment replacement form', 'attachment-replacement');
    const uploaded = adminPage.waitForResponse(response => new URL(response.url()).pathname.endsWith('/replacement')
      && response.request().method() === 'POST');
    await dialog.getByRole('button', { name: '保存并进入审核', exact: true }).click();
    const response = await uploaded;
    assert.equal(response.status(), 201);
    report.replacement = await response.json();
    await visible(dialog.getByText('文件已留存，等待审核。公开依据将标注为“授权人工补充”。', { exact: true }));
    assert.deepEqual(await json(student, '/api/v1/events/' + fixture.attachment_event_id), attachmentBefore);
    const quality = await json(student, '/api/v1/events/' + fixture.attachment_event_id + '/quality');
    assert.ok(quality.documents.some(row => row.snapshot_id === fixture.attachment_snapshot_id));
    assert.ok(!quality.documents.some(row => row.snapshot_id === report.replacement.snapshot_id));
    const pending = (await json(admin, '/api/v1/admin/reviews?limit=100')).items
      .find(row => row.candidate_id === report.replacement.candidate_id);
    assert.equal(pending?.review_status, 'pending');
    assert.equal(pending.revision, report.replacement.revision);
    assert.ok(pending.warnings.includes('operator_upload_requires_review'));
    assert.ok(pending.snapshot_ids.includes(report.replacement.snapshot_id));
    await screenshot(adminPage, '05-upload-pending-review');
    await dialog.getByRole('button', { name: '返回管理工作台', exact: true }).click();
  });

  await check('operator review publishes the attachment with visible authorization provenance', async () => {
    await nav(adminPage, '管理工作台');
    await adminPage.getByRole('button', { name: '候选审核', exact: true }).click();
    await stable(adminPage);
    const candidate = adminPage.locator('.review-card').filter({
      has: adminPage.getByRole('heading', { name: attachmentBefore.title, exact: true }),
    });
    await visible(candidate);
    await candidate.getByRole('button', { name: '核对附件完整程度', exact: true }).click();
    await visible(candidate.locator('.candidate-attachments .quality-document'));
    assert.match(await candidate.locator('.candidate-attachments').innerText(), /已解析/);
    await candidate.getByLabel('理由', { exact: true }).fill('已核对仅合成附件、材料、日期和授权来源标识。');
    const accepted = adminPage.waitForResponse(response => new URL(response.url()).pathname.endsWith('/decision')
      && response.request().method() === 'POST');
    await candidate.getByRole('button', { name: '提交审核决定', exact: true }).click();
    assert.equal((await accepted).status(), 200);
    await control(admin, adminSession, fixture.drain_path);
    const published = await json(student, '/api/v1/events/' + fixture.attachment_event_id);
    assert.equal(published.version, attachmentBefore.version + 1);
    assert.ok(published.evidence.some(row => row.capture?.method === 'operator_upload'
      && row.capture.original_verified === false));
    const quality = await json(student, '/api/v1/events/' + fixture.attachment_event_id + '/quality');
    assert.ok(quality.documents.some(row => row.snapshot_id === report.replacement.snapshot_id && row.state === 'ok'));
    assert.ok(!quality.documents.some(row => row.snapshot_id === fixture.attachment_snapshot_id));
    const detail = await openEvent(adminPage, published);
    await detail.getByRole('button', { name: /^原文依据/ }).click();
    const provenance = detail.getByText('授权人工补充 · 未从原网页直接获取', { exact: true }).first();
    await visible(provenance);
    await provenance.scrollIntoViewIfNeeded();
    await screenshot(adminPage, '06-approved-attachment-provenance');
    await adminPage.keyboard.press('Escape');
  });

  assert.deepEqual(report.pageErrors, []);
  assert.deepEqual(report.failedResponses, []);
  assert.deepEqual(report.failedRequests, []);
  assert.equal(a11yReport.scenarios.length, 2);
  const blocking = a11yReport.scenarios.flatMap(scenario => scenario.violations
    .filter(row => ['serious', 'critical'].includes(row.impact)).map(row => ({ scenario: scenario.label, ...row })));
  const undetermined = a11yReport.scenarios.flatMap(scenario => scenario.incomplete
    .filter(row => row.id.startsWith('aria-')).map(row => ({ scenario: scenario.label, ...row })));
  assert.deepEqual(blocking, [], 'Serious or critical accessibility violations');
  assert.deepEqual(undetermined, [], 'ARIA rules left undetermined');
  a11yReport.status = 'passed';
  report.status = 'passed';
  await student.close();
  await admin.close();
} catch (error) {
  report.status = 'failed';
  a11yReport.status = a11yReport.scenarios.length === 2 ? 'failed' : 'incomplete';
  report.error = error.stack;
  process.exitCode = 1;
  console.error(error.stack);
  if (active) await screenshot(active, 'reliability-failure').catch(() => {});
} finally {
  await fs.writeFile(path.join(out, 'reliability-report.json'), JSON.stringify(report, null, 2));
  await fs.writeFile(path.join(out, 'reliability-a11y-report.json'), JSON.stringify(a11yReport, null, 2));
  await browser.close();
}

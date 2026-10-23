/**
 * Verify that search highlighting is honest against real server data.
 *
 * `highlight.test.mjs` pins the pure function; this entry pins the contract
 * that actually reaches a reader:
 *   1. the marked-up text is still the excerpt the API returned, character for
 *      character — evidence excerpts are substring-verified, so a highlight
 *      that rewrites them would quietly invalidate the evidence;
 *   2. every marked run really does come from the query, so the colour is not
 *      decorative noise;
 *   3. the mark colour clears WCAG AA against its real rendered background,
 *      measured from computed styles rather than from a palette spreadsheet;
 *   4. the mark adds no padding or border, so it cannot change line breaking.
 *
 * Runs against the labelled loopback demo with no route interception.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { loadPlaywright, relativeArtifact } from './playwright.mjs';

const { chromium } = loadPlaywright();
const base = (process.env.CAMPUS_E2E_URL || 'http://127.0.0.1:8021').replace(/\/$/, '');
const address = new URL(base);
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(address.hostname), 'Highlight verification requires a loopback demo');
assert.ok(['http:', 'https:'].includes(address.protocol) && !address.username && !address.password && !address.search && !address.hash);
const query = process.env.CAMPUS_E2E_QUERY || '奖学金申请材料';
const out = path.resolve(process.env.CAMPUS_E2E_OUTPUT || '.campus-highlight-output');
await fs.mkdir(out, { recursive: true });

const report = {
  base, query,
  mode: 'Real browser_demo search results. Synthetic notices only; no intercepted requests.',
  min_contrast_required: 4.5,
  hits: [], marks: [], contrast: [], screenshots: [], pageErrors: [], failedResponses: [],
};

const browser = await chromium.launch({ headless: true });
report.browser = browser.version();
let active;
const visible = (locator) => locator.waitFor({ state: 'visible', timeout: 15000 });

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' });
  const health = await context.request.get(base + '/api/v1/health/ready');
  assert.equal(health.status(), 200);
  assert.equal((await health.json()).demo_data, true, 'Explicit demo_data=true required');

  const page = await context.newPage();
  active = page;
  page.on('pageerror', (error) => report.pageErrors.push(error.message));
  page.on('response', (response) => {
    if (response.status() >= 500) report.failedResponses.push({ path: new URL(response.url()).pathname, status: response.status() });
  });

  const stable = async () => {
    await page.waitForFunction(() => !document.querySelector('.loading'), null, { timeout: 15000 });
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  };
  const nav = async (name) => {
    const opener = page.getByRole('button', { name: '打开菜单', exact: true });
    if (await opener.isVisible()) await opener.click();
    await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name, exact: true }).click();
    await visible(page.locator('main h1').first());
    await stable();
  };
  const login = async (email) => {
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
    await stable();
  };

  await page.goto(base + '/campus.html');
  await visible(page.locator('.demo-banner'));
  assert.match(await page.locator('.demo-banner').innerText(), /合成演示数据/);
  await visible(page.locator('main h1').first());
  await stable();
  await login('student@example.com');

  await nav('找信息');
  const input = page.getByLabel('搜索关键词', { exact: true });
  await input.fill(query);
  const pending = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/v1/search' && response.request().method() === 'GET');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  const response = await pending;
  assert.equal(response.status(), 200);
  const payload = await response.json();
  assert.ok(Array.isArray(payload.hits) && payload.hits.length > 0, 'The demo must return at least one hit for "' + query + '"');
  report.hits = payload.hits.map((hit) => ({ event_id: hit.event_id, title: hit.title, excerpt: hit.excerpt }));

  await visible(page.locator('.search-result').first());
  await stable();

  // Contrast is computed from the colours the browser actually resolved, walking
  // up the tree for the first non-transparent background and compositing any
  // alpha, so a translucent panel cannot hide a low-contrast mark.
  const measured = await page.evaluate(() => {
    const parse = (value) => {
      const match = /^rgba?\(([^)]+)\)$/.exec((value || '').trim());
      if (!match) return null;
      const parts = match[1].split(/[\s,/]+/).filter(Boolean).map(Number);
      if (parts.length < 3 || parts.some((part) => Number.isNaN(part))) return null;
      return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
    };
    const over = (front, back) => ({
      r: front.r * front.a + back.r * (1 - front.a),
      g: front.g * front.a + back.g * (1 - front.a),
      b: front.b * front.a + back.b * (1 - front.a),
      a: 1,
    });
    const luminance = (colour) => {
      const channel = (value) => {
        const scaled = value / 255;
        return scaled <= 0.03928 ? scaled / 12.92 : Math.pow((scaled + 0.055) / 1.055, 2.4);
      };
      return 0.2126 * channel(colour.r) + 0.7152 * channel(colour.g) + 0.0722 * channel(colour.b);
    };
    const ratio = (a, b) => {
      const first = luminance(a);
      const second = luminance(b);
      return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
    };
    const backdrop = (node) => {
      const layers = [];
      let current = node;
      while (current) {
        const parsed = parse(getComputedStyle(current).backgroundColor);
        if (parsed && parsed.a > 0) {
          layers.push(parsed);
          if (parsed.a === 1) break;
        }
        current = current.parentElement;
      }
      let result = { r: 255, g: 255, b: 255, a: 1 };
      for (let index = layers.length - 1; index >= 0; index -= 1) result = over(layers[index], result);
      return result;
    };
    const rounded = (value) => Math.round(value * 100) / 100;
    const results = [];
    for (const card of document.querySelectorAll('.search-result')) {
      const title = card.querySelector('.card-title');
      const paragraph = card.querySelector('p');
      const collect = (root, label) => {
        if (!root) return;
        for (const mark of root.querySelectorAll('mark')) {
          const style = getComputedStyle(mark);
          const background = backdrop(mark);
          const foreground = over(parse(style.color) || { r: 0, g: 0, b: 0, a: 1 }, background);
          results.push({
            region: label,
            text: mark.textContent,
            color: style.color,
            background: `rgb(${Math.round(background.r)}, ${Math.round(background.g)}, ${Math.round(background.b)})`,
            contrast: rounded(ratio(foreground, background)),
            padding_top: style.paddingTop, padding_bottom: style.paddingBottom,
            padding_left: style.paddingLeft, padding_right: style.paddingRight,
            border_top: style.borderTopWidth, border_bottom: style.borderBottomWidth,
            border_left: style.borderLeftWidth, border_right: style.borderRightWidth,
          });
        }
      };
      collect(title, 'title');
      collect(paragraph, 'excerpt');
    }
    return {
      cards: Array.from(document.querySelectorAll('.search-result')).map((card) => ({
        title: card.querySelector('.card-title')?.textContent?.trim() ?? '',
        excerpt: card.querySelector('p')?.textContent ?? '',
      })),
      marks: results,
    };
  });

  report.marks = measured.marks;
  report.contrast = measured.marks.map((mark) => ({ region: mark.region, text: mark.text, contrast: mark.contrast, background: mark.background }));

  assert.equal(measured.cards.length, payload.hits.length, 'Rendered result count must match the API hit count');
  measured.cards.forEach((card, index) => {
    const hit = payload.hits[index];
    assert.equal(card.title, hit.title, 'Highlighted title must be the API title, unchanged (hit ' + index + ')');
    assert.equal(card.excerpt, hit.excerpt, 'Highlighted excerpt must be the API excerpt, character for character (hit ' + index + ')');
  });

  assert.ok(measured.marks.length > 0, 'No <mark> was rendered: the query "' + query + '" matched the index but nothing was highlighted');
  // Adjacent matches are merged into one <mark> so the background has no seam,
  // so a mark is a *concatenation* of query substrings rather than necessarily
  // one substring ("奖学金" + "材料" merges to "奖学金材料" for the query
  // "奖学金申请材料"). Check the general property with a small DP.
  const coverable = (text) => {
    const reachable = new Array(text.length + 1).fill(false);
    reachable[0] = true;
    for (let end = 1; end <= text.length; end += 1) {
      for (let start = 0; start < end; start += 1) {
        if (reachable[start] && query.includes(text.slice(start, end))) {
          reachable[end] = true;
          break;
        }
      }
    }
    return reachable[text.length];
  };
  const strays = measured.marks.filter((mark) => !coverable(mark.text));
  assert.deepEqual(strays, [],
    'Every marked run must be built from query substrings: ' + JSON.stringify(measured.marks.map((mark) => mark.text)));

  const zero = ['padding_top', 'padding_bottom', 'padding_left', 'padding_right', 'border_top', 'border_bottom', 'border_left', 'border_right'];
  for (const mark of measured.marks) {
    for (const key of zero) {
      assert.equal(parseFloat(mark[key]), 0, 'mark must not add ' + key + ' (it would change line breaking): ' + JSON.stringify(mark));
    }
  }

  const worst = measured.marks.reduce((lowest, mark) => (lowest === null || mark.contrast < lowest.contrast ? mark : lowest), null);
  report.worst_contrast = worst;
  assert.ok(worst.contrast >= 4.5,
    'Highlighted text must clear WCAG AA: worst was ' + worst.contrast + ':1 (' + worst.region + ' ' + JSON.stringify(worst.text) + ')');

  await page.screenshot({ path: path.join(out, 'highlight-contrast.png'), animations: 'disabled' });
  report.screenshots.push(relativeArtifact(path.join(out, 'highlight-contrast.png')));
  assert.deepEqual(report.pageErrors, []);
  assert.deepEqual(report.failedResponses, []);
  report.status = 'passed';
  console.log(`PASS highlight · ${measured.marks.length} mark(s) across ${measured.cards.length} hit(s), worst contrast ${worst.contrast}:1`);
} catch (error) {
  report.status = 'failed';
  report.error = error.stack;
  process.exitCode = 1;
  console.error(error.stack);
  if (active) await active.screenshot({ path: path.join(out, 'highlight-failure.png'), animations: 'disabled' }).catch(() => {});
} finally {
  await fs.writeFile(path.join(out, 'highlight-report.json'), JSON.stringify(report, null, 2));
  await browser.close();
}

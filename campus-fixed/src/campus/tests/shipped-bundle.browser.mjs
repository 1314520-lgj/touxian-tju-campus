/**
 * Verify the SHIPPED bundle (dist/) in a real Chromium.
 *
 * This is the one test that looks at the built artifact rather than the source
 * tree: it serves dist/ over a local HTTP server, answers /api/v1/* from
 * fixtures, and asserts on rendered DOM and computed layout. Deadlines in the
 * fixtures are expressed relative to today in Asia/Shanghai, so the urgency
 * expectations are exact instead of depending on what the database holds.
 *
 * Run it with a Playwright installation pointed at from the outside, the same
 * way the .e2e.mjs entries resolve theirs:
 *
 *   PLAYWRIGHT_MODULE=<path to playwright-core> node src/campus/tests/shipped-bundle.browser.mjs
 *
 * It writes verification/task5/browser-shipped-bundle.log and screenshots into
 * verification/task5/browser/. Exit code is non-zero if any check fails.
 */

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const ROOT = process.cwd();
const DIST = path.join(ROOT, 'dist');
const OUT = path.join(ROOT, 'verification', 'task5', 'browser');

function loadPlaywright() {
  const candidates = [];
  if (process.env.PLAYWRIGHT_MODULE) candidates.push(process.env.PLAYWRIGHT_MODULE);
  candidates.push('playwright');
  candidates.push(path.join(process.cwd(), 'node_modules', 'playwright'));
  const failures = [];
  for (const candidate of candidates) {
    try { return require(candidate); } catch (error) { failures.push(`${candidate} (${error.code || error.message})`); }
  }
  throw new Error('Playwright not available:\n' + failures.map((line) => '  tried ' + line).join('\n'));
}

// ---- school-day helpers -----------------------------------------------------
const shanghai = (date = new Date()) =>
  new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
const shift = (iso, days) => {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
};

const TODAY = shanghai();
const DAYS = { today: TODAY, tomorrow: shift(TODAY, 1), in3: shift(TODAY, 3), in10: shift(TODAY, 10), past: shift(TODAY, -4) };

const event = (id, title, date, extra = {}) => ({
  id, title, version: 3, status: 'active', event_type: 'scholarship',
  action_required: true, action_steps: ['准备材料'], source_url: 'https://oaa.tju.edu.cn/notice',
  deadlines: [{ kind: 'school_deadline', precision: 'date', date, raw_text: date }],
  ...extra,
});

const EVENTS = [
  event('e-today', '【今天】国家奖学金申请通知', DAYS.today),
  event('e-tomorrow', '【明天】学业奖学金申请通知', DAYS.tomorrow),
  event('e-in3', '【三天内】社会实践报名通知', DAYS.in3),
  event('e-in10', '【十天后】交换生项目通知', DAYS.in10),
  event('e-past', '【已截止】上学年评优通知', DAYS.past),
];

const SEARCH_HITS = [{
  evidence_id: 'ev-1', event_id: 'e-today', event_version: 3, historical: false,
  title: '关于2026年国家奖学金申请的通知', excerpt: '申请材料包括成绩单与推荐信，请于截止前提交。',
  source_url: 'https://oaa.tju.edu.cn/notice',
}];

// ---- tiny static server -----------------------------------------------------
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png' };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const rel = url.pathname === '/' ? '/campus.html' : url.pathname;
  const file = path.join(DIST, rel);
  if (!file.startsWith(DIST) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404); res.end('not found'); return;
  }
  res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' });
  res.end(fs.readFileSync(file));
});

const report = [];
const check = (name, ok, detail = '') => { report.push(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -- ' + detail : ''}`); return ok; };

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const { chromium } = loadPlaywright();
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  console.log('serving dist/ at', base);

  const browser = await chromium.launch();
  try {
    for (const [label, viewport] of [['desktop', { width: 1440, height: 1000 }], ['mobile', { width: 390, height: 844 }]]) {
      const context = await browser.newContext({ viewport, deviceScaleFactor: 1 });
      const page = await context.newPage();
      try {
      const consoleErrors = [], pageErrors = [];
      page.on('console', (m) => {
        if (m.type() !== 'error') return;
        // The fixture answers /me with 401 to mean "not logged in"; the app is
        // designed to treat that as a signed-out visitor, and the browser still
        // logs the failed request. That is expected, not a defect.
        if (/401/.test(m.text()) && /Unauthorized/.test(m.text())) return;
        consoleErrors.push(m.text());
      });
      page.on('pageerror', (e) => pageErrors.push(String(e)));

      let slow = true;
      await page.route((url) => url.pathname.startsWith('/api/v1/'), async (route) => {
        const p = new URL(route.request().url()).pathname;
        const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
        if (p === '/api/v1/me') return json({ message: '未登录' }, 401);
        if (p === '/api/v1/catalog') return json({ colleges: [], event_types: [], education_levels: [] });
        if (p === '/api/v1/sources') return json({ items: [], next_cursor: null });
        if (p === '/api/v1/health/ready') return json({ status: 'ok' });
        if (p === '/api/v1/events') {
          if (slow) { await new Promise((r) => setTimeout(r, 900)); slow = false; }
          return json({ items: EVENTS, next_cursor: null });
        }
        if (p === '/api/v1/search') return json({ hits: SEARCH_HITS, next_cursor: null, index_status: 'ready' });
        return json({}, 404);
      });

      await page.goto(base + '/campus.html', { waitUntil: 'domcontentloaded' });

      // 1. skeleton cards must be visible while the first page is in flight
      let skeleton = true;
      try { await page.waitForSelector('.skeleton-card', { timeout: 4000 }); }
      catch { skeleton = false; }
      check(`${label}: skeleton cards shown during first load`, skeleton);

      await page.waitForSelector('.event-card', { timeout: 15000 });
      check(`${label}: event cards rendered`, true);
      await page.screenshot({ path: path.join(OUT, `${label}-home.png`), fullPage: true });

      // 2. urgency tags, computed from the same school-day rule the app uses
      const tags = await page.locator('.event-card .tag.urgent').allTextContents();
      const expectToday = shift(TODAY, 0) === DAYS.today;
      check(`${label}: urgency tag "今天截止"`, expectToday && tags.includes('今天截止'), JSON.stringify(tags));
      check(`${label}: urgency tag "明天截止"`, tags.includes('明天截止'), JSON.stringify(tags));
      check(`${label}: urgency tag "3 天内截止"`, tags.includes('3 天内截止'), JSON.stringify(tags));
      check(`${label}: far deadline gets no urgency tag`, tags.length === 3, JSON.stringify(tags));
      check(`${label}: today tag carries the .today class`,
        await page.locator('.event-card .tag.urgent.today').count() === 1);

      // 3. past deadline is filed under history, not the live list
      const past = await page.locator('details.past-items').count();
      check(`${label}: expired event moved into 历史通知`, past === 1);

      // 4. document.title follows the page
      const titleToday = await page.title();
      check(`${label}: document.title on home`, titleToday === '今天 · 偷闲 · 天大版', titleToday);

      // 5. search page: title updates, and a highlighted title stays inline
      // At phone widths the navigation lives in a drawer that starts closed.
      const navSearch = page.locator('nav[aria-label="主导航"] button', { hasText: '找信息' });
      if (!(await navSearch.isVisible())) {
        await page.locator('.mobile-menu').click();
        await page.waitForTimeout(200);
      }
      await navSearch.click();
      await page.waitForTimeout(150);
      const titleSearch = await page.title();
      check(`${label}: document.title after navigating`, titleSearch === '找信息 · 偷闲 · 天大版', titleSearch);

      await page.getByLabel('搜索关键词').fill('奖学金');
      await page.getByRole('button', { name: '搜索', exact: true }).click();
      await page.waitForSelector('.search-result', { timeout: 10000 });
      const wrapped = await page.locator('.search-result .card-title > span').count();
      const marks = await page.locator('.search-result .card-title mark').count();
      check(`${label}: search title highlight wrapped in one span`, wrapped === 1, `spans=${wrapped}`);
      check(`${label}: search title still highlights the query`, marks >= 1, `marks=${marks}`);

      // The regression being guarded: highlightRuns used to return several runs,
      // and .card-title is a space-between flex row, so the runs were pushed to
      // opposite ends of the card ("2026 年国家        奖学金        申请通知").
      // One wrapper span keeps the title a single flex child and therefore contiguous.
      const spread = await page.locator('.search-result .card-title').first().evaluate((el) => {
        const span = el.querySelector(':scope > span');
        const range = document.createRange();
        range.selectNodeContents(span);
        const rects = [...range.getClientRects()].filter((r) => r.width > 0);
        let maxGap = 0;
        for (let i = 1; i < rects.length; i++) maxGap = Math.max(maxGap, Math.round(rects[i].left - rects[i - 1].right));
        return { directChildren: el.children.length, lineRects: rects.length, maxGap };
      });
      check(`${label}: title is one flex child plus the chevron`, spread.directChildren === 2, JSON.stringify(spread));
      check(`${label}: highlighted title stays contiguous`, spread.maxGap <= 8, JSON.stringify(spread));
      await page.screenshot({ path: path.join(OUT, `${label}-search.png`), fullPage: true });

      // 6. no uncaught errors anywhere
      check(`${label}: no uncaught page errors`, pageErrors.length === 0, pageErrors.join(' | '));
      check(`${label}: no console errors`, consoleErrors.length === 0, consoleErrors.join(' | '));
      } catch (error) {
        check(`${label}: run completed without a harness error`, false, String(error).split('\n')[0]);
      } finally {
        await context.close();
      }
    }
  } finally {
    await browser.close();
    server.close();
  }

  const failures = report.filter((l) => l.startsWith('FAIL')).length;
  const text = [
    'Real-browser verification of the SHIPPED bundle (dist/)',
    `served from: dist/   school day: ${TODAY} (Asia/Shanghai)`,
    '',
    ...report,
    '',
    `${report.filter((l) => l.startsWith('PASS')).length} passed, ${failures} failed`,
  ].join('\n');
  fs.writeFileSync(path.join(OUT, '..', 'browser-shipped-bundle.log'), text + '\n', 'utf8');
  console.log(text);
  return failures ? 1 : 0;
}

main().then((code) => process.exit(code)).catch((error) => { console.error(error); process.exit(2); });

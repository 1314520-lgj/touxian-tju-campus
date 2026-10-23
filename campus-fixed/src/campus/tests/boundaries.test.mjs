import test from 'node:test';
import assert from 'node:assert/strict';
import { extractLocal, draftFromFields, fieldsFromDraft } from '../localExtract.js';
import { api, request, setSession, safeURL, APIError, query, snapshotURL } from '../api.js';

test('local extraction keeps raw text and excerpt out of default DTO', () => {
  const raw = '测试报名\n请于2026年10月22日前提交申请表\n材料：成绩单\nhttps://example.test/apply';
  const result = extractLocal(raw);
  assert.equal(result.draft.deadline_date, '2026-10-22');
  assert.equal(result.draft.deadline_at, null);
  assert.equal(result.draft.evidence_excerpt, null);
  assert.equal(result.draft.excerpt_sync_consent, false);
  assert.ok(!Object.keys(result.draft).includes('raw_text'));
});
test('relative date remains unresolved rather than invented', () => {
  const result = extractLocal('报名通知\n请于下周五之前提交申请');
  assert.equal(result.draft.deadline_date, null);
  assert.ok(result.warnings.some((line) => line.includes('相对日期')));
});
test('non-deadline date is not assigned as a deadline', () => {
  const result = extractLocal('讲座通知\n2026年10月22日举行讲座');
  assert.equal(result.draft.deadline_date, null);
});
test('deadline line is preferred to preceding activity date', () => {
  const result = extractLocal('活动于2026年10月30日举办\n报名截止2026年10月22日');
  assert.equal(result.draft.deadline_date, '2026-10-22');
  assert.ok(result.warnings.some((line) => line.includes('多个日期')));
});
test('invalid calendar date is never normalized silently', () => {
  assert.equal(extractLocal('截止2026年2月30日提交').draft.deadline_date, null);
  assert.equal(extractLocal('截止2026-10-32提交').draft.deadline_date, null);
});
test('explicit time is converted from Asia/Shanghai and date-only stays date-only', () => {
  const base = { title: '我的事项', actions: '提交申请', materials: '', urls: '', date: '2026-10-22', time: '', consent: false, excerpt: '此文字默认不上传' };
  const dateOnly = draftFromFields(base);
  assert.equal(dateOnly.deadline_at, null); assert.equal(dateOnly.deadline_date, '2026-10-22'); assert.equal(dateOnly.evidence_excerpt, null);
  const precise = draftFromFields({ ...base, time: '17:30' });
  assert.equal(precise.deadline_at, '2026-10-22T09:30:00.000Z'); assert.equal(precise.deadline_date, null);
  assert.equal(fieldsFromDraft(precise).time, '17:30');
});
test('only explicit excerpt consent syncs an excerpt', () => {
  const fields = fieldsFromDraft(extractLocal('个人事项').draft);
  fields.excerpt = '我确认的摘录';
  assert.equal(draftFromFields(fields).evidence_excerpt, null);
  fields.consent = true;
  assert.equal(draftFromFields(fields).evidence_excerpt, '我确认的摘录');
});
test('URL boundary rejects executable or relative URLs', () => {
  assert.equal(safeURL('javascript:alert(1)'), null); assert.equal(safeURL('/api/secret'), null);
  assert.equal(safeURL('https://example.test/'), 'https://example.test/');
});
test('query values are encoded without losing false', () => {
  assert.equal(query({ q: '资料 & 奖学金', include_historical: false, cursor: null }), '?q=%E8%B5%84%E6%96%99+%26+%E5%A5%96%E5%AD%A6%E9%87%91&include_historical=false');
});
test('client sends cookie, CSRF, idempotency and confirmed DTO only', async () => {
  let captured;
  setSession({ csrf_token: 'test-csrf' });
  globalThis.fetch = async (url, options) => { captured = { url, options }; return new Response(JSON.stringify({ id: 'item' }), { status: 201 }); };
  const body = extractLocal('报名通知\n截止2026年10月22日提交').draft;
  await api.addItem(body, 'test-key');
  assert.equal(captured.url, '/api/v1/me/items'); assert.equal(captured.options.credentials, 'include');
  assert.equal(captured.options.headers['X-CSRF-Token'], 'test-csrf');
  assert.equal(captured.options.headers['Idempotency-Key'], 'test-key');
  assert.deepEqual(JSON.parse(captured.options.body), body);
});
test('client exposes conflict details without manufacturing success', async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ code: 'REVISION_CONFLICT', message: '版本已更新', request_id: 'test-id', details: {} }), { status: 409 });
  await assert.rejects(() => request('/me/items/id', { method: 'PATCH', body: {} }), (error) => error instanceof APIError && error.status === 409 && error.requestId === 'test-id');
});
test('authorized attachment uploads preserve multipart bytes and CSRF without JSON encoding', async () => {
  let captured;
  setSession({ csrf_token: 'upload-csrf' });
  globalThis.fetch = async (url, options) => { captured = { url, options }; return new Response(JSON.stringify({ snapshot_id: 'replacement' }), { status: 201 }); };
  const text = '材料：经授权提供的申请表。';
  await api.replaceAttachment('source-snapshot', new File([text], 'authorized.txt', { type: 'text/plain' }), '明确授权依据', 'upload-idempotency');
  assert.equal(captured.url, '/api/v1/admin/snapshots/source-snapshot/replacement');
  assert.equal(captured.options.credentials, 'include');
  assert.equal(captured.options.headers['Content-Type'], undefined, 'Browser supplies multipart boundary');
  assert.equal(captured.options.headers['X-CSRF-Token'], 'upload-csrf');
  assert.equal(captured.options.headers['Idempotency-Key'], 'upload-idempotency');
  assert.ok(captured.options.body instanceof FormData);
  assert.equal(captured.options.body.get('basis'), '明确授权依据');
  assert.equal(await captured.options.body.get('file').text(), text);
});
test('network failure stays an error; no implicit fixtures', async () => {
  globalThis.fetch = async () => { throw new TypeError('offline'); };
  await assert.rejects(() => api.events(), (error) => error.code === 'NETWORK_ERROR');
});
test('signed out session removes CSRF token', async () => {
  setSession(null); let captured;
  globalThis.fetch = async (_, options) => { captured = options; return new Response(null, { status: 204 }); };
  await request('/auth/session', { method: 'DELETE' });
  assert.equal(captured.headers['X-CSRF-Token'], undefined);
});

test('relative signed snapshot paths resolve only within known same-origin boundary', () => {
  const origin = 'http://127.0.0.1:8000';
  const id = '309a39ae-284e-5d0b-9714-17d47af24c3d';
  const path = '/api/v1/snapshots/' + id + '/file?read_token=known';
  assert.equal(snapshotURL(path, origin), origin + path);
  assert.equal(snapshotURL('https://attacker.test' + path, origin), null);
  assert.equal(snapshotURL('/api/v1/me?read_token=known', origin), null);
  assert.equal(safeURL(path), null);
});

test('OCR-inserted whitespace preserves an explicit Chinese deadline date', () => {
  const result = extractLocal('校园 奖学金 申请 通知\n请 于 2026 年 12 月 22 日 前 提交 申请表\n材料：成绩单');
  assert.equal(result.draft.deadline_date, '2026-12-22');
  assert.equal(result.draft.deadline_at, null);
  assert.equal(result.draft.evidence_excerpt, null);
});

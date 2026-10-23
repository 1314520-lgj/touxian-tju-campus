import test from 'node:test';
import assert from 'node:assert/strict';
import { api, normalizeSubscriptions, setSession } from '../api.js';

test('subscription migration preserves legacy all and selected meanings and permits explicit zero', () => {
  assert.deepEqual(normalizeSubscriptions({ source_ids: [], event_types: ['academic'] }), { source_mode: 'all', source_ids: [], event_types: ['academic'] });
  assert.equal(normalizeSubscriptions({ source_ids: ['source'], event_types: [] }).source_mode, 'selected');
  assert.equal(normalizeSubscriptions({ source_ids: [], event_types: [], source_mode: 'selected' }).source_mode, 'selected');
  const initial = { source_ids: ['one'], event_types: ['activity'], source_mode: 'all' };
  const normalized = normalizeSubscriptions(initial); normalized.source_ids.push('two');
  assert.deepEqual(initial.source_ids, ['one']); assert.equal(normalized.source_mode, 'all');
});
test('source requests use the real HTTP boundary with CSRF, idempotency and encoded pagination', async () => {
  const before = globalThis.fetch, calls = [];
  globalThis.fetch = async (url, options) => { calls.push({ url, options }); return new Response(JSON.stringify({ id: 'server-issued-id' }), { status: options.method === 'POST' ? 201 : 200, headers: { 'Content-Type': 'application/json' } }); };
  try {
    setSession({ csrf_token: 'server-issued-csrf' });
    const body = { name: '天津大学', kind: 'wechat', url: null, auto_subscribe: true };
    await api.addSourceRequest(body, 'create-key');
    await api.sourceRequests('cursor & two');
    await api.adminSourceRequests();
    await api.resolveSourceRequest('request-id', { decision: 'reject', source_id: null, note: '缺少授权渠道' }, 'resolve-key');
    assert.equal(calls[0].url, '/api/v1/me/source-requests'); assert.deepEqual(JSON.parse(calls[0].options.body), body);
    assert.equal(calls[0].options.headers['X-CSRF-Token'], 'server-issued-csrf'); assert.equal(calls[0].options.headers['Idempotency-Key'], 'create-key');
    assert.equal(calls[1].url, '/api/v1/me/source-requests?cursor=cursor+%26+two&limit=100');
    assert.equal(calls[2].url, '/api/v1/admin/source-requests?limit=100');
    assert.equal(calls[3].url, '/api/v1/admin/source-requests/request-id/resolve');
    assert.equal(calls[3].options.headers['Idempotency-Key'], 'resolve-key'); assert.equal(calls[3].options.credentials, 'include');
  } finally { globalThis.fetch = before; setSession(null); }
});

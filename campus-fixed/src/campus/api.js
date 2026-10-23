/** One HTTP boundary. No fallback fixtures, local accounts, or forged success. */
let csrf = null;
const prefix = '/api/v1';
export class APIError extends Error {
  constructor(status, data) {
    super(data?.message || ('请求失败（' + status + '）'));
    this.name = 'APIError'; this.status = status; this.code = data?.code || 'HTTP_ERROR';
    this.requestId = data?.request_id; this.details = data?.details;
  }
}
export function setSession(session) { csrf = session?.csrf_token || null; }
export function idempotencyKey() { return crypto.randomUUID(); }
export function query(values) {
  const params = new URLSearchParams();
  Object.entries(values || {}).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') params.set(key, String(value));
  });
  return params.size ? '?' + params : '';
}
export async function request(path, { method = 'GET', body, key, signal, metadata = false } = {}) {
  const headers = { Accept: 'application/json' };
  const multipart = typeof FormData !== 'undefined' && body instanceof FormData;
  if (body !== undefined && !multipart) headers['Content-Type'] = 'application/json';
  if (!['GET', 'HEAD'].includes(method) && csrf) headers['X-CSRF-Token'] = csrf;
  if (key) headers['Idempotency-Key'] = key;
  let response;
  try {
    response = await fetch(prefix + path, { method, credentials: 'include', headers, body: body === undefined ? undefined : multipart ? body : JSON.stringify(body), signal });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new APIError(0, { code: 'NETWORK_ERROR', message: '连接不到服务，请检查网络或服务是否已启动。' });
  }
  const text = response.status === 204 ? '' : await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch {
      if (response.ok) throw new APIError(response.status, { code: 'INVALID_RESPONSE', message: '服务返回格式异常，请稍后重试。' });
    }
  }
  if (!response.ok) throw new APIError(response.status, data);
  if (data?.csrf_token) setSession(data);
  return metadata ? { data, headers: response.headers } : data;
}
export const api = {
  me: () => request('/me'),
  challenge: (email) => request('/auth/challenges', { method: 'POST', body: { email }, metadata: true }),
  login: (challenge_id, code) => request('/auth/sessions', { method: 'POST', body: { challenge_id, code } }),
  logout: () => request('/auth/session', { method: 'DELETE' }),
  catalog: () => request('/catalog'),
  sources: (cursor) => request('/sources' + query({ cursor, limit: 100 })),
  sourceRequests: (cursor) => request('/me/source-requests' + query({ cursor, limit: 100 })),
  addSourceRequest: (body, key) => request('/me/source-requests', { method: 'POST', body, key }),
  adminSourceRequests: (cursor) => request('/admin/source-requests' + query({ cursor, limit: 100 })),
  resolveSourceRequest: (id, body, key) => request('/admin/source-requests/' + id + '/resolve', { method: 'POST', body, key }),
  profile: () => request('/me/profile'), saveProfile: (body) => request('/me/profile', { method: 'PUT', body }),
  subscriptions: () => request('/me/subscriptions'), saveSubscriptions: (body) => request('/me/subscriptions', { method: 'PUT', body }),
  feed: (cursor) => request('/me/feed' + query({ cursor, limit: 100 })),
  events: (cursor) => request('/events' + query({ cursor, limit: 100 })),
  event: (id) => request('/events/' + id),
  versions: (id, cursor) => request('/events/' + id + '/versions' + query({ cursor, limit: 100 })),
  changes: (id) => request('/events/' + id + '/changes'),
  quality: (id) => request('/events/' + id + '/quality'),
  state: (id) => request('/me/events/' + id + '/state'),
  setState: (id, body) => request('/me/events/' + id + '/state', { method: 'PATCH', body }),
  items: (cursor) => request('/me/items' + query({ cursor, limit: 100 })),
  addItem: (body, key) => request('/me/items', { method: 'POST', body, key }),
  patchItem: (id, body) => request('/me/items/' + id, { method: 'PATCH', body }),
  deleteItem: (id) => request('/me/items/' + id, { method: 'DELETE' }),
  search: (q, values) => request('/search' + query({ q, limit: 100, ...values })),
  answer: (body) => request('/answers', { method: 'POST', body }),
  preferences: () => request('/me/preferences'), savePreferences: (body) => request('/me/preferences', { method: 'PUT', body }),
  pushStatus: () => request('/me/push-status'),
  digests: (cursor) => request('/me/digests' + query({ cursor, limit: 100 })),
  deliveries: (cursor) => request('/me/deliveries' + query({ cursor, limit: 100 })),
  calendarToken: () => request('/me/calendar-token', { method: 'POST', key: idempotencyKey() }),
  revokeCalendar: () => request('/me/calendar-token', { method: 'DELETE' }),
  snapshot: (id) => request('/snapshots/' + id), parsed: (id) => request('/snapshots/' + id + '/parsed'), evidence: (id) => request('/evidence/' + id),
  adminSnapshot: (id) => request('/admin/snapshots/' + id), adminParsed: (id) => request('/admin/snapshots/' + id + '/parsed'),
  replaceAttachment: (id, file, basis, key) => {
    const body = new FormData(); body.append('file', file); body.append('basis', basis);
    return request('/admin/snapshots/' + id + '/replacement', { method: 'POST', body, key });
  },
  adminEvents: (cursor) => request('/admin/events' + query({ cursor, limit: 100 })),
  adminSources: (cursor) => request('/admin/sources' + query({ cursor, limit: 100 })),
  addSource: (body, key) => request('/admin/sources', { method: 'POST', body, key }),
  patchSource: (id, body) => request('/admin/sources/' + id, { method: 'PATCH', body }),
  verifySource: (id, body) => request('/admin/sources/' + id + '/verification', { method: 'POST', body }),
  fetchSource: (id) => request('/admin/sources/' + id + '/fetch', { method: 'POST', key: idempotencyKey() }),
  reviews: (cursor) => request('/admin/reviews' + query({ cursor, limit: 100 })),
  review: (id, body) => request('/admin/reviews/' + id + '/decision', { method: 'POST', body }),
  publication: (id, body) => request('/admin/events/' + id + '/publication', { method: 'PATCH', body }),
  jobs: (cursor) => request('/admin/jobs' + query({ cursor, limit: 100 })),
  job: (id) => request('/admin/jobs/' + id),
  retryJob: (id) => request('/admin/jobs/' + id + '/retry', { method: 'POST', key: idempotencyKey() }),
  ready: () => request('/health/ready'),
};
export function safeURL(value) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : null; } catch { return null; }
}

// Only server-issued snapshot file paths may be resolved relative to this origin.
export function snapshotURL(value, origin) {
  try {
    const url = new URL(value, origin);
    return url.origin === new URL(origin).origin && /^\/api\/v1\/(?:admin\/)?snapshots\/[0-9a-f-]+\/file$/.test(url.pathname) && url.searchParams.has('read_token') ? url.href : null;
  } catch { return null; }
}

// Legacy nonempty source_ids filtered sources; an explicit selected mode also permits zero.
export function normalizeSubscriptions(value) {
  return { source_ids: [...(value?.source_ids || [])], event_types: [...(value?.event_types || [])], source_mode: value?.source_mode || (value?.source_ids?.length ? 'selected' : 'all') };
}

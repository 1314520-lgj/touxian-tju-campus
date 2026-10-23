import React, { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ArrowUpRight, Bell, BookOpen, CalendarDays, Check, ChevronRight, CircleCheck, Clock3, FileSearch, Home, Inbox, LogOut, Menu, Plus, RefreshCw, Search, Settings2, ShieldCheck, Sparkles, Star, X } from 'lucide-react';
import { api, idempotencyKey, normalizeSubscriptions, safeURL, setSession, snapshotURL } from './api.js';
import { draftFromFields, extractLocal, fieldsFromDraft } from './localExtract.js';
import { campusDate, cardDeadlineList, deadlineUrgency, eventHasEnded, nextDeadlineKey, personalHasEnded, personalUrgency, urgencyLabel } from './feedView.js';
import { highlightRuns } from './highlight.js';

const Context = createContext(null);
const useApp = () => useContext(Context);
const types = { academic: '教务', assignment: '作业', scholarship: '奖助', competition: '竞赛', activity: '活动', employment: '就业', meeting: '会议', other: '其他' };
const states = { todo: '待办', in_progress: '进行中', done: '已完成', archived: '已归档' };
const statuses = { active: '进行中', cancelled: '已取消', under_review: '待核实' };
const changeKinds = { deadline_or_date_touched: '时间或期限', content_added: '新增内容', content_removed: '删除内容', wording_revised: '措辞调整' };
function changeCountsText(counts) {
  const parts = [];
  if (counts?.added) parts.push('新增 ' + counts.added + ' 段');
  if (counts?.removed) parts.push('删除 ' + counts.removed + ' 段');
  if (counts?.rewritten) parts.push('改写 ' + counts.rewritten + ' 段');
  return parts.join('、') || '内容有变化';
}
const healthLabels = { healthy: '正常', degraded: '更新缓慢', auth_expired: '授权到期', failed: '采集失败', never_checked: '尚未检查' };
const verificationLabels = { verified_official: '官方来源', verified_organization: '组织来源', unverified: '尚未核验' };
const feedContentLabels = { link: '原网页', authorized_fulltext: '已授权订阅全文' };
const deliveryLabels = { scheduled: '已安排', sending: '发送中', sent: '已送达', failed: '失败', unknown: '送达未确认', cancelled: '已取消' };
const jobLabels = { queued: '排队中', running: '运行中', succeeded: '已成功', failed: '失败', cancelled: '已取消' };
const localHosts = ['localhost', '127.0.0.1', '::1', '[::1]'];
const emptyDraft = { title: '', action_steps: [], materials: [], entry_urls: [], deadline_at: null, deadline_date: null, timezone: 'Asia/Shanghai', evidence_excerpt: null, excerpt_sync_consent: false };

// Overlapping dialogs and the navigation drawer may close in either order.
const scrollOwners = new Set();
let previousBodyOverflow = '';
const modalLayers = [];
function syncModalAccessibility() {
  const top = modalLayers[modalLayers.length - 1];
  const background = document.querySelector('.app-shell');
  if (background) background.inert = Boolean(top);
  modalLayers.forEach((layer) => {
    layer.inert = layer !== top;
    if (layer !== top) layer.setAttribute('aria-hidden', 'true');
    else layer.removeAttribute('aria-hidden');
  });
}
function lockBodyScroll() {
  const owner = {};
  if (!scrollOwners.size) previousBodyOverflow = document.body.style.overflow;
  scrollOwners.add(owner);
  document.body.style.overflow = 'hidden';
  return () => {
    if (scrollOwners.delete(owner) && !scrollOwners.size) document.body.style.overflow = previousBodyOverflow;
  };
}

function CaptureNotice({ capture }) {
  if (capture?.method === 'operator_upload') return <span className="warning-line">授权人工补充 · 未从原网页直接获取</span>;
  return capture?.method === 'authorized_feed' ? <span className="warning-line">订阅供稿 · 原网页未直接核验</span> : null;
}

const qualityStates = { ok: '已解析', partial: '解析不完整', failed: '未能解析', unparsed: '尚未解析' };
function AttachmentReplacement({ document, close, done }) {
  const app = useApp(), mutation = useMutation(), key = useRef(idempotencyKey());
  const [file, setFile] = useState(null), [basis, setBasis] = useState(''), [result, setResult] = useState(null);
  const oversized = file && file.size > 8 * 1024 * 1024;
  return <Modal title="授权补充附件" close={close}><p>用于补充已获授权的文件。保存后进入人工审核，审核通过前不会改变学生看到的内容。</p><External href={document.source_url}>查看受限原网页</External>{result ? <div className="success-line" role="status"><p>文件已留存，等待审核。公开依据将标注为“授权人工补充”。</p><button className="primary" onClick={() => { done(); close(); }}>返回管理工作台</button></div> : <form onSubmit={(e) => { e.preventDefault(); mutation.run(() => api.replaceAttachment(document.snapshot_id, file, basis.trim(), key.current), (saved) => { setResult(saved); app.updated(); }); }}><label>已获授权的附件<input type="file" required accept=".pdf,.docx,.xlsx,.txt" onChange={(e) => { setFile(e.target.files?.[0] || null); key.current = idempotencyKey(); mutation.clear(); }} /></label><p className={oversized ? 'warning-line' : 'small muted'}>{oversized ? '文件超过 8 MiB，请重新选择。' : '支持 PDF、DOCX、XLSX、UTF-8 TXT，最多 8 MiB。'}</p><label>授权提供依据<textarea rows={4} required maxLength={2000} value={basis} onChange={(e) => { setBasis(e.target.value); key.current = idempotencyKey(); }} placeholder="说明文件如何取得，以及授权提供的依据" /></label><ErrorBox error={mutation.error} /><button className="primary" disabled={mutation.busy || !file || oversized || !basis.trim()}>{mutation.busy ? '保存中…' : '保存并进入审核'}</button></form>}</Modal>;
}
function DocumentQuality({ quality, version, reload }) {
  const app = useApp(), [replacement, setReplacement] = useState(null);
  const current = quality.data?.version === version ? quality.data : null;
  const incomplete = current?.documents.filter((doc) => doc.state !== 'ok') || [];
  const operator = ['operator', 'admin'].includes(app.session?.user.role);
  return <section className={'document-quality ' + (current?.complete ? 'quality-complete' : '')} aria-label="原文与附件完整程度"><ErrorBox error={quality.error} retry={reload} />{quality.loading || !current && !quality.error ? <p className="small muted">正在核对当前版本的原文与附件…</p> : current && (current.complete ? <p className="small muted">当前版本的正文与附件均已解析。办理要求仍以原文为准。</p> : <><strong>材料可能不完整，请核对原文</strong><p>部分{incomplete.some((doc) => doc.is_attachment) ? '附件' : '原文'}未能完整读取，行动清单可能缺少要求。</p>{incomplete.map((doc) => <div className="quality-document" key={doc.snapshot_id}><span>{doc.is_attachment ? '附件' : '正文'} · {doc.warnings.includes('attachment_access_restricted') ? '原网页需要验证码或授权' : qualityStates[doc.state]}</span><External href={doc.source_url}>打开原网页核对</External>{operator && doc.is_attachment && <button className="text-button" onClick={() => setReplacement(doc)}>授权补充附件</button>}</div>)}</>)}{replacement && <AttachmentReplacement document={replacement} close={() => setReplacement(null)} done={() => { reload(); app.updated(); app.openEvent(null); app.navigate('admin'); }} />}</section>;
}
function CandidateAttachments({ candidate, refresh }) {
  const [expanded, setExpanded] = useState(false), [replacement, setReplacement] = useState(null);
  const documents = useResource(async () => {
    const rows = await Promise.all(candidate.snapshot_ids.map(async (id) => {
      const { snapshot } = await api.adminSnapshot(id);
      if (!snapshot.parent_snapshot_id) return null;
      let parsed;
      try { parsed = await api.adminParsed(id); } catch (error) { if (error.status !== 404) throw error; }
      return { ...snapshot, state: parsed?.state || 'unparsed', warnings: parsed?.warnings || [] };
    }));
    return rows.filter(Boolean);
  }, [candidate.candidate_id, candidate.revision], expanded);
  return <div className="candidate-attachments"><button className="text-button" onClick={() => setExpanded((value) => !value)}>{expanded ? '收起附件完整程度' : '核对附件完整程度'}</button>{expanded && <><ErrorBox error={documents.error} retry={documents.reload} />{documents.loading && <Loading />}{documents.data?.map((doc) => <div className="quality-document" key={doc.snapshot_id}><span>{doc.title} · {qualityStates[doc.state]}</span><External href={doc.source_url}>原网页</External>{doc.state !== 'ok' && <button className="text-button" onClick={() => setReplacement(doc)}>授权补充附件</button>}</div>)}{documents.data?.length === 0 && <p className="small muted">该候选没有留存附件。</p>}</>}{replacement && <AttachmentReplacement document={replacement} close={() => setReplacement(null)} done={refresh} />}</div>;
}

// Intl formatters are expensive to construct; build once instead of per card.
const stampFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
const todayFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: 'long', day: 'numeric', weekday: 'long' });
function formatStamp(value) {
  if (!value) return '未记录';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : stampFormat.format(date);
}
function deadlineText(deadline) {
  if (!deadline) return '截止时间待确认';
  if (deadline.precision === 'date') return deadline.date + '（未注明时刻）';
  if (deadline.precision === 'minute') return formatStamp(deadline.at);
  return (deadline.raw_text || '截止时间') + ' · 待确认';
}
function itemDeadline(item) { return item.draft.deadline_at ? formatStamp(item.draft.deadline_at) : item.draft.deadline_date ? item.draft.deadline_date + '（未注明时刻）' : '尚未设置截止时间'; }
function cardDeadlineText(card) {
  // Only the dates a student can still act on; if every date has closed the card keeps them all
  // and shows its 已截止 tag next to them. The API's deadline_label concatenates every date,
  // which is how a closed September window stayed on the first card next to a December exam.
  const dates = cardDeadlineList(card.event);
  return dates.length ? dates.map(deadlineText).join('；') : deadlineText(null);
}
function orderedCards(cards) {
  // The server already orders /me/feed by the next deadline that still matters (events.py
  // sort_key); the public /events list is ordered by updated_at, so the same rule is applied
  // here for both. Ordering by the earliest date a notice mentions is what put a closed
  // September 报名 window ahead of its own December exam date on the first card.
  return cards.map((card) => ({ card, key: nextDeadlineKey(card.event) }))
    .sort((a, b) => a.key.localeCompare(b.key))
    .map((row) => row.card);
}
function useResource(loader, deps = [], enabled = true) {
  const loaderRef = useRef(loader); loaderRef.current = loader;
  const [state, setState] = useState({ data: null, loading: enabled, error: null });
  const [sequence, setSequence] = useState(0);
  useEffect(() => {
    let active = true;
    if (!enabled) { setState({ data: null, loading: false, error: null }); return; }
    setState((old) => ({ ...old, loading: true, error: null }));
    loaderRef.current().then((data) => active && setState({ data, loading: false, error: null })).catch((error) => active && setState((old) => ({ ...old, loading: false, error })));
    return () => { active = false; };
  }, [...deps, enabled, sequence]);
  return { ...state, update: (data) => setState({ data, loading: false, error: null }), reload: () => setSequence((n) => n + 1) };
}
function usePages(loader, deps = [], enabled = true, preservePages = false) {
  const loaderRef = useRef(loader); loaderRef.current = loader;
  const generation = useRef(0), pending = useRef(false), pagesLoaded = useRef(1);
  const [state, setState] = useState({ items: [], cursor: null, loading: enabled, error: null });
  const [sequence, setSequence] = useState(0);
  useEffect(() => {
    const current = ++generation.current; pending.current = false;
    if (!enabled) { pagesLoaded.current = 1; setState({ items: [], cursor: null, loading: false, error: null }); return; }
    setState({ items: [], cursor: null, loading: true, error: null }); pending.current = true;
    const count = preservePages ? pagesLoaded.current : 1;
    (async () => {
      let cursor = null, items = [], loaded = 0;
      do {
        const data = await loaderRef.current(cursor);
        if (current !== generation.current) return;
        items.push(...data.items); cursor = data.next_cursor; loaded += 1;
      } while (cursor && loaded < count);
      pagesLoaded.current = loaded;
      setState({ items, cursor, loading: false, error: null });
    })().catch((error) => current === generation.current && setState({ items: [], cursor: null, loading: false, error })).finally(() => { if (current === generation.current) pending.current = false; });
    return () => { generation.current += 1; };
  }, [...deps, enabled, sequence]);
  async function more() {
    if (!state.cursor || pending.current) return;
    const current = generation.current; pending.current = true;
    setState((old) => ({ ...old, loading: true, error: null }));
    try {
      const data = await loaderRef.current(state.cursor);
      if (current === generation.current) { pagesLoaded.current += 1; setState((old) => ({ items: [...old.items, ...data.items], cursor: data.next_cursor, loading: false, error: null })); }
    } catch (error) { if (current === generation.current) setState((old) => ({ ...old, loading: false, error })); }
    finally { if (current === generation.current) pending.current = false; }
  }
  return { ...state, more, reload: () => setSequence((n) => n + 1) };
}
function useMutation() {
  const [busy, setBusy] = useState(false), [error, setError] = useState(null);
  async function run(operation, success, conflict) {
    if (busy) return false;
    setBusy(true); setError(null);
    try { const data = await operation(); success?.(data); return true; }
    catch (err) { setError(err); if (err.status === 409) conflict?.(); return false; }
    finally { setBusy(false); }
  }
  return { busy, error, run, clear: () => setError(null) };
}
function ErrorBox({ error, retry }) {
  const app = useApp();
  if (!error) return null;
  return <div className="error-box" role="alert"><strong>{error.status === 409 && error.code !== 'SOURCE_NOT_READY' ? '内容已被更新，请重新核对后操作。' : error.message || '请求失败'}</strong>{error.requestId && <small>问题编号：{error.requestId}</small>}<div className="inline-actions">{retry && <button className="text-button" onClick={retry}>重新载入</button>}{error.status === 401 && app && <button className="text-button" onClick={app.ensureLogin}>重新登录</button>}</div></div>;
}
function CardSkeleton({ count = 4 }) { return <div className="cards skeleton-cards" aria-hidden="true">{Array.from({ length: count }, (_, i) => <div className="skeleton-card" key={i}><span className="sk sk-tag" /><span className="sk sk-title" /><span className="sk sk-line" /><span className="sk sk-line short" /></div>)}</div>; }
function Loading({ label = '正在读取最新信息…' }) { return <div className="loading" role="status"><span className="spinner" />{label}</div>; }
function Empty({ icon: Icon = Inbox, title, children, action }) { return <div className="empty"><Icon size={34} strokeWidth={1.3} /><h2>{title}</h2><p>{children}</p>{action}</div>; }
function More({ page }) { return page.cursor ? <button type="button" className="load-more" disabled={page.loading} onClick={page.more}>{page.loading ? '加载中…' : '加载更多'}</button> : null; }
function External({ href, children, className = '' }) {
  const url = safeURL(href);
  return url ? <a className={className} href={url} target="_blank" rel="noopener noreferrer">{children}<ArrowUpRight size={13} aria-hidden="true" /></a> : <span className="muted">{children}（链接不可用）</span>;
}
function Heading({ eyebrow, title, children, action }) { return <header className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1>{children && <p>{children}</p>}</div>{action}</header>; }
function Modal({ title, children, close, wide = false }) {
  const ref = useRef(null);
  useEffect(() => {
    const previous = document.activeElement;
    const layer = ref.current.closest('.modal-backdrop');
    modalLayers.push(layer); syncModalAccessibility();
    (ref.current?.querySelector('input:not(:disabled), textarea:not(:disabled), select:not(:disabled), button:not(:disabled)') || ref.current)?.focus();
    function key(event) {
      const layers = document.querySelectorAll('.modal');
      if (layers[layers.length - 1] !== ref.current) return;
      if (event.key === 'Escape') close();
      if (event.key === 'Tab') {
        const elements = Array.from(ref.current?.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]') || []).filter((element) => element.getClientRects().length > 0);
        if (!elements?.length) return;
        const first = elements[0], last = elements[elements.length - 1];
        if (event.shiftKey && (document.activeElement === first || !elements.includes(document.activeElement))) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || !elements.includes(document.activeElement))) { event.preventDefault(); first.focus(); }
      }
    }
    document.addEventListener('keydown', key);
    const unlockScroll = lockBodyScroll();
    return () => {
      document.removeEventListener('keydown', key); unlockScroll();
      const index = modalLayers.indexOf(layer);
      if (index !== -1) modalLayers.splice(index, 1);
      syncModalAccessibility();
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  return createPortal(<div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && close()}><section className={'modal ' + (wide ? 'wide' : '')} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} ref={ref}><div className="modal-head"><h2>{title}</h2><button className="icon-button" aria-label="关闭" onClick={close}><X size={20} /></button></div><div className="modal-body">{children}</div></section></div>, document.body);
}
function EventCard({ card, compact = false }) {
  const app = useApp(), mutation = useMutation();
  const [localState, setLocalState] = useState(card.state);
  useEffect(() => setLocalState(card.state), [card.state]);
  const event = card.event, personal = localState;
  const classification = { required: '需要行动', optional: '可选机会', informational: '了解即可', needs_review: '待核实' }[card.classification];
  const urgency = personal?.state === 'done' ? null : deadlineUrgency(event);
  async function change(patch) {
    if (!app.session) { app.ensureLogin(); return; }
    await mutation.run(async () => {
      const latest = card.state === null ? (await api.state(event.id)).state : personal;
      return api.setState(event.id, { expected_state_revision: latest?.state_revision || 0, acknowledged_event_version: event.version, ...patch });
    }, (value) => { setLocalState(value); app.updated(); app.notify(patch.saved !== undefined ? (value.saved ? '已收藏，可在我的事项中查看。' : '已取消收藏。') : patch.state ? '已设为' + states[value.state] + '。' : '已标记已读。'); }, app.updated);
  }
  return <article className={'event-card ' + (personal?.state === 'done' ? 'is-done ' : '') + (card.classification === 'required' ? 'important' : '')}>
    <div className="card-top"><span className={'tag ' + card.classification}>{classification}</span>{eventHasEnded(event) && <span className="tag warning">已截止</span>}{urgency !== null && <span className={'tag urgent' + (urgency <= 0 ? ' today' : '')}>{urgencyLabel(urgency)}</span>}<span className="muted small">{types[event.event_type]} · v{event.version}</span>{personal?.needs_attention && <span className="tag warning">内容有更新</span>}</div>
    <button className="card-title" onClick={() => app.openEvent(event.id)}>{event.title}<ChevronRight size={18} /></button>
    {event.status !== 'active' && <p className="warning-line">{statuses[event.status]}</p>}
    <div className="deadline"><Clock3 size={14} />{cardDeadlineText(card)}</div>
    {!compact && event.action_steps.length > 0 && <p className="action-preview">{event.action_steps[0]}</p>}
    {!compact && <div className="match-note">{card.match === 'not_matched' ? '当前画像不匹配' : card.match === 'matched' ? '与你相关' : '适用范围需要核对'}{card.match_reasons[0] && ' · ' + card.match_reasons[0]}</div>}
    {card.source_stale && <p className="warning-line">来源更新异常，请核对原文。</p>}
    {app.session && <div className="card-actions"><button disabled={mutation.busy} className={personal?.saved ? 'is-selected' : ''} onClick={() => change({ saved: !personal?.saved })}><Star size={15} fill={personal?.saved ? 'currentColor' : 'none'} />{personal?.saved ? '已收藏' : '收藏'}</button><button disabled={mutation.busy || personal?.read && !personal?.needs_attention} onClick={() => change({ read: true })}>{personal?.read ? <Check size={15} /> : <BookOpen size={15} />}{personal?.read ? '已读' : '标记已读'}</button><button disabled={mutation.busy} onClick={() => change({ state: personal?.state === 'done' ? 'todo' : 'done', read: true })}><CircleCheck size={15} />{personal?.state === 'done' ? '重新待办' : '完成'}</button></div>}
    <ErrorBox error={mutation.error} />
  </article>;
}
function PersonalCard({ item }) {
  const app = useApp(), mutation = useMutation();
  const [remove, setRemove] = useState(false);
  const urgency = personalUrgency(item);
  return <article className={'personal-card ' + (item.state === 'done' ? 'is-done' : '')}><div className="card-top"><span className="tag private">仅自己可见</span>{urgency !== null && <span className={'tag urgent' + (urgency <= 0 ? ' today' : '')}>{urgencyLabel(urgency)}</span>}<span className="small muted">{states[item.state]}</span></div><h3>{item.draft.title}</h3><div className="deadline"><Clock3 size={14} />{itemDeadline(item)}</div>{item.draft.action_steps[0] && <p className="action-preview">{item.draft.action_steps[0]}</p>}<div className="card-actions"><button disabled={mutation.busy} onClick={() => mutation.run(() => api.patchItem(item.id, { expected_revision: item.revision, state: item.state === 'done' ? 'todo' : 'done' }), () => { app.updated(); app.notify(item.state === 'done' ? '已重新加入待办。' : '个人事项已完成。'); }, app.updated)}><CircleCheck size={15} />{item.state === 'done' ? '重新待办' : '完成'}</button><button onClick={() => app.openImport(item)}>编辑</button><button className="danger-text" onClick={() => setRemove(true)}>删除</button></div>{remove && <div className="confirm-inline"><p>删除该事项及其后续提醒？</p><button className="danger" disabled={mutation.busy} onClick={() => mutation.run(() => api.deleteItem(item.id), () => { setRemove(false); app.updated(); app.notify('个人事项及后续提醒已删除。'); })}>确认删除</button><button onClick={() => setRemove(false)}>保留</button></div>}<ErrorBox error={mutation.error} /></article>;
}
function Login({ close, onLogin }) {
  const [email, setEmail] = useState(''), [code, setCode] = useState(''), [challenge, setChallenge] = useState(null), [devCode, setDevCode] = useState(null);
  const mutation = useMutation();
  async function submit(event) {
    event.preventDefault();
    if (!challenge) await mutation.run(() => api.challenge(email.trim()), (result) => {
      setChallenge(result.data);
      const dev = result.headers.get('X-Dev-Login-Code');
      setDevCode(dev && localHosts.includes(location.hostname) ? dev : null);
    });
    else await mutation.run(() => api.login(challenge.challenge_id, code), (session) => { setSession(session); onLogin(session); close(); });
  }
  return <Modal title="登录偷闲 · 天大版" close={close}><p className="muted">使用邮箱验证码登录，保存自己的画像、待办和提醒偏好。</p><form onSubmit={submit}><label>邮箱<input type="email" autoComplete="email" required value={email} disabled={Boolean(challenge)} onChange={(e) => setEmail(e.target.value)} placeholder="你的邮箱" /></label>{challenge && <><label>六位验证码<input inputMode="numeric" pattern="[0-9]{6}" maxLength={6} autoComplete="one-time-code" autoFocus required value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} /></label><p className="small muted">验证码有效至 {formatStamp(challenge.expires_at)}。</p>{devCode && <div className="dev-note">本机显式开发认证 · 验证码 <strong>{devCode}</strong>。此模式不会真实发送邮件。</div>}</>}<ErrorBox error={mutation.error} /><div className="form-actions"><button className="primary" disabled={mutation.busy}>{mutation.busy ? '处理中…' : challenge ? '登录' : '获取验证码'}</button>{challenge && <button type="button" onClick={() => { setChallenge(null); setCode(''); setDevCode(null); mutation.clear(); }}>重新获取</button>}</div></form></Modal>;
}
function EventDetail({ id, close }) {
  const app = useApp(), mutation = useMutation();
  const data = useResource(async () => {
    const event = await api.event(id);
    const result = app.session ? await api.state(id) : { state: null };
    return { event, state: result.state };
  }, [id, app.session?.user.id, app.refresh]);
  const currentVersion = data.data?.event.version;
  const versions = usePages((cursor) => api.versions(id, cursor), [id, currentVersion], Boolean(currentVersion));
  const changeResource = useResource(() => api.changes(id), [id, currentVersion], Boolean(currentVersion));
  const changes = { ...changeResource, data: changeResource.data?.version === currentVersion ? changeResource.data : null };
  const quality = useResource(() => api.quality(id), [id, currentVersion], Boolean(currentVersion));
  useEffect(() => {
    const changed = !quality.loading && quality.data && quality.data.version !== currentVersion
      || !changeResource.loading && changeResource.data && changeResource.data.version !== currentVersion;
    if (changed || quality.error?.status === 409 || changeResource.error?.status === 409) data.reload();
  }, [currentVersion, quality.data?.version, quality.loading, quality.error, changeResource.data?.version, changeResource.loading, changeResource.error]);
  function reloadQuality() { data.reload(); quality.reload(); }
  function reloadChanges() { data.reload(); changeResource.reload(); }
  const [tab, setTab] = useState('detail'), [snapshot, setSnapshot] = useState(null);
  async function change(patch) {
    if (!app.session) return app.ensureLogin();
    await mutation.run(() => api.setState(id, { expected_state_revision: data.data.state?.state_revision || 0, acknowledged_event_version: data.data.event.version, ...patch }), (state) => { data.update({ ...data.data, state }); app.updated(); }, () => { data.reload(); app.updated(); });
  }
  async function openSnapshot(evidence) {
    await mutation.run(() => api.snapshot(evidence.snapshot_id), (value) => setSnapshot(value));
  }
  const event = data.data?.event, personal = data.data?.state;
  return <Modal title="事项详情" close={close} wide>{data.loading && <Loading />}<ErrorBox error={data.error} retry={data.reload} />{event && <><div className="detail-title"><div className="card-top"><span className="tag">{types[event.event_type]}</span><span className="small muted">版本 {event.version} · {statuses[event.status]}</span>{personal?.needs_attention && <span className="tag warning">需重新核对</span>}</div><h2>{event.title}</h2><p className="small muted">更新于 {formatStamp(event.updated_at)}</p></div>{app.session && <div className="inline-actions"><button disabled={mutation.busy} onClick={() => change({ saved: !personal?.saved })}><Star size={15} />{personal?.saved ? '取消收藏' : '收藏与提醒'}</button><button disabled={mutation.busy} onClick={() => change({ read: true })}>标记已读</button><select aria-label="事项状态" disabled={mutation.busy} value={personal?.state || 'todo'} onChange={(e) => change({ state: e.target.value, read: true })}>{Object.entries(states).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>{personal?.needs_attention && <button disabled={mutation.busy} onClick={() => change({ read: true })}>已核对最新版本</button>}</div>}<ErrorBox error={mutation.error} /><DocumentQuality quality={quality} version={event.version} reload={reloadQuality} />{changes.data?.changes?.length ? <div className="change-banner" role="status"><span className="tag warning">内容有更新</span><span className="small">最近已公开变化：{changes.data.changes[changes.data.changes.length - 1].change.summary}</span><button className="text-button" onClick={() => setTab('changes')}>查看变化记录</button></div> : null}<div className="tabs"><button className={tab === 'detail' ? 'active' : ''} onClick={() => setTab('detail')}>行动清单</button><button className={tab === 'evidence' ? 'active' : ''} onClick={() => setTab('evidence')}>原文依据 ({event.evidence.length})</button><button className={tab === 'changes' ? 'active' : ''} onClick={() => setTab('changes')}>变化记录{changes.data?.changes?.length ? ' (' + changes.data.changes.length + ')' : ''}</button><button className={tab === 'versions' ? 'active' : ''} onClick={() => setTab('versions')}>版本记录</button></div>{tab === 'detail' && <><section className="detail-section"><h3>什么时候</h3>{event.deadlines.length ? event.deadlines.map((d, i) => <div className="deadline-row" key={i}><strong>{deadlineText(d)}</strong><small>{d.kind === 'school_deadline' ? '学校截止' : d.kind === 'college_deadline' ? '学院截止' : d.kind === 'event_time' ? '活动时间' : '其他时间'} · 原文：{d.raw_text}</small></div>) : <p className="muted">原文没有明确截止时间。</p>}</section><section className="detail-section"><h3>需要做什么</h3>{event.action_steps.length ? <ol>{event.action_steps.map((step, i) => <li key={i}>{step}</li>)}</ol> : <p className="muted">没有明确行动步骤，请核对原文。</p>}</section><section className="detail-section"><h3>准备哪些材料</h3>{event.materials.length ? <ul>{event.materials.map((material, i) => <li key={i}>{material}</li>)}</ul> : <p className="muted">原文未明确列出材料。</p>}</section><section className="detail-section"><h3>适用范围</h3><p>{event.audience.conditions_text || '适用条件不明确，请查看原文。'}</p><p className="small muted">识别程度：{event.audience.certainty === 'explicit' ? '原文明确说明' : event.audience.certainty === 'partial' ? '条件不完整' : '尚未确定'}</p></section><section className="detail-section"><h3>办理入口</h3>{event.entry_urls.length ? event.entry_urls.map((url, i) => <External key={url} href={url} className="entry-link">打开入口 {i + 1}</External>) : <p className="muted">未找到办理链接。</p>}</section></>}{tab === 'evidence' && <>{event.evidence.map((e) => <blockquote className="evidence" key={e.id}><p>{e.quote}</p><footer><CaptureNotice capture={e.capture} /><External href={e.source_url}>查看原网页</External>{e.page && <span>第 {e.page} 页</span>}<button className="text-button" disabled={mutation.busy} onClick={() => openSnapshot(e)}>打开留存快照</button></footer></blockquote>)}{snapshot && <div className="panel"><h3>{snapshot.snapshot.title}</h3><CaptureNotice capture={snapshot.snapshot.capture} /><p className="small muted">留存于 {formatStamp(snapshot.snapshot.fetched_at)} · 链接有效至 {formatStamp(snapshot.expires_at)}</p><External href={snapshotURL(snapshot.signed_url, location.origin)} className="entry-link">查看快照文件</External><p className="small muted">原网页可能已有变化；此快照对应当前引用依据。</p></div>}</>}{tab === 'changes' && <><ErrorBox error={changes.error} retry={reloadChanges} />{changes.loading && <Loading />}<p className="small muted">下面按时间显示已公开版本采用的原文变化，由程序逐段比对；未审核采集不会出现在这里。措辞或时间的细微差别仍请以原文为准。</p>{changes.data?.changes?.length ? changes.data.changes.map((row) => <div className="version-row" key={row.snapshot_id}><strong>{row.change.summary}</strong><span>{formatStamp(row.observed_at)}</span><p>{changeCountsText(row.change.counts)}{row.change.kinds.length ? ' · ' + row.change.kinds.map((kind) => changeKinds[kind] || kind).join('、') : ''}</p>{row.change.kinds.includes('deadline_or_date_touched') && <small>时间或期限可能有变化，请重新核对原文。</small>}{row.change.truncated && <small className="muted">变化较多，此处只显示摘要。</small>}</div>) : <p className="muted">目前没有已公开的原文变化记录。</p>}</>}{tab === 'versions' && <><ErrorBox error={versions.error} retry={versions.reload} />{versions.loading && <Loading />}{versions.items.map((version) => <div className="version-row" key={version.version}><strong>v{version.version} · {statuses[version.status]}</strong><span>{formatStamp(version.updated_at)}</span><p>{version.changed_fields.length ? '变更：' + version.changed_fields.join('、') : '首次发布'}</p>{version.deadlines.map((d, i) => <small key={i}>{deadlineText(d)} </small>)}</div>)}<More page={versions} /></>}</>}</Modal>;
}

function Today() {
  const app = useApp();
  const feed = usePages(api.feed, [app.refresh], Boolean(app.session), true);
  const items = usePages(api.items, [app.refresh], Boolean(app.session), true);
  // A freshly started instance has notices but nothing classified "required" yet (the review gate
  // only accepts what is still actionable, and everything else stays 留给人). Defaulting to
  // 需要我行动 turned that into "当前没有需要处理的信息" on the first screen. The default view
  // therefore gives the library, and 需要我行动 stays one tap away right next to it.
  const [filter, setFilter] = useState('all');
  const day = campusDate();
  const { active, past, visible, actionCount, reviewCount } = useMemo(() => {
    const unfinished = orderedCards(feed.items.filter((card) => !['done', 'archived'].includes(card.state?.state) || card.state?.needs_attention));
    const active = unfinished.filter((card) => !eventHasEnded(card.event));
    const past = unfinished.filter((card) => eventHasEnded(card.event));
    // 需要我行动 is a subset of the library, never a stand-in for it: the count drives the chip
    // badge so an urgent item cannot hide behind the wider default view.
    const needsAction = (card) => card.classification === 'required' || card.state?.saved || card.state?.needs_attention;
    const visible = active.filter((card) => filter === 'all' || filter === 'action' && needsAction(card) || filter === 'review' && card.classification === 'needs_review');
    return { active, past, visible, actionCount: active.filter(needsAction).length, reviewCount: active.filter((card) => card.classification === 'needs_review').length };
  }, [feed.items, filter, day]);
  const { privateItems, pastItems } = useMemo(() => {
    const unfinishedItems = items.items.filter((item) => !['done', 'archived'].includes(item.state));
    return { privateItems: unfinishedItems.filter((item) => !personalHasEnded(item)), pastItems: unfinishedItems.filter((item) => personalHasEnded(item)) };
  }, [items.items, day]);
  // Three different situations must not read the same: nothing in the library at all, a library
  // whose notices have all closed, and a filter that simply matches nothing right now.
  const emptyState = !feed.loading && !feed.error && !visible.length
    ? (!active.length && !past.length
      ? <Empty icon={Inbox} title="还没有可看的公开通知" action={<button className="primary" onClick={() => app.navigate('sources')}>查看信息来源</button>}>来源默认停用，需要运营者启用采集后才会入库；入库后这里会按你的画像显示。</Empty>
      : !active.length
        ? <Empty icon={Sparkles} title="清单里的通知都已截止">库里有 {past.length} 条已截止或取消的通知，展开下面的「已截止或取消」可以核对历史与变更。</Empty>
        : <Empty icon={Sparkles} title={filter === 'action' ? '当前没有需要你行动的事项' : '当前没有待核实的信息'} action={<button className="primary" onClick={() => setFilter('all')}>看全部相关信息</button>}>{filter === 'action' ? '这是「需要我行动」筛选的结果，不是库空了：' : ''}库里还有 {active.length} 条未截止通知，切到「全部相关信息」就能看到。</Empty>)
    : null;
  const date = todayFormat.format(new Date());
  return <><Heading eyebrow={date} title="把今天留给真正重要的事" action={app.session && <button className="primary" onClick={() => app.openImport()}><Plus size={17} />添加个人事项</button>}>先看要做的事，随时回到原文核对。</Heading>{!app.session ? <><div className="welcome"><div><span className="eyebrow">信息少翻几页，事情少漏一件</span><h2>你的校园行动清单</h2><p>汇集公开通知、活动与办理入口，提取截止时间和材料。登录后按学院、年级和兴趣筛选，把群通知整理成自己的待办。</p><button className="primary" onClick={app.ensureLogin}>登录并设置我的画像<ChevronRight size={17} /></button></div><div className="welcome-graphic" role="group" aria-label="从通知到行动的三个步骤"><div className="workflow-step"><span className="workflow-icon"><Inbox size={20} /></span><div><strong>信息汇集到一处</strong><span>官网通知 · 公众号供稿 · 个人导入</span></div><span className="workflow-number">01</span></div><div className="workflow-step"><span className="workflow-icon"><FileSearch size={20} /></span><div><strong>先读重点，再看依据</strong><span>适用人群、所需材料和截止时间</span></div><span className="workflow-number">02</span></div><div className="workflow-step"><span className="workflow-icon"><CircleCheck size={20} /></span><div><strong>把通知变成下一步</strong><span>收藏、待办，直达原文办理入口</span></div><span className="workflow-number">03</span></div></div></div><PublicEvents title="最近公开事项" /></> : <><div className="stats"><div><span>当前页需要关注</span><strong>{active.filter((c) => c.classification === 'required').length}<small> 项</small></strong></div><div><span>当前个人待办</span><strong>{privateItems.length}<small> 项</small></strong></div><div><span>待核实信息</span><strong>{active.filter((c) => c.classification === 'needs_review').length}<small> 项</small></strong></div></div><div className="section-title"><h2>我的信息清单</h2><button className="text-button" onClick={() => { feed.reload(); items.reload(); }}><RefreshCw size={14} />刷新</button></div><div className="filter-row">{[['action', '需要我行动', actionCount], ['all', '全部相关信息', active.length], ['review', '待核实', reviewCount]].map(([value, label, count]) => <button key={value} className={(filter === value ? 'selected' : '') + (value === 'action' && actionCount ? ' needs-action' : '')} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}{count ? <span className="chip-count" aria-hidden="true">{count}</span> : null}</button>)}</div><ErrorBox error={feed.error} retry={feed.reload} />{feed.loading && !feed.items.length && <><Loading /><CardSkeleton /></>}{emptyState}<div className="cards">{visible.map((card) => <EventCard key={card.event.id} card={card} />)}</div><More page={feed} />{past.length > 0 && <details className="past-items"><summary>已截止或取消（{past.length} 项）<span>展开核对历史与变更</span></summary><div className="cards">{past.map((card) => <EventCard key={card.event.id} card={card} />)}</div></details>}<div className="section-title"><h2>个人待办</h2><span className="muted small">从群通知或自己的计划整理</span></div><ErrorBox error={items.error} retry={items.reload} />{items.loading && !items.items.length && <Loading />}<div className="cards">{privateItems.map((item) => <PersonalCard key={item.id} item={item} />)}</div>{!items.loading && !items.error && !privateItems.length && <div className="add-empty"><Plus size={18} /><span>把一条群通知变成待办</span><button onClick={() => app.openImport()}>本地整理</button></div>}<More page={items} />{pastItems.length > 0 && <details className="past-items"><summary>已过期个人待办（{pastItems.length} 项）<span>仍未完成，可继续处理</span></summary><div className="cards">{pastItems.map((item) => <PersonalCard key={item.id} item={item} />)}</div></details>}</>}</>;
}
function cardFromEvent(event) {
  return { event, state: null, classification: event.action_required === true ? 'required' : event.action_required === false ? 'informational' : 'needs_review', match: 'unknown', match_reasons: [], deadline_label: null, source_stale: false };
}
function PublicEvents({ title, opportunity = false }) {
  const app = useApp(), page = usePages(async (cursor) => {
    if (app.session) return api.feed(cursor);
    const value = await api.events(cursor);
    return { items: value.items.map(cardFromEvent), next_cursor: value.next_cursor };
  }, [app.refresh, app.session?.user.id], true, true);
  const [filter, setFilter] = useState('all');
  const day = campusDate();
  const { visible, past } = useMemo(() => {
    const filtered = page.items.filter((card) => (!opportunity || ['scholarship', 'competition', 'activity', 'employment'].includes(card.event.event_type)) && (filter === 'all' || card.event.event_type === filter));
    return { visible: orderedCards(filtered.filter((card) => !eventHasEnded(card.event))), past: orderedCards(filtered.filter((card) => eventHasEnded(card.event))) };
  }, [page.items, opportunity, filter, day]);
  const availableTypes = opportunity ? ['scholarship', 'competition', 'activity', 'employment'] : Object.keys(types);
  return <>{title && <div className="section-title"><h2>{title}</h2></div>}<div className="filter-row"><button className={filter === 'all' ? 'selected' : ''} aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>全部</button>{availableTypes.map((type) => <button key={type} className={filter === type ? 'selected' : ''} aria-pressed={filter === type} onClick={() => setFilter(type)}>{types[type]}</button>)}</div><ErrorBox error={page.error} retry={page.reload} />{page.loading && !page.items.length && <><Loading /><CardSkeleton /></>}<div className="cards">{visible.map((card) => <EventCard key={card.event.id} card={card} />)}</div>{!page.loading && !page.error && !visible.length && <Empty icon={Sparkles} title="当前没有这类信息">来源尚未发布相关事项，或仍在人工审核中。</Empty>}<More page={page} />{past.length > 0 && <details className="past-items"><summary>历史通知（{past.length} 项）<span>已截止或取消，展开查看</span></summary><div className="cards">{past.map((card) => <EventCard key={card.event.id} card={card} />)}</div></details>}</>;
}
function Opportunities() { return <><Heading eyebrow="探索校园" title="值得留意的机会">奖助、竞赛、活动与就业。适用条件请以原文为准。</Heading><PublicEvents opportunity /></>; }
function SearchPage() {
  const app = useApp();
  const [mode, setMode] = useState('search'), [text, setText] = useState(''), [college, setCollege] = useState(''), [historical, setHistorical] = useState(false), [submitted, setSubmitted] = useState(null), [index, setIndex] = useState(null);
  const hits = usePages(async (cursor) => {
    const response = await api.search(submitted?.text, { college_id: submitted?.college, include_historical: submitted?.historical, cursor });
    setIndex(response.index_status); return { items: response.hits, next_cursor: response.next_cursor };
  }, [submitted], submitted?.mode === 'search');
  const answer = useResource(() => api.answer({ question: submitted.text, education_level: null, college_id: submitted.college || null, include_historical: submitted.historical }), [submitted], submitted?.mode === 'answer');
  function submit(event) {
    event.preventDefault();
    if (!text.trim()) return;
    if (mode === 'answer' && !app.session) return app.ensureLogin();
    setSubmitted({ text: text.trim(), college, historical, mode });
  }
  // Show which characters of a hit came from the query, so why a result ranked
  // is checkable at a glance instead of something the reader takes on faith.
  const marked = (value) => highlightRuns(value, submitted?.text).map((run, position) =>
    run.match ? <mark key={position}>{run.text}</mark> : run.text);
  return <><Heading eyebrow="少找几次，多一点依据" title="找信息">搜索已发布公开原文，或按问题整理带出处的回答。</Heading><section className="search-panel"><div className="tabs"><button className={mode === 'search' ? 'active' : ''} onClick={() => setMode('search')}><Search size={16} />搜原文</button><button className={mode === 'answer' ? 'active' : ''} onClick={() => setMode('answer')}><FileSearch size={16} />问问题</button></div><form onSubmit={submit}><div className="search-input"><Search size={20} /><input aria-label={mode === 'search' ? '搜索关键词' : '你的问题'} value={text} maxLength={mode === 'search' ? 500 : 2000} required onChange={(e) => setText(e.target.value)} placeholder={mode === 'search' ? '例如：奖学金申请材料、选课、竞赛报名' : '例如：这个奖学金什么时候截止，需要什么材料？'} /><button className="primary" disabled={hits.loading || answer.loading}>{mode === 'search' ? '搜索' : '整理依据'}</button></div><div className="inline-actions"><select aria-label="学院范围" value={college} onChange={(e) => setCollege(e.target.value)}><option value="">所有学院</option>{app.catalog?.colleges.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select><label className="check"><input type="checkbox" checked={historical} onChange={(e) => setHistorical(e.target.checked)} />包含历史版本</label></div></form></section>{!submitted && <Empty icon={FileSearch} title="从一个关键词或问题开始">回答会展示原文引用；证据不足时会明确说明。</Empty>}{submitted?.mode === 'search' && <><div className="section-title"><h2>“{submitted.text}”的结果</h2><span className="small muted">{index === 'lagging' ? '索引更新中' : index === 'unavailable' ? '索引暂不可用' : '公开信息'}</span></div><ErrorBox error={hits.error} retry={hits.reload} />{hits.loading && <Loading />}{hits.items.map((hit, i) => <article className="search-result" key={hit.evidence_id + ':' + i}><div className="card-top">{hit.historical && <span className="tag warning">历史版本</span>}{hit.ended && <span className="tag warning">已截止</span>}<span className="small muted">v{hit.event_version}</span></div><button className="card-title" onClick={() => app.openEvent(hit.event_id)}><span>{marked(hit.title)}</span><ChevronRight size={16} /></button><p>{marked(hit.excerpt)}</p><External href={hit.source_url}>查看原文</External></article>)}{!hits.loading && !hits.error && !hits.items.length && <Empty title="没有找到对应信息">尝试换一个关键词；信息也可能尚未接入或发布。</Empty>}<More page={hits} /></>}{submitted?.mode === 'answer' && <><ErrorBox error={answer.error} retry={answer.reload} />{answer.loading && <Loading label="正在检索原文依据…" />}{answer.data && <section className="answer-panel"><div className="card-top"><span className={'tag ' + (answer.data.status === 'grounded' ? 'required' : 'warning')}>{({ grounded: '有原文依据', insufficient_evidence: '证据不足', conflicting_sources: '来源存在冲突', temporarily_unavailable: '暂时无法检索' })[answer.data.status]}</span><span className="small muted">{formatStamp(answer.data.generated_at)}</span></div><p className="answer-text">{answer.data.text}</p>{answer.data.citations.map((citation, i) => <blockquote className="evidence" key={citation.evidence_id + ':' + i}><p>{citation.quote}</p><footer><button className="text-button" onClick={() => app.openEvent(citation.event_id)}>查看事项 · v{citation.event_version}</button><External href={citation.source_url}>查看原文</External>{citation.page && <span>第 {citation.page} 页</span>}</footer></blockquote>)}<p className="small muted">办理前请核对引用的最新通知和入口。</p></section>}</>}</>;
}
function MyItems() {
  const app = useApp(), items = usePages(api.items, [app.refresh], Boolean(app.session), true), feed = usePages(api.feed, [app.refresh], Boolean(app.session), true);
  const [filter, setFilter] = useState('unfinished');
  if (!app.session) return <><Heading eyebrow="自己的进度，自己的节奏" title="我的事项" /><Empty title="登录后保存个人事项" action={<button className="primary" onClick={app.ensureLogin}>登录</button>}>群文字在本地整理，确认后的事项仅自己可见。</Empty></>;
  const selected = (state) => filter === 'all' || filter === 'unfinished' && !['done', 'archived'].includes(state) || filter === state;
  const privateItems = items.items.filter((item) => selected(item.state));
  const saved = feed.items.filter((card) => (card.state?.saved || card.state && card.state.state !== 'todo') && selected(card.state?.state || 'todo'));
  return <><Heading eyebrow="自己的进度，自己的节奏" title="我的事项" action={<button className="primary" onClick={() => app.openImport()}><Plus size={17} />添加事项</button>}>个人事项和收藏的公开事项集中在这里。</Heading><div className="filter-row">{[['unfinished', '未完成'], ['done', '已完成'], ['archived', '已归档'], ['all', '全部']].map(([key, label]) => <button key={key} className={filter === key ? 'selected' : ''} onClick={() => setFilter(key)}>{label}</button>)}</div><ErrorBox error={items.error} retry={items.reload} /><ErrorBox error={feed.error} retry={feed.reload} />{(items.loading || feed.loading) && <Loading />}<div className="section-title"><h2>我的事项清单</h2><span className="small muted">个人待办与收藏的公开事项</span></div><div className="cards">{privateItems.map((item) => <PersonalCard key={item.id} item={item} />)}{saved.map((card) => <EventCard key={card.event.id} card={card} />)}</div>{!items.loading && !feed.loading && !items.error && !feed.error && !privateItems.length && !saved.length && <Empty title="这里还没有事项">可以收藏一条公开通知，或把群里的文字整理成个人待办。</Empty>}<More page={items} /><More page={feed} /></>;
}
function ItemEditor({ item, close }) {
  const app = useApp(), mutation = useMutation();
  const [raw, setRaw] = useState(''), [warnings, setWarnings] = useState([]), [fields, setFields] = useState(item ? fieldsFromDraft(item.draft) : null);
  const [ocr, setOcr] = useState(null), [ocrError, setOcrError] = useState(null);
  const worker = useRef(null), alive = useRef(true), key = useRef(idempotencyKey());
  useEffect(() => () => { alive.current = false; const current = worker.current; worker.current = null; current?.terminate().catch(() => {}); }, []);
  const update = (name, value) => setFields((old) => ({ ...old, [name]: value }));
  function extract() {
    const result = extractLocal(raw); setWarnings(result.warnings); setFields({ ...fieldsFromDraft(result.draft), excerpt: raw.slice(0, 2000) });
  }
  async function recognize(file) {
    if (!file) return;
    setOcrError(null);
    if (!/^image\/(png|jpeg|webp|bmp)$/.test(file.type) || file.size > 8 * 1024 * 1024) { setOcrError(new Error('请选择不超过 8 MB 的 PNG、JPEG、WebP 或 BMP 图片。')); return; }
    setOcr({ progress: 0, status: '检查图片' });
    let activeWorker = null, url = null;
    try {
      url = URL.createObjectURL(file);
      const image = new Image(); image.src = url; await image.decode();
      if (image.naturalWidth * image.naturalHeight > 20000000) throw new Error('图片过大，请裁剪到通知区域后重试。');
      const { createWorker } = await import('tesseract.js');
      activeWorker = await createWorker('chi_sim+eng', 1, { ...(window.CAMPUS_OCR || {}), logger: (m) => { if (alive.current) setOcr({ progress: m.progress || 0, status: m.status }); } });
      worker.current = activeWorker;
      if (!alive.current) return;
      const result = await activeWorker.recognize(file);
      if (!result.data.text.trim()) throw new Error('没有识别到文字，请尝试清晰截图或手动粘贴。');
      if (alive.current) { setRaw(result.data.text.slice(0, 30000)); setFields(null); setWarnings(['图片文字识别可能有误，请核对文字、日期、材料和入口再保存。']); }
    } catch (err) { if (alive.current) setOcrError(new Error('本地识别失败：' + (err.message || '请改为粘贴文字。'))); }
    finally { if (url) URL.revokeObjectURL(url); if (activeWorker && worker.current === activeWorker) { worker.current = null; try { await activeWorker.terminate(); } catch {} } if (alive.current) setOcr(null); }
  }
  async function save(event) {
    event.preventDefault();
    if (!app.session) return app.ensureLogin();
    let draft;
    try { draft = draftFromFields(fields); } catch { mutation.run(() => Promise.reject(new Error('请填写有效的截止日期和时间。'))); return; }
    if (!draft.title || draft.entry_urls.some((url) => !safeURL(url))) { mutation.run(() => Promise.reject(new Error('请填写标题，并检查办理入口是否为完整的 http/https 链接。'))); return; }
    await mutation.run(() => item ? api.patchItem(item.id, { expected_revision: item.revision, draft }) : api.addItem(draft, key.current), () => { app.updated(); app.notify('个人事项已保存。'); close(); }, app.updated);
  }
  return <Modal title={item ? '编辑个人事项' : '把群通知变成个人待办'} close={close} wide><div className="privacy-note"><ShieldCheck size={19} /><div><strong>原文与图片在浏览器内处理</strong><p>点击保存才上传你确认的结构化事项。原文摘录默认不上传。</p></div></div>{!item && <><label>群文字或通知<textarea rows={6} value={raw} maxLength={30000} disabled={Boolean(ocr)} onChange={(e) => setRaw(e.target.value)} placeholder="粘贴需要整理的通知，姓名、手机号等可以先删去。" /></label><div className="inline-actions"><button className="primary" disabled={!raw.trim() || Boolean(ocr)} onClick={extract}><Sparkles size={16} />本地提取，核对后保存</button><button disabled={Boolean(ocr)} onClick={() => { setFields(fieldsFromDraft(emptyDraft)); setWarnings([]); }}>手动填写</button><label className={'upload-button ' + (ocr ? 'disabled' : '')}>选择截图本地识别<input type="file" accept="image/png,image/jpeg,image/webp,image/bmp" disabled={Boolean(ocr)} onChange={(e) => { recognize(e.target.files[0]); e.target.value = ''; }} /></label></div><p className="small muted">首次 OCR 需要下载识别模型，耗时取决于网络。图片不会发送至服务器。</p>{ocr && <div className="ocr-progress" role="status"><progress value={ocr.progress} max={1} /><span>本地 OCR · {Math.round(ocr.progress * 100)}% · {ocr.status === 'recognizing text' ? '识别文字' : '加载识别组件'}</span></div>}<ErrorBox error={ocrError} /></>}{warnings.length > 0 && <div className="warning-box">{warnings.map((warning, i) => <p key={i}>{warning}</p>)}</div>}{fields && <form onSubmit={save} className="draft-form"><div className="section-title"><h3>确认要保存的事项</h3><span className="tag private">仅自己可见</span></div><label>标题<input value={fields.title} required maxLength={300} onChange={(e) => update('title', e.target.value)} /></label><div className="form-grid"><label>需要做什么（每行一步）<textarea rows={4} value={fields.actions} onChange={(e) => update('actions', e.target.value)} /></label><label>准备材料（每行一项）<textarea rows={4} value={fields.materials} onChange={(e) => update('materials', e.target.value)} /></label></div><label>办理入口（每行一个链接）<textarea rows={2} value={fields.urls} onChange={(e) => update('urls', e.target.value)} /></label><div className="form-grid"><label>截止日期<input type="date" value={fields.date} onChange={(e) => update('date', e.target.value)} /></label><label>原文明确的时刻（选填）<input type="time" disabled={!fields.date} value={fields.time} onChange={(e) => update('time', e.target.value)} /></label></div><p className="small muted">时区：北京时间。原文没有时刻时保留日期，不自动补成 23:59。</p><label className="check"><input type="checkbox" checked={fields.consent} onChange={(e) => update('consent', e.target.checked)} />我确认同步下面这段摘录，供自己日后核对</label>{fields.consent && <label>需同步的摘录<textarea rows={3} maxLength={2000} value={fields.excerpt} onChange={(e) => update('excerpt', e.target.value)} /></label>}<ErrorBox error={mutation.error} /><div className="form-actions"><button className="primary" disabled={mutation.busy || Boolean(ocr)}>{mutation.busy ? '保存中…' : '确认并保存事项'}</button><button type="button" onClick={close}>取消</button></div>{mutation.error?.status === 409 && <p className="small muted">当前编辑内容仍保留。关闭后重新打开事项，以最新版本继续编辑。</p>}</form>}</Modal>;
}

const sourceKindLabels = { website: '网站', rss: '网站 RSS', wechat: '微信公众号', authorized_feed: '授权订阅' };
const sourceRequestLabels = { pending: '待接入', connected: '已接通', rejected: '未接通' };
function SourceMode({ value, onChange, disabled = false, name = 'source-mode' }) {
  return <fieldset className="source-modes" disabled={disabled}><legend>接收哪些来源的信息</legend><label className="check"><input type="radio" name={name} checked={value.source_mode === 'all'} onChange={() => onChange({ ...value, source_mode: 'all' })} />全部来源</label><label className="check"><input type="radio" name={name} checked={value.source_mode === 'selected'} onChange={() => onChange({ ...value, source_mode: 'selected' })} />只看我选的来源（可 0 个）</label><p className="small muted">只选部分来源时，新信息按你的选择筛选。选 0 个将暂停接收新的来源信息，已有收藏仍保留。</p></fieldset>;
}
function SourceRequestForm({ onSave }) {
  const app = useApp(), mutation = useMutation(), key = useRef(idempotencyKey());
  const [name, setName] = useState(''), [kind, setKind] = useState('website'), [url, setURL] = useState(''), [autoSubscribe, setAutoSubscribe] = useState(true);
  function edit(setter, value) { setter(value); key.current = idempotencyKey(); }
  async function submit(event) {
    event.preventDefault();
    const cleanURL = url.trim() ? safeURL(url.trim()) : null;
    let invalid = !name.trim() || (url.trim() && !cleanURL) || (kind === 'website' && !cleanURL);
    if (cleanURL) { const parsed = new URL(cleanURL); invalid ||= Boolean(parsed.username || parsed.password) || (kind === 'wechat' && parsed.hostname !== 'mp.weixin.qq.com'); }
    if (invalid) { await mutation.run(() => Promise.reject(new Error(kind === 'wechat' ? '请填写公众号名称；链接选填，只接受 mp.weixin.qq.com 的文章链接。' : '请填写来源名称和完整的公开网站 http/https 网址。'))); return; }
    await mutation.run(() => api.addSourceRequest({ name: name.trim(), kind, url: cleanURL, auto_subscribe: autoSubscribe }, key.current), () => { setName(''); setURL(''); key.current = idempotencyKey(); onSave(); app.notify('接入申请已提交，可在下方查看处理状态。'); });
  }
  return <section className="panel source-request-form"><h2>申请接入新来源</h2><p className="small muted">没有找到想关注的网站或公众号？提交后由管理员检查公开访问条件并配置采集。公众号需要可用的授权订阅渠道，提交名称或文章链接后不会立即开始抓取。</p><form onSubmit={submit}><div className="form-grid"><label>来源名称<input required maxLength={200} value={name} onChange={(e) => edit(setName, e.target.value)} placeholder="例如：天津大学教务处" /></label><label>来源类型<select aria-label="来源类型" value={kind} onChange={(e) => { edit(setKind, e.target.value); setURL(''); }}><option value="website">网站</option><option value="wechat">微信公众号</option></select></label></div><label>{kind === 'website' ? '网站网址' : '公众号文章链接（选填）'}<input type="url" required={kind === 'website'} maxLength={2048} value={url} onChange={(e) => edit(setURL, e.target.value)} placeholder={kind === 'website' ? 'https://www.tju.edu.cn/' : 'https://mp.weixin.qq.com/s/…'} /></label><label className="check"><input type="checkbox" checked={autoSubscribe} onChange={(e) => edit(setAutoSubscribe, e.target.checked)} />接通后自动加入我选择的来源</label><ErrorBox error={mutation.error} /><button className="primary" disabled={mutation.busy}>{mutation.busy ? '提交中…' : '提交接入申请'}</button></form></section>;
}
function MySourceRequests({ page }) {
  return <section className="panel source-requests"><div className="section-title"><h2>我的接入申请</h2><button className="text-button" onClick={page.reload}>刷新申请状态</button></div><ErrorBox error={page.error} retry={page.reload} />{page.loading && <Loading />}{page.items.map((item) => <article className="source-request" key={item.id}><div className="section-title"><h3>{item.name}</h3><span className={'tag ' + (item.status === 'pending' ? 'warning' : '')}>{sourceRequestLabels[item.status]}</span></div><p className="small muted">{sourceKindLabels[item.kind]} · 提交于 {formatStamp(item.created_at)} · {item.auto_subscribe ? '接通后自动关注' : '接通后自行选择'}</p>{item.url && <External href={item.url}>提交的链接</External>}{item.resolution_note && <p className="request-note">处理说明：{item.resolution_note}</p>}{item.status === 'pending' && <p className="small muted">等待管理员检查并接入；目前尚未开始采集这个申请来源。</p>}</article>)}{!page.loading && !page.error && !page.items.length && <p className="small muted">尚未提交接入申请。</p>}<More page={page} /></section>;
}
function SourcesView() {
  const app = useApp(), mutation = useMutation();
  const subscriptions = useResource(api.subscriptions, [app.session?.user.id], Boolean(app.session));
  const requests = usePages(api.sourceRequests, [app.session?.user.id], Boolean(app.session), true);
  const [value, setValue] = useState(null), [search, setSearch] = useState(''), [kind, setKind] = useState('all');
  useEffect(() => { setValue(subscriptions.data ? normalizeSubscriptions(subscriptions.data) : null); }, [subscriptions.data]);
  const normalized = search.trim().toLocaleLowerCase();
  const filtered = app.sources.filter((source) => (!normalized || (source.name + ' ' + source.origin_url).toLocaleLowerCase().includes(normalized)) && (kind === 'all' || (kind === 'website' ? ['website', 'rss'].includes(source.kind) : kind === 'other' ? !['website', 'rss', 'wechat'].includes(source.kind) : source.kind === kind)));
  async function save() {
    await mutation.run(async () => { const latest = await api.subscriptions(); return api.saveSubscriptions({ source_ids: value.source_ids, source_mode: value.source_mode, event_types: latest.event_types }); }, (saved) => { subscriptions.update(saved); app.updated(); app.notify('来源订阅已保存。'); });
  }
  return <><Heading eyebrow="让信息按你的选择到达" title="信息来源">选择想关注的网站和公众号；核验状态、采集健康和更新时间均以实际结果为准。</Heading>{app.session ? <section className="panel source-selection"><ErrorBox error={subscriptions.error} retry={subscriptions.reload} />{subscriptions.loading && <Loading label="正在读取你的订阅…" />}{value && <><SourceMode value={value} onChange={setValue} disabled={mutation.busy || subscriptions.loading} /><div className="inline-actions"><button className="primary" disabled={mutation.busy || subscriptions.loading} onClick={save}>{mutation.busy ? '保存中…' : '保存来源订阅'}</button><span className="small muted">{value.source_mode === 'all' ? '接收全部来源的新信息' : '已选 ' + value.source_ids.length + ' 个来源'}</span></div><ErrorBox error={mutation.error} /></>}</section> : <div className="panel"><p>登录后可以选择自己关注的来源，并申请接入新的网站或公众号。</p><button className="primary" onClick={app.ensureLogin}>登录并选择来源</button></div>}<div className="section-title"><h2>全部来源</h2><span className="small muted">已载入 {filtered.length} 个来源</span></div><label className="search-input"><Search size={18} /><input aria-label="搜索来源名称或网址" type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索来源名称或网址" /></label><div className="filter-row" role="group" aria-label="来源类型筛选">{[['all', '全部类型'], ['website', '网站'], ['wechat', '微信公众号'], ['other', '其他来源']].map(([id, label]) => <button key={id} aria-pressed={kind === id} className={kind === id ? 'selected' : ''} onClick={() => setKind(id)}>{label}</button>)}</div><ErrorBox error={app.sourcesPage.error} retry={app.sourcesPage.reload} />{app.sourcesPage.loading && <Loading />}{filtered.map((source) => <article className="source-row source-choice" key={source.id}><div className="source-icon"><BookOpen size={20} /></div><div className="source-info"><h3>{source.name}</h3><div className="inline-actions small"><span className="tag">{sourceKindLabels[source.kind]}</span><span className={'tag ' + (source.verification === 'unverified' ? 'warning' : '')}>{verificationLabels[source.verification]}</span><span className={'health ' + source.health}>{healthLabels[source.health]}</span>{!source.enabled && <span className="muted">已停用</span>}</div><p className="small muted source-url">{source.origin_url}</p><p className="small muted">上次成功更新：{formatStamp(source.last_success_at)}</p>{source.kind === 'wechat' && source.health !== 'healthy' && <p className="warning-line">公众号采集尚未就绪或更新异常，请核对原文。</p>}</div><div className="source-choice-actions">{app.session && value && <label className="check"><input type="checkbox" aria-label={'关注' + source.name} checked={value.source_mode === 'all' || value.source_ids.includes(source.id)} disabled={mutation.busy || subscriptions.loading || value.source_mode === 'all'} onChange={(e) => setValue((old) => ({ ...old, source_ids: e.target.checked ? [...old.source_ids, source.id] : old.source_ids.filter((id) => id !== source.id) }))} />关注</label>}<External href={source.origin_url}>查看原站</External></div></article>)}{!app.sourcesPage.loading && !app.sourcesPage.error && !filtered.length && <Empty title={app.sources.length ? '没有符合条件的已载入来源' : '还没有接入信息来源'}>可以调整筛选、继续加载来源，或提交接入申请。</Empty>}{app.sourcesPage.cursor && <p className="small muted">当前已载入 {app.sources.length} 个来源，搜索和筛选作用于已载入来源；继续加载可查看更多。</p>}<More page={app.sourcesPage} />{app.session && <><SourceRequestForm onSave={requests.reload} /><MySourceRequests page={requests} /></>}</>;
}
function ProfileForm({ initial, catalog, onSave }) {
  const app = useApp(), mutation = useMutation(), [value, setValue] = useState(initial), [interests, setInterests] = useState(initial.interests.join('、'));
  const update = (name, next) => setValue((old) => ({ ...old, [name]: next }));
  return <form onSubmit={(e) => { e.preventDefault(); mutation.run(() => api.saveProfile({ ...value, interests: interests.split(/[,，、\n]/).map((s) => s.trim()).filter(Boolean) }), (saved) => { onSave(saved); app.updated(); app.notify('画像已更新。'); }); }}><div className="form-grid"><label>学院<select aria-label="学院" value={value.college_id || ''} onChange={(e) => update('college_id', e.target.value || null)}><option value="">尚未填写</option>{catalog?.colleges.map((college) => <option key={college.id} value={college.id}>{college.name}</option>)}</select></label><label>专业<input maxLength={100} value={value.major || ''} onChange={(e) => update('major', e.target.value || null)} /></label><label>入学年份<input type="number" min={2000} max={2100} value={value.entry_year || ''} onChange={(e) => update('entry_year', e.target.value ? Number(e.target.value) : null)} /></label><label>培养层次<select aria-label="培养层次" value={value.education_level} onChange={(e) => update('education_level', e.target.value)}><option value="unknown">尚未填写</option>{(catalog?.education_levels || []).filter((level) => level.id !== 'unknown').map((level) => <option key={level.id} value={level.id}>{level.name}</option>)}</select></label><label>校区<select aria-label="校区" value={value.campus} onChange={(e) => update('campus', e.target.value)}><option value="unknown">尚未填写</option>{(catalog?.campuses || []).filter((campus) => campus.id !== 'unknown').map((campus) => <option key={campus.id} value={campus.id}>{campus.name}</option>)}</select></label><label>兴趣（逗号或顿号分隔）<input maxLength={400} value={interests} onChange={(e) => setInterests(e.target.value)} placeholder="例如：竞赛、志愿服务、实习" /></label></div><p className="small muted">画像用于筛选相关信息。条件不完整的通知仍需自行核对。</p><ErrorBox error={mutation.error} /><button className="primary" disabled={mutation.busy}>{mutation.busy ? '保存中…' : '保存画像'}</button></form>;
}
function SubscriptionForm({ initial, onSave }) {
  const app = useApp(), mutation = useMutation(), [value, setValue] = useState(() => normalizeSubscriptions(initial));
  function toggle(name, key) { setValue((old) => ({ ...old, [name]: old[name].includes(key) ? old[name].filter((id) => id !== key) : [...old[name], key] })); }
  return <form onSubmit={(e) => { e.preventDefault(); mutation.run(() => api.saveSubscriptions(normalizeSubscriptions(value)), (saved) => { setValue(normalizeSubscriptions(saved)); onSave(saved); app.updated(); app.notify('订阅已保存。'); }); }}><h3>关注哪些来源</h3><SourceMode value={value} onChange={setValue} disabled={mutation.busy} name="account-source-mode" />{app.sources.map((source) => <label className="check subscription-check" key={source.id}><input type="checkbox" checked={value.source_mode === 'all' || value.source_ids.includes(source.id)} disabled={mutation.busy || value.source_mode === 'all'} onChange={() => toggle('source_ids', source.id)} /><span>{source.name}<small className="muted">{healthLabels[source.health]} · {verificationLabels[source.verification]}</small></span></label>)}{!app.sources.length && <p className="muted">尚无来源可选。</p>}<More page={app.sourcesPage} /><h3>关注哪些类别</h3><div className="checkbox-grid">{Object.entries(types).map(([id, label]) => <label className="check" key={id}><input type="checkbox" checked={value.event_types.includes(id)} onChange={() => toggle('event_types', id)} />{label}</label>)}</div><ErrorBox error={mutation.error} /><button className="primary" disabled={mutation.busy}>{mutation.busy ? '保存中…' : '保存订阅'}</button></form>;
}
function PreferencesForm({ initial, onSave }) {
  const app = useApp(), mutation = useMutation(), [value, setValue] = useState(initial), [url, setURL] = useState(null), [revoke, setRevoke] = useState(false);
  const push = useResource(api.pushStatus, [app.session?.user.id, app.refresh]);
  const pushReady = !push.loading && !push.error && push.data?.available === true;
  const update = (name, next) => setValue((old) => ({ ...old, [name]: next }));
  async function copy() {
    try { await navigator.clipboard.writeText(url); app.notify('日历链接已复制，请仅在自己的日历中使用。'); } catch { app.notify('未能自动复制，请选中下面链接手动复制。'); }
  }
  return <><form onSubmit={(e) => { e.preventDefault(); mutation.run(() => api.savePreferences(value), (saved) => { onSave(saved); app.updated(); app.notify('提醒偏好已保存。'); }); }}><div className="setting-row"><div><strong>每日简报</strong><p>把相关信息汇成一份清单。</p></div><label className="switch-label"><input type="checkbox" checked={value.daily_digest_enabled} onChange={(e) => update('daily_digest_enabled', e.target.checked)} />开启</label></div><div className="form-grid"><label>简报时间<input type="time" required value={value.digest_time} onChange={(e) => update('digest_time', e.target.value)} /></label><label>只有日期时的提醒时刻<input type="time" required value={value.date_only_reminder_time} onChange={(e) => update('date_only_reminder_time', e.target.value)} /></label></div><div className="setting-row"><div><strong>截止提醒</strong><p>仅提醒收藏的公开事项和自己的未完事项。</p></div><label className="switch-label"><input type="checkbox" checked={value.reminders_enabled} onChange={(e) => update('reminders_enabled', e.target.checked)} />开启</label></div><label>提前多少分钟提醒<input type="number" min={0} max={10080} required value={value.reminder_lead_minutes} onChange={(e) => update('reminder_lead_minutes', Number(e.target.value))} /></label><div className="setting-row"><div><strong>重要变更提醒</strong><p>关注延期、材料变化、取消和撤回。</p></div><label className="switch-label"><input type="checkbox" checked={value.change_reminders_enabled} onChange={(e) => update('change_reminders_enabled', e.target.checked)} />开启</label></div><div className="form-grid"><label>免打扰开始<input type="time" value={value.quiet_start || ''} onChange={(e) => { const time = e.target.value || null; setValue((old) => ({ ...old, quiet_start: time, quiet_end: time ? old.quiet_end || '08:00' : null })); }} /></label><label>免打扰结束<input type="time" disabled={!value.quiet_start} required={Boolean(value.quiet_start)} value={value.quiet_end || ''} onChange={(e) => update('quiet_end', e.target.value || null)} /></label></div><h2 className="setting-heading">提醒渠道</h2><div className="checkbox-grid">{[['in_app', '站内提醒'], ['email', '邮件'], ['push', '推送通知']].map(([id, label]) => <label className="check" key={id}><input type="checkbox" checked={value.channels.includes(id)} disabled={id === 'push' && !pushReady && !value.channels.includes(id)} onChange={(e) => update('channels', e.target.checked ? [...value.channels, id] : value.channels.filter((channel) => channel !== id))} />{label}</label>)}</div><p className="small muted">{push.loading ? '正在核对个人推送配置…' : pushReady ? '个人推送接收目标已配置。' : '个人推送尚未就绪，运营人员需为你的账号配置独立接收目标。'}邮件需要完成服务配置；实际送达情况请查看提醒记录。</p><ErrorBox error={push.error} retry={push.reload} /><div className="setting-row"><div><strong>日历订阅</strong><p>把关注的截止日期添加到自己的日历。</p></div><label className="switch-label"><input type="checkbox" checked={value.calendar_enabled} onChange={(e) => update('calendar_enabled', e.target.checked)} />开启</label></div><ErrorBox error={mutation.error} /><button className="primary" disabled={mutation.busy}>{mutation.busy ? '保存中…' : '保存提醒偏好'}</button></form><section className="detail-section"><h2 className="setting-heading">我的日历链接</h2><p className="small muted">订阅链接相当于访问凭证，请保密。日历客户端的同步速度由客户端决定。</p><div className="inline-actions"><button disabled={mutation.busy} onClick={() => mutation.run(api.calendarToken, (data) => setURL(location.origin + '/api/v1/calendar/' + encodeURIComponent(data.token) + '.ics'))}>生成订阅链接</button><button className="danger-text" onClick={() => setRevoke(true)}>撤销现有链接</button></div>{url && <div className="calendar-link"><input aria-label="日历订阅链接" readOnly value={url} onFocus={(e) => e.target.select()} /><button onClick={copy}>复制</button><External href={url}>下载 ICS</External></div>}{revoke && <div className="confirm-inline"><p>撤销后，已有订阅地址无法继续获取更新。</p><button className="danger" disabled={mutation.busy} onClick={() => mutation.run(api.revokeCalendar, () => { setURL(null); setRevoke(false); app.notify('日历链接已撤销。'); })}>确认撤销</button><button onClick={() => setRevoke(false)}>保留</button></div>}</section></>;
}
function DigestsView() {
  const app = useApp(), digests = usePages(api.digests, [app.refresh]), deliveries = usePages(api.deliveries, [app.refresh]);
  const [tab, setTab] = useState('digest');
  return <><div className="tabs"><button className={tab === 'digest' ? 'active' : ''} onClick={() => setTab('digest')}>每日简报</button><button className={tab === 'delivery' ? 'active' : ''} onClick={() => setTab('delivery')}>提醒记录</button></div>{tab === 'digest' ? <><ErrorBox error={digests.error} retry={digests.reload} />{digests.loading && <Loading />}{digests.items.map((digest) => <section className="digest" key={digest.id}><h3>{digest.date} 简报</h3><p className="small muted">生成于 {formatStamp(digest.generated_at)}</p><div className="cards">{digest.cards.map((card) => <EventCard compact key={card.event.id} card={card} />)}{digest.personal_items.map((item) => <PersonalCard key={item.id} item={item} />)}</div>{!digest.cards.length && !digest.personal_items.length && <p className="muted">这一天没有相关事项。</p>}</section>)}{!digests.loading && !digests.error && !digests.items.length && <Empty icon={Bell} title="还没有简报">开启每日简报后，由后台按设置时间生成。</Empty>}<More page={digests} /></> : <><ErrorBox error={deliveries.error} retry={deliveries.reload} />{deliveries.loading && <Loading />}{deliveries.items.map((delivery) => <article className="delivery-row" key={delivery.id}><Bell size={19} /><div><strong>{({ digest: '每日简报', deadline: '截止提醒', material_change: '事项变更', cancelled: '事项取消', withdrawn: '公开信息撤回' })[delivery.kind]}</strong><p className="small muted">{({ email: '邮件', push: '推送', in_app: '站内' })[delivery.channel] || '站内'} · 安排于 {formatStamp(delivery.scheduled_at)}{delivery.sent_at && ' · 送达于 ' + formatStamp(delivery.sent_at)}</p>{delivery.subject?.kind === 'public_event' && <button className="text-button" onClick={() => app.openEvent(delivery.subject.id)}>查看相关事项</button>}{delivery.subject?.kind === 'personal_item' && <small className="muted">自己的个人事项</small>}</div><span className={'tag ' + (['failed', 'unknown'].includes(delivery.status) ? 'warning' : '')}>{deliveryLabels[delivery.status]}</span></article>)}{!deliveries.loading && !deliveries.error && !deliveries.items.length && <Empty icon={Bell} title="暂无提醒记录">这里显示真实安排和送达状态。</Empty>}<More page={deliveries} /></>}</>;
}
function Account() {
  const app = useApp(), [tab, setTab] = useState('profile');
  const data = useResource(() => Promise.all([api.profile(), api.subscriptions(), api.preferences()]), [app.session?.user.id], Boolean(app.session));
  if (!app.session) return <><Heading eyebrow="让信息更贴近你" title="我的设置" /><Empty title="登录后设置画像和提醒" action={<button className="primary" onClick={app.ensureLogin}>登录</button>} /></>;
  return <><Heading eyebrow={app.session.user.email} title="我的设置">完善画像、选择订阅，用合适的频率接收信息。</Heading><div className="tabs">{[['profile', '个人画像'], ['subscriptions', '信息订阅'], ['preferences', '提醒与日历'], ['digests', '简报与提醒记录']].map(([id, label]) => <button className={tab === id ? 'active' : ''} key={id} onClick={() => setTab(id)}>{label}</button>)}</div><ErrorBox error={data.error} retry={data.reload} />{data.loading && <Loading />}{data.data && <section className="panel">{tab === 'profile' && <ProfileForm key={app.session.user.id} initial={data.data[0]} catalog={app.catalog} onSave={(saved) => data.update([saved, data.data[1], data.data[2]])} />}{tab === 'subscriptions' && <SubscriptionForm initial={data.data[1]} onSave={(saved) => data.update([data.data[0], saved, data.data[2]])} />}{tab === 'preferences' && <PreferencesForm initial={data.data[2]} onSave={(saved) => data.update([data.data[0], data.data[1], saved])} />}{tab === 'digests' && <DigestsView />}</section>}</>;
}

function SourceEditor({ source, close, refresh }) {
  const mutation = useMutation(), key = useRef(idempotencyKey());
  const [value, setValue] = useState(source ? { name: source.name, kind: source.kind, origin_url: source.origin_url, adapter: source.adapter, source_identity: source.source_identity || '', feed_content_mode: source.adapter === 'website' ? 'link' : source.feed_content_mode || 'link' } : { name: '', kind: 'rss', origin_url: '', adapter: 'rss', source_identity: '', feed_content_mode: 'link' });
  const update = (name, next) => setValue((old) => ({ ...old, [name]: next }));
  return <Modal title={source ? '修改信息来源' : '添加信息来源'} close={close}><p className="small muted">仅接入公开页面或已经授权的订阅。公众号通过配置好的 RSS 或 API 采集器获取，不在学生浏览器登录第三方服务。</p><form onSubmit={(e) => { e.preventDefault(); const body = { ...value, source_identity: value.source_identity.trim() || null, feed_content_mode: value.adapter === 'website' ? 'link' : value.feed_content_mode }; if (source) delete body.kind; mutation.run(() => source ? api.patchSource(source.id, body) : api.addSource(body, key.current), () => { refresh(); close(); }); }}><label>来源名称<input required maxLength={200} value={value.name} onChange={(e) => update('name', e.target.value)} /></label><label>来源类型<select disabled={Boolean(source)} value={value.kind} onChange={(e) => { update('kind', e.target.value); update('adapter', e.target.value === 'wechat' ? 'werss' : e.target.value === 'website' ? 'website' : e.target.value === 'authorized_feed' ? 'authorized_api' : 'rss'); if (e.target.value === 'website') update('feed_content_mode', 'link'); }}><option value="rss">RSS 订阅</option><option value="website">公开网页</option><option value="wechat">微信公众号（已授权采集器）</option><option value="authorized_feed">其他授权订阅</option></select></label><label>原始地址<input type="url" required value={value.origin_url} onChange={(e) => update('origin_url', e.target.value)} placeholder="https://…" /></label><label>适配器<select required value={value.adapter} onChange={(e) => { update('adapter', e.target.value); if (e.target.value === 'website') update('feed_content_mode', 'link'); }}><option value="rss">RSS</option><option value="website">公开网站</option><option value="rsshub">RSSHub</option><option value="werss">WeRSS 公众号订阅</option><option value="authorized_api">授权 API</option></select></label><label>正文获取方式<select value={value.adapter === 'website' ? 'link' : value.feed_content_mode} disabled={value.adapter === 'website'} onChange={(e) => update('feed_content_mode', e.target.value)}><option value="link">打开原网页获取正文</option><option value="authorized_fulltext">使用已授权订阅全文</option></select></label>{value.adapter === 'website' ? <p className="small muted">公开网站从原网页获取正文。</p> : value.feed_content_mode === 'authorized_fulltext' ? <p className="warning-line">仅在已核验供稿授权且订阅包含完整正文时启用；摘要不能当作全文。原网页未直接核验，依据会明确标注。</p> : <p className="small muted">按订阅提供的原网页地址获取正文。</p>}<p className="small muted">修改地址、适配器、来源标识或正文获取方式后，需要重新核验。</p><label>来源标识（选填）<input value={value.source_identity} onChange={(e) => update('source_identity', e.target.value)} placeholder="由适配器定义的来源 ID" /></label><p className="small muted">地址不得包含用户名、密码或 Token。凭据由维护者在服务器配置，来源标识只填稳定账号或频道 ID。</p><ErrorBox error={mutation.error} /><div className="form-actions"><button className="primary" disabled={mutation.busy}>{mutation.busy ? '保存中…' : '保存来源'}</button><button type="button" onClick={close}>取消</button></div></form></Modal>;
}
function VerificationEditor({ source, close, refresh }) {
  const mutation = useMutation(), [verification, setVerification] = useState(source.verification), [basis, setBasis] = useState(source.verification_basis || '');
  return <Modal title="核验信息来源" close={close}><h3>{source.name}</h3><form onSubmit={(e) => { e.preventDefault(); mutation.run(() => api.verifySource(source.id, { verification, basis: basis.trim() }), () => { refresh(); close(); }); }}><label>核验结果<select value={verification} onChange={(e) => setVerification(e.target.value)}>{Object.entries(verificationLabels).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label><label>核验依据<textarea rows={4} required value={basis} onChange={(e) => setBasis(e.target.value)} placeholder="说明官网关联、组织授权或尚未确认的原因" /></label><ErrorBox error={mutation.error} /><button className="primary" disabled={mutation.busy}>保存核验结果</button></form></Modal>;
}
function ReviewCard({ candidate, refresh }) {
  const mutation = useMutation(), [reason, setReason] = useState(''), [decision, setDecision] = useState('accept'), [edit, setEdit] = useState(false), [json, setJSON] = useState(JSON.stringify(candidate, null, 2)), [parseData, setParseData] = useState(null), [snapshot, setSnapshot] = useState(null);
  async function submit(event) {
    event.preventDefault();
    let replacement = null;
    if (edit) {
      try {
        replacement = JSON.parse(json);
        if (replacement.candidate_id !== candidate.candidate_id || replacement.revision !== candidate.revision) throw new Error('候选 ID 和版本不可改动。');
      } catch (err) { mutation.run(() => Promise.reject(new Error('修正内容无效：' + err.message))); return; }
    }
    await mutation.run(() => api.review(candidate.candidate_id, { expected_candidate_revision: candidate.revision, decision, reason: reason.trim(), replacement_candidate: replacement }), refresh, refresh);
  }
  return <article className="review-card"><div className="card-top"><span className="tag warning">{candidate.review_status === 'pending' ? '待审核' : candidate.review_status === 'accepted' ? '已接受' : '已拒绝'}</span><span className="small muted">候选 v{candidate.revision} · {candidate.change_kind === 'new' ? '新事项' : candidate.change_kind === 'update' ? '信息更新' : '取消'}</span></div><h3>{candidate.title}</h3><p>{candidate.action_steps.join('；') || '无明确行动'}</p><div className="inline-actions">{candidate.deadlines.map((d, i) => <span className="tag" key={i}>{deadlineText(d)}</span>)}</div>{candidate.warnings.length > 0 && <div className="warning-box">{candidate.warnings.map((warning, i) => <p key={i}>{warning}</p>)}</div>}<details><summary>查看原文依据与解析结果</summary>{candidate.evidence.map((evidence) => <blockquote className="evidence" key={evidence.id}><p>{evidence.quote}</p><footer><CaptureNotice capture={evidence.capture} /><External href={evidence.source_url}>原网页</External><button className="text-button" disabled={mutation.busy} onClick={() => mutation.run(() => api.adminParsed(evidence.snapshot_id), setParseData)}>查看解析全文</button><button className="text-button" disabled={mutation.busy} onClick={() => mutation.run(() => api.adminSnapshot(evidence.snapshot_id), setSnapshot)}>打开原文件</button></footer><small className="muted">证据 ID：{evidence.id}{evidence.page && ' · 第 ' + evidence.page + ' 页'}</small></blockquote>)}{snapshot && <div className="panel"><h3>{snapshot.snapshot.title}</h3><CaptureNotice capture={snapshot.snapshot.capture} /><External href={snapshotURL(snapshot.signed_url, location.origin)} className="entry-link">查看原始快照文件</External><p className="small muted">链接有效至 {formatStamp(snapshot.expires_at)}</p></div>}{parseData && <div className="parsed-text">{parseData.change && <p className="change-note"><small>相比上次采集</small>{parseData.change.summary}（新增 {parseData.change.counts.added} 段 · 删除 {parseData.change.counts.removed} 段 · 改写 {parseData.change.counts.rewritten} 段）{parseData.change.kinds.includes('deadline_or_date_touched') && <strong>时间或期限可能有变化</strong>}</p>}{parseData.segments.map((segment) => <p key={segment.id}><small>{segment.page ? '第 ' + segment.page + ' 页 · ' : ''}{segment.id}</small>{segment.text}</p>)}</div>}</details><CandidateAttachments candidate={candidate} refresh={refresh} />{candidate.review_status === 'pending' && <form onSubmit={submit}><label className="check"><input type="checkbox" checked={edit} onChange={(e) => setEdit(e.target.checked)} />修正候选结构后再审核</label>{edit && <><label>候选 JSON<textarea className="code-input" rows={15} value={json} onChange={(e) => setJSON(e.target.value)} /></label><p className="small muted">修改日期、材料等字段时，保留对应真实原文证据。服务端会重新校验结构与引用；不能仅把依据 ID 填成任意值。</p></>}<div className="form-grid"><label>决定<select aria-label="处理结果" value={decision} onChange={(e) => setDecision(e.target.value)}><option value="accept">接受并进入事项归并</option><option value="reject">拒绝发布</option></select></label><label>理由<input required value={reason} onChange={(e) => setReason(e.target.value)} placeholder="说明核对结果" /></label></div><ErrorBox error={mutation.error} /><button className={decision === 'reject' ? 'danger' : 'primary'} disabled={mutation.busy}>{mutation.busy ? '提交中…' : '提交审核决定'}</button></form>}</article>;
}
function PublicationForm() {
  const app = useApp(), mutation = useMutation(), events = usePages(api.adminEvents, [app.refresh]);
  const [id, setID] = useState(''), [version, setVersion] = useState(1), [publication, setPublication] = useState('withdrawn'), [reason, setReason] = useState(''), [result, setResult] = useState(null);
  return <section className="panel"><h2>公开事项发布状态</h2><p className="small muted">撤回后不再出现在公开列表和检索中。从管理列表选择事项后再确认修改。发布状态修改也会产生新版本。</p><ErrorBox error={events.error} retry={events.reload} />{events.loading && <Loading />}{events.items.map((event) => <div className="job-row" key={event.id}><div><strong>{event.title}</strong><p className="small muted">v{event.version} · {event.publication_state === 'withdrawn' ? '已撤回' : '公开中'}</p></div><button onClick={() => { setID(event.id); setVersion(event.version); setPublication(event.publication_state === 'published' ? 'withdrawn' : 'published'); setReason(''); setResult(null); }}>选择此事项</button></div>)}<More page={events} /><form onSubmit={(e) => { e.preventDefault(); mutation.run(() => api.publication(id.trim(), { expected_event_version: Number(version), publication_state: publication, reason: reason.trim() }), (data) => { setResult(data); setVersion(data.version); events.reload(); app.updated(); }); }}><div className="form-grid"><label>选择事项<select aria-label="选择事项" required value={id} onChange={(e) => { const event = events.items.find((item) => item.id === e.target.value); setID(e.target.value); if (event) { setVersion(event.version); setPublication(event.publication_state === 'published' ? 'withdrawn' : 'published'); setReason(''); setResult(null); } }}><option value="">请选择要操作的事项</option>{events.items.map((event) => <option value={event.id} key={event.id}>{event.title} · v{event.version} · {event.publication_state === 'withdrawn' ? '已撤回' : '公开中'}</option>)}</select></label><label>当前版本<input readOnly value={'v' + version} /></label><label>目标状态<select aria-label="目标状态" value={publication} onChange={(e) => setPublication(e.target.value)}><option value="withdrawn">撤回公开内容</option><option value="published">重新公开</option></select></label><label>操作依据<input required value={reason} onChange={(e) => setReason(e.target.value)} /></label></div><ErrorBox error={mutation.error} /><button className={publication === 'withdrawn' ? 'danger' : 'primary'} disabled={mutation.busy}>{mutation.busy ? '提交中…' : '确认修改发布状态'}</button>{result && <p className="success-line" role="status">{result.title} · 已{result.publication_state === 'withdrawn' ? '撤回' : '公开'}，当前版本 v{result.version}</p>}</form></section>;
}
function SourceRequestResolution({ item, sources, close, refresh }) {
  const app = useApp(), mutation = useMutation(), key = useRef(idempotencyKey());
  const [decision, setDecision] = useState('connect'), [sourceId, setSourceId] = useState(''), [note, setNote] = useState('');
  const matching = sources.items.filter((source) => item.kind === 'wechat' ? source.kind === 'wechat' : source.kind !== 'wechat');
  const ready = (source) => source.enabled && source.health === 'healthy' && source.last_success_at && source.verification !== 'unverified';
  const selected = matching.find((source) => source.id === sourceId);
  function edit(setter, value) { setter(value); key.current = idempotencyKey(); }
  async function submit(event) { event.preventDefault(); if (decision === 'connect' && (!selected || !ready(selected))) return; await mutation.run(() => api.resolveSourceRequest(item.id, { decision, source_id: decision === 'connect' ? sourceId : null, note: note.trim() }, key.current), () => { refresh(); app.updated(); app.notify(decision === 'connect' ? '申请已连接到实际采集来源。' : '申请处理结果已保存。'); close(); }); }
  return <Modal title="处理来源接入申请" close={close}><p><strong>{item.name}</strong> · {sourceKindLabels[item.kind]}</p>{item.url && <External href={item.url}>申请链接</External>}<form onSubmit={submit}><label>处理结果<select aria-label="处理结果" value={decision} onChange={(e) => edit(setDecision, e.target.value)}><option value="connect">连接已就绪来源</option><option value="reject">本次不接入</option></select></label>{decision === 'connect' && <><p className="small muted">先在“信息来源”中建立并核验来源。仅已启用、采集正常且有成功更新记录的来源可以接通；公众号申请必须连接公众号来源。</p><label>连接的来源<select aria-label="连接的来源" required value={sourceId} onChange={(e) => edit(setSourceId, e.target.value)}><option value="">请选择已就绪来源</option>{matching.map((source) => <option key={source.id} value={source.id} disabled={!ready(source)}>{source.name}{!ready(source) ? '（尚未就绪）' : ''}</option>)}</select></label><ErrorBox error={sources.error} retry={sources.reload} /><More page={sources} /></>}<label>处理说明<textarea rows={3} required maxLength={1000} value={note} onChange={(e) => edit(setNote, e.target.value)} placeholder="说明接入结果或暂不能接入的原因，学生可以看到。" /></label><ErrorBox error={mutation.error} /><div className="form-actions"><button className="primary" disabled={mutation.busy || (decision === 'connect' && (!selected || !ready(selected)))}>{mutation.busy ? '保存中…' : '确认处理'}</button><button type="button" onClick={close}>取消</button></div></form></Modal>;
}
function AdminSourceRequests({ sources }) {
  const app = useApp(), requests = usePages(api.adminSourceRequests, [app.refresh], true, true), [selected, setSelected] = useState(null);
  return <><div className="section-title"><h2>学生接入申请</h2><button className="text-button" onClick={requests.reload}>刷新接入申请</button></div><p className="small muted">核实来源身份和实际采集结果后再连接申请。接通不会代替公众号授权，也不会把未就绪渠道标成可用。</p><ErrorBox error={requests.error} retry={requests.reload} />{requests.loading && <Loading />}{requests.items.map((item) => <article className="admin-source source-request" key={item.id}><div className="section-title"><h3>{item.name}</h3><span className={'tag ' + (item.status === 'pending' ? 'warning' : '')}>{sourceRequestLabels[item.status]}</span></div><p className="small muted">{sourceKindLabels[item.kind]} · {formatStamp(item.created_at)} · {item.auto_subscribe ? '接通后自动关注' : '自行选择关注'}</p>{item.url && <External href={item.url}>申请链接</External>}{item.resolution_note && <p className="request-note">处理说明：{item.resolution_note}</p>}{item.status === 'pending' && <div className="form-actions"><button onClick={() => setSelected(item)}>处理申请</button></div>}</article>)}{!requests.loading && !requests.error && !requests.items.length && <Empty title="没有来源接入申请" />}<More page={requests} />{selected && <SourceRequestResolution item={selected} sources={sources} close={() => setSelected(null)} refresh={requests.reload} />}</>;
}
function Admin() {
  const app = useApp(), mutation = useMutation();
  const [tab, setTab] = useState('sources'), [edit, setEdit] = useState(null), [verify, setVerify] = useState(null), [job, setJob] = useState(null);
  const sources = usePages(api.adminSources, [app.refresh]), reviews = usePages(api.reviews, [app.refresh]), jobs = usePages(api.jobs, [app.refresh]);
  const monitored = useResource(() => api.job(job), [job], Boolean(job));
  const isAdmin = app.session?.user.role === 'admin';
  useEffect(() => {
    if (!job || ['succeeded', 'failed', 'cancelled'].includes(monitored.data?.state)) return;
    const timer = setInterval(() => { if (!document.hidden) monitored.reload(); }, 3000);
    return () => clearInterval(timer);
  }, [job, monitored.data?.state]);
  useEffect(() => {
    if (tab !== 'jobs' || !jobs.items.some((j) => ['queued', 'running'].includes(j.state))) return;
    const timer = setInterval(() => { if (!document.hidden) jobs.reload(); }, 5000);
    return () => clearInterval(timer);
  }, [tab, jobs.items]);
  return <><Heading eyebrow="校园信息维护" title="管理工作台" action={tab === 'sources' && isAdmin && <button className="primary" onClick={() => setEdit({})}><Plus size={16} />接入来源</button>}>抓取任务、候选审核和公开状态均以服务端实际结果为准。</Heading><div className="tabs">{[['sources', '信息来源'], ['reviews', '候选审核'], ['jobs', '后台任务'], ...(isAdmin ? [['requests', '接入申请'], ['publication', '发布与撤回']] : [])].map(([id, label]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>{label}</button>)}</div><ErrorBox error={mutation.error} />{tab === 'sources' && <><div className="section-title"><h2>已接入来源</h2><span className="small muted">已载入 {sources.items.length} 个</span></div><ErrorBox error={sources.error} retry={sources.reload} />{sources.loading && <Loading />}{sources.items.map((source) => <article className="admin-source" key={source.id}><div className="section-title"><h3>{source.name}</h3><span className={'health ' + source.health}>{healthLabels[source.health]}</span></div><p className="small muted">{source.adapter} · 正文：{feedContentLabels[source.feed_content_mode || 'link']} · {verificationLabels[source.verification]} · {source.enabled ? '已启用' : '已停用'}</p><p className="source-address">{source.origin_url}</p><p className="small muted">成功更新：{formatStamp(source.last_success_at)} · 最近检查：{formatStamp(source.last_checked_at)}</p>{source.last_error_code && <p className="warning-line">采集错误：{source.last_error_code}</p>}{source.ingestion && <div className="small muted"><p>当前采集窗口：已完成 {source.ingestion.completed_count} 条 · 待补采 {source.ingestion.pending_count} 条 · 失败待处理 {source.ingestion.failed_count} 条</p>{source.ingestion.window_truncated && <p className="warning-line">列表超过单次发现上限，尚未覆盖全部历史通知，请查看原站。</p>}</div>}<div className="inline-actions"><button disabled={mutation.busy || !source.enabled} onClick={() => mutation.run(() => api.fetchSource(source.id), (result) => { setJob(result.id); monitored.reload(); jobs.reload(); })}><RefreshCw size={14} />手动抓取</button>{isAdmin && <><button onClick={() => setEdit(source)}>修改来源</button><button onClick={() => setVerify(source)}>核验来源</button><button disabled={mutation.busy} onClick={() => mutation.run(() => api.patchSource(source.id, { enabled: !source.enabled }), () => { sources.reload(); app.updated(); })}>{source.enabled ? '停用' : '启用'}</button></>}</div></article>)}{!sources.loading && !sources.error && !sources.items.length && <Empty title="还没有接入来源">管理员可以添加公开 RSS、网站或配置好的公众号订阅。</Empty>}<More page={sources} />{job && <div className="panel"><h3>当前抓取任务</h3><ErrorBox error={monitored.error} retry={monitored.reload} />{monitored.data ? <><p>{jobLabels[monitored.data.state]} · {monitored.data.attempts} 次尝试</p>{monitored.data.last_error_code && <p className="warning-line">{monitored.data.last_error_code}</p>}<p className="small muted">任务 ID：{job}</p><button onClick={() => { monitored.reload(); sources.reload(); reviews.reload(); }}>刷新结果</button></> : <Loading />}</div>}</>}{tab === 'reviews' && <><ErrorBox error={reviews.error} retry={reviews.reload} />{reviews.loading && <Loading />}{reviews.items.map((candidate) => <ReviewCard key={candidate.candidate_id + ':' + candidate.revision} candidate={candidate} refresh={() => { reviews.reload(); app.updated(); }} />)}{!reviews.loading && !reviews.error && !reviews.items.length && <Empty icon={ShieldCheck} title="当前没有待审核候选">抓取与解析成功后，候选会进入这里。</Empty>}<More page={reviews} /></>}{tab === 'jobs' && <><div className="section-title"><h2>实际任务记录</h2><button className="text-button" onClick={jobs.reload}><RefreshCw size={14} />刷新</button></div><ErrorBox error={jobs.error} retry={jobs.reload} />{jobs.loading && <Loading />}{jobs.items.map((item) => <div className="job-row" key={item.id}><div><strong>{({ fetch_source: '信息抓取', parse_document: '正文与附件解析', extract_candidates: '事项提取', reconcile_event: '事项归并', index_event: '搜索索引', plan_reminders: '提醒安排', build_digest: '生成简报', deliver_message: '投递提醒' })[item.kind]}</strong><p className="small muted">{formatStamp(item.run_at)} · 尝试 {item.attempts} 次</p><small className="muted job-id">{item.id}</small>{item.last_error_code && <p className="warning-line">{item.last_error_code}</p>}</div><span className={'tag ' + (item.state === 'failed' ? 'warning' : '')}>{jobLabels[item.state]}</span>{item.state === 'failed' && <button disabled={mutation.busy} onClick={() => mutation.run(() => api.retryJob(item.id), jobs.reload)}>重试</button>}</div>)}{!jobs.loading && !jobs.error && !jobs.items.length && <Empty title="还没有后台任务" />}<More page={jobs} /></>}{tab === 'requests' && isAdmin && <AdminSourceRequests sources={sources} />}{tab === 'publication' && isAdmin && <PublicationForm />}{edit && <SourceEditor source={edit.id ? edit : null} close={() => setEdit(null)} refresh={() => { sources.reload(); app.updated(); }} />}{verify && <VerificationEditor source={verify} close={() => setVerify(null)} refresh={() => { sources.reload(); app.updated(); }} />}</>;
}

export default function App() {
  const [session, setUserSession] = useState(null), [boot, setBoot] = useState(true), [bootError, setBootError] = useState(null);
  const [page, setPage] = useState('today'), [login, setLogin] = useState(false), [detail, setDetail] = useState(null), [editor, setEditor] = useState(null), [refresh, setRefresh] = useState(0), [toast, setToast] = useState(''), [menu, setMenu] = useState(false);
  const toastTimer = useRef(null), sidebarRef = useRef(null), menuButtonRef = useRef(null);
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width: 760px)').matches);
  useEffect(() => {
    const desktop = window.matchMedia('(min-width: 761px)');
    const releaseDrawer = () => { if (desktop.matches) setMenu(false); };
    desktop.addEventListener('change', releaseDrawer);
    return () => desktop.removeEventListener('change', releaseDrawer);
  }, []);
  useEffect(() => {
    const media = window.matchMedia('(max-width: 760px)');
    const sync = () => setNarrow(media.matches);
    media.addEventListener('change', sync);
    return () => media.removeEventListener('change', sync);
  }, []);
  useEffect(() => {
    if (!menu) return;
    sidebarRef.current?.querySelector('nav button')?.focus();
    function key(event) {
      if (document.querySelector('.modal')) return;
      if (event.key === 'Escape') { setMenu(false); menuButtonRef.current?.focus(); }
      if (event.key === 'Tab') {
        const elements = sidebarRef.current?.querySelectorAll('a[href], button:not(:disabled)');
        if (!elements?.length) return;
        const first = elements[0], last = elements[elements.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    }
    document.addEventListener('keydown', key);
    const unlockScroll = lockBodyScroll();
    return () => { document.removeEventListener('keydown', key); unlockScroll(); };
  }, [menu]);
  const catalog = useResource(api.catalog, []);
  const sources = usePages(api.sources, [refresh]);
  const ready = useResource(api.ready, []);
  const logoutMutation = useMutation();
  useEffect(() => {
    let active = true;
    api.me().then((value) => { if (active) { setSession(value); setUserSession(value); } }).catch((error) => { if (active && error.status !== 401) setBootError(error); }).finally(() => active && setBoot(false));
    return () => { active = false; clearTimeout(toastTimer.current); };
  }, []);
  useEffect(() => {
    const label = ({ today: '今天', opportunities: '机会', search: '找信息', my: '我的事项', sources: '信息来源', account: '我的设置', admin: '管理工作台' })[page];
    document.title = (label ? label + ' · ' : '') + '偷闲 · 天大版';
  }, [page]);
  function notify(message) { setToast(message); clearTimeout(toastTimer.current); toastTimer.current = setTimeout(() => setToast(''), 5000); }
  function navigate(next) { setPage(next); if (menu) menuButtonRef.current?.focus(); setMenu(false); window.scrollTo({ top: 0, behavior: 'smooth' }); }
  function updated() { setRefresh((number) => number + 1); }
  const context = { session, catalog: catalog.data, sources: sources.items, sourcesPage: sources, refresh, updated, notify, navigate, openEvent: setDetail, ensureLogin: () => setLogin(true), openImport: (item) => { if (!session) return setLogin(true); setEditor(item || {}); } };
  const admin = session && ['admin', 'operator'].includes(session.user.role);
  const navigation = [['today', '今天', Home], ['opportunities', '机会', Sparkles], ['search', '找信息', Search], ['my', '我的事项', CircleCheck], ['sources', '信息来源', BookOpen], ['account', '我的设置', Settings2], ...(admin ? [['admin', '管理工作台', ShieldCheck]] : [])];
  return <Context.Provider value={context}><div className="app-shell"><a className="skip-link" href="#campus-main">跳到主要内容</a><div id="campus-navigation" ref={sidebarRef} className={'sidebar ' + (menu ? 'open' : '')} role={menu ? 'dialog' : 'complementary'} aria-modal={menu || undefined} aria-label={menu ? '导航菜单' : '站点导航'} {...(narrow && !menu ? { inert: '' } : {})}><div className="sidebar-brand"><a href="/campus.html" className="brand" onClick={(e) => { e.preventDefault(); navigate('today'); }}><span className="brand-mark">闲</span><span><strong>偷闲</strong><small>天津大学 · 校园信息助手</small></span></a><button className="sidebar-close icon-button" aria-label="收起导航菜单" onClick={() => { setMenu(false); menuButtonRef.current?.focus(); }}><X size={21} /></button></div><div className="sidebar-caption">让信息到位，让行动有数</div><nav aria-label="主导航">{navigation.map(([id, label, Icon]) => <button className={page === id ? 'active' : ''} aria-current={page === id ? 'page' : undefined} key={id} onClick={() => navigate(id)}><Icon size={19} />{label}{page === id && <span className="nav-dot" />}</button>)}</nav><div className="sidebar-bottom"><div className="school-label"><span className="school-seal">TJU</span><div>天大生活<small>Weijin Road · Beiyang Park</small></div></div><p>公开信息保留出处<br />个人事项只属于你</p>{session ? <><div className="account-email" title={session.user.email}>{session.user.email}</div><button className="logout" disabled={logoutMutation.busy} onClick={() => logoutMutation.run(api.logout, () => { setSession(null); setUserSession(null); setToast(''); setDetail(null); setEditor(null); navigate('today'); updated(); })}><LogOut size={15} />退出登录</button><ErrorBox error={logoutMutation.error} /></> : <button className="primary" onClick={() => setLogin(true)}>邮箱登录</button>}</div></div><div className="workspace"><header className="topbar"><button ref={menuButtonRef} className="mobile-menu icon-button" aria-label="打开菜单" aria-expanded={menu} aria-controls="campus-navigation" onClick={() => setMenu(!menu)}><Menu size={21} /></button><div className="topbar-title"><span>校园工作台</span><ChevronRight size={14} /><strong>{navigation.find(([id]) => id === page)?.[1]}</strong></div><span className="topbar-note"><ShieldCheck size={14} />有出处，才有把握</span>{session ? <button className="avatar" onClick={() => navigate('account')} aria-label="打开我的设置">{session.user.email[0].toUpperCase()}</button> : <button className="login-top" onClick={() => setLogin(true)}>登录</button>}</header>{ready.data?.demo_data === true && <div className="demo-banner" role="status">合成演示数据 · 用于本地功能验收，不是真实学校通知。</div>}{ready.data?.public_sample === true && <div className="demo-banner" role="status">含截至 2026-10-09 的历史公开样本。来源默认停用，启用并核验采集后再用于正式办理。</div>}<main id="campus-main" className="main-content" tabIndex={-1}>{boot ? <Loading label="正在检查登录状态…" /> : <><ErrorBox error={bootError} retry={() => location.reload()} /><ErrorBox error={catalog.error} retry={catalog.reload} />{page === 'today' && <Today />}{page === 'opportunities' && <Opportunities />}{page === 'search' && <SearchPage />}{page === 'my' && <MyItems />}{page === 'sources' && <SourcesView />}{page === 'account' && <Account />}{page === 'admin' && admin && <Admin />}</>}<footer className="page-footer">偷闲 · 天大版是独立校园信息工具。通知、条件和办理要求请以原发布方为准。</footer></main></div>{menu && <button className="menu-shade" aria-label="关闭菜单" onClick={() => { setMenu(false); menuButtonRef.current?.focus(); }} />}{login && <Login close={() => setLogin(false)} onLogin={(value) => { setUserSession(value); setBootError(null); updated(); }} />}{detail && <EventDetail id={detail} close={() => setDetail(null)} />}{editor && <ItemEditor item={editor.id ? editor : null} close={() => setEditor(null)} />}{toast && <div className="toast" role="status"><Check size={17} />{toast}<button aria-label="关闭提示" onClick={() => setToast('')}><X size={15} /></button></div>}</div></Context.Provider>;
}



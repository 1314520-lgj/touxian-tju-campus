/** Campus dates stay in the school's time zone, independent of the viewer's device. */
// Built once: constructing an Intl formatter per call dominated list filtering.
const campusDayFormat = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' });
export function campusDate(now = new Date()) {
  return campusDayFormat.format(now);
}
export function deadlineHasPassed(deadline, now = new Date()) {
  if (deadline?.precision === 'date' && /^\d{4}-\d{2}-\d{2}$/.test(deadline.date || '')) return deadline.date < campusDate(now);
  if (deadline?.precision === 'minute' && deadline.at) {
    const time = Date.parse(deadline.at);
    return Number.isFinite(time) && time < now.getTime();
  }
  return false;
}
export function eventHasEnded(event, now = new Date()) {
  if (event.status === 'cancelled') return true;
  const deadlines = event.deadlines || [];
  const closing = deadlines.filter((deadline) => deadline.kind !== 'event_time');
  const relevant = closing.length ? closing : deadlines;
  // A missing or uncertain deadline must remain visible for the student to check.
  return relevant.length > 0 && relevant.every((deadline) => deadlineHasPassed(deadline, now));
}
export function personalHasEnded(item, now = new Date()) {
  const draft = item.draft;
  return deadlineHasPassed(draft.deadline_at ? { precision: 'minute', at: draft.deadline_at } : { precision: 'date', date: draft.deadline_date }, now);
}

// A notice may carry several dates: a 报名 window that already closed plus the exam it leads to.
// Both the card label and the list order must lead with the one that still matters, which is the
// rule the API already applies to /me/feed (events.py sort_key). These three helpers keep that
// rule in one place so the anonymous list cannot drift back to "earliest date wins".
function deadlineValue(deadline) { return deadline?.date || deadline?.at || '9999'; }
/** Deadlines with a real date, the still-open ones first (same order the API label is built with). */
export function deadlinesOpenFirst(event, now = new Date()) {
  const today = campusDate(now);
  return [...(event?.deadlines || [])].sort((a, b) => {
    const left = deadlineValue(a), right = deadlineValue(b);
    const leftPassed = left.slice(0, 10) < today, rightPassed = right.slice(0, 10) < today;
    if (leftPassed !== rightPassed) return leftPassed ? 1 : -1;
    return left < right ? -1 : left > right ? 1 : 0;
  });
}
/** The date a card shows: the next one still open, or the notice's own dates once all have closed. */
export function cardDeadline(event, now = new Date()) {
  return deadlinesOpenFirst(event, now)[0] || null;
}
/** Deadlines a card may put in words: dated ones, never the "待确认" ones. */
function labelableDeadlines(event) {
  return (event?.deadlines || []).filter((deadline) => deadline && deadline.precision !== 'unknown' && (deadline.date || deadline.at));
}
/** Cards in display order: the dates a student can still act on, oldest first. A notice whose
 * dates have all passed keeps its dates so the card can show them next to its 已截止 tag. */
export function cardDeadlineList(event, now = new Date()) {
  const dated = labelableDeadlines(event);
  const open = dated.filter((deadline) => !deadlineHasPassed(deadline, now));
  return deadlinesOpenFirst({ deadlines: open.length ? open : dated }, now);
}
/** List key: the next open deadline; undated and fully closed notices sort last as reference material. */
export function nextDeadlineKey(event, now = new Date()) {
  const today = campusDate(now);
  const upcoming = (event?.deadlines || [])
    .map((deadline) => deadlineValue(deadline))
    .filter((value) => value.slice(0, 10) >= today);
  return upcoming.sort()[0] || '9999';
}

// Whole school days (Asia/Shanghai) between today and a still-open deadline, or null.
const dayNumber = (iso) => { const [y, m, d] = iso.split('-').map(Number); return Date.UTC(y, m - 1, d) / 86400000; };
function daysLeft(deadline, now) {
  if (!deadline || deadlineHasPassed(deadline, now)) return null;
  let day = null;
  if (deadline.precision === 'date' && /^\d{4}-\d{2}-\d{2}$/.test(deadline.date || '')) day = deadline.date;
  else if (deadline.precision === 'minute' && Number.isFinite(Date.parse(deadline.at))) day = campusDate(new Date(deadline.at));
  return day ? dayNumber(day) - dayNumber(campusDate(now)) : null;
}
/** Days until the nearest open closing deadline when it is within `window` days; otherwise null. */
export function deadlineUrgency(event, now = new Date(), window = 3) {
  if (event.status && event.status !== 'active') return null;
  const nearest = (event.deadlines || []).filter((deadline) => deadline.kind !== 'event_time')
    .map((deadline) => daysLeft(deadline, now)).filter((days) => days !== null).sort((a, b) => a - b)[0];
  return nearest !== undefined && nearest <= window ? nearest : null;
}
export function personalUrgency(item, now = new Date(), window = 3) {
  const draft = item.draft;
  if (['done', 'archived'].includes(item.state)) return null;
  const days = daysLeft(draft.deadline_at ? { precision: 'minute', at: draft.deadline_at } : draft.deadline_date ? { precision: 'date', date: draft.deadline_date } : null, now);
  return days !== null && days <= window ? days : null;
}
export function urgencyLabel(days) { return days <= 0 ? '今天截止' : days === 1 ? '明天截止' : days + ' 天内截止'; }

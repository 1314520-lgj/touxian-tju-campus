import test from 'node:test';
import assert from 'node:assert/strict';
import { campusDate, cardDeadline, cardDeadlineList, deadlineHasPassed, deadlineUrgency, deadlinesOpenFirst, eventHasEnded, nextDeadlineKey, personalHasEnded, personalUrgency, urgencyLabel } from '../feedView.js';
const now = new Date('2026-10-08T16:10:00Z'); // Oct 9 in Tianjin, Oct 8 in many other zones.
test('school date is Asia/Shanghai rather than the viewer device date', () => {
  assert.equal(campusDate(now), '2026-10-09');
  assert.equal(deadlineHasPassed({ precision: 'date', date: '2026-10-08' }, now), true);
  assert.equal(deadlineHasPassed({ precision: 'date', date: '2026-10-09' }, now), false);
});
test('date-only stays current throughout its school day; precise instants compare in UTC', () => {
  assert.equal(deadlineHasPassed({ precision: 'date', date: '2026-10-09' }, new Date('2026-10-09T15:59:59Z')), false);
  assert.equal(deadlineHasPassed({ precision: 'minute', at: '2026-10-09T00:09:00+08:00' }, now), true);
  assert.equal(deadlineHasPassed({ precision: 'minute', at: '2026-10-09T00:11:00+08:00' }, now), false);
});
test('uncertain and missing deadlines stay visible for checking', () => {
  for (const deadlines of [[], [{ precision: 'unknown', raw_text: '下周五' }], [{ precision: 'minute', at: null }]]) assert.equal(eventHasEnded({ status: 'under_review', deadlines }, now), false);
});
test('cancelled events move to visible history and all actionable deadlines must have passed', () => {
  const past = { kind: 'school_deadline', precision: 'date', date: '2026-10-08' };
  const future = { kind: 'college_deadline', precision: 'date', date: '2026-10-10' };
  assert.equal(eventHasEnded({ status: 'cancelled', deadlines: [] }, now), true);
  assert.equal(eventHasEnded({ status: 'active', deadlines: [past, future] }, now), false);
  assert.equal(eventHasEnded({ status: 'active', deadlines: [past] }, now), true);
  assert.equal(eventHasEnded({ status: 'active', deadlines: [past, { kind: 'event_time', precision: 'date', date: '2026-10-20' }] }, now), true);
});
test('personal overdue items can be grouped without treating missing dates as expired', () => {
  assert.equal(personalHasEnded({ draft: { deadline_date: '2026-10-08', deadline_at: null } }, now), true);
  assert.equal(personalHasEnded({ draft: { deadline_date: null, deadline_at: null } }, now), false);
});
test('deadline urgency counts school days and ignores closed, far or non-deadline dates', () => {
  const active = (deadlines, status = 'active') => ({ status, deadlines });
  const date = (value, kind = 'school_deadline') => ({ kind, precision: 'date', date: value });
  assert.equal(deadlineUrgency(active([date('2026-10-09')]), now), 0);
  assert.equal(deadlineUrgency(active([date('2026-10-10')]), now), 1);
  assert.equal(deadlineUrgency(active([date('2026-10-12')]), now), 3);
  assert.equal(deadlineUrgency(active([date('2026-10-13')]), now), null);
  assert.equal(deadlineUrgency(active([date('2026-10-08')]), now), null);
  assert.equal(deadlineUrgency(active([date('2026-10-09', 'event_time')]), now), null);
  assert.equal(deadlineUrgency(active([date('2026-10-09')], 'cancelled'), now), null);
  assert.equal(deadlineUrgency(active([date('2026-10-12'), date('2026-10-10')]), now), 1);
  assert.equal(deadlineUrgency(active([{ kind: 'school_deadline', precision: 'minute', at: '2026-10-09T18:00:00+08:00' }]), now), 0);
  assert.equal(deadlineUrgency(active([{ kind: 'school_deadline', precision: 'unknown', raw_text: '下周' }]), now), null);
  assert.equal(urgencyLabel(0), '今天截止'); assert.equal(urgencyLabel(1), '明天截止'); assert.equal(urgencyLabel(3), '3 天内截止');
});
test('personal urgency skips finished items and items without dates', () => {
  const item = (draft, state = 'todo') => ({ state, draft: { deadline_at: null, deadline_date: null, ...draft } });
  assert.equal(personalUrgency(item({ deadline_date: '2026-10-10' }), now), 1);
  assert.equal(personalUrgency(item({ deadline_date: '2026-10-10' }, 'done'), now), null);
  assert.equal(personalUrgency(item({}), now), null);
  assert.equal(personalUrgency(item({ deadline_at: '2026-10-09T00:09:00+08:00' }), now), null);
});

// The anonymous first card used to open with a 报名 window that had closed in September while the
// same notice still carried a December exam date. The server already leads with the open date; the
// list kept it only after the client stopped re-sorting by the earliest date it could find.
const cet = {
  status: 'active',
  deadlines: [
    { kind: 'school_deadline', precision: 'minute', at: '2026-09-24T17:00:00+08:00' },
    { kind: 'event_time', precision: 'date', date: '2026-12-12' },
    { kind: 'college_deadline', precision: 'unknown', raw_text: '以学院通知为准' },
  ],
};
test('cards lead with the deadline that still matters, not the first array entry', () => {
  assert.equal(cardDeadline(cet, now).date, '2026-12-12');
  assert.equal(cardDeadline(cet, now).kind, 'event_time');
  // Undated deadlines carry the API's "9999" sentinel, so they sit after real open dates and
  // before the ones that already closed — exactly the order the API builds its label with.
  const open = deadlinesOpenFirst(cet, now).map((d) => d.date || d.at || null);
  assert.deepEqual(open, ['2026-12-12', null, '2026-09-24T17:00:00+08:00']);
  // A notice whose closing dates are all gone still shows its dates, so the card can mark it closed.
  const closed = { status: 'active', deadlines: [{ kind: 'school_deadline', precision: 'date', date: '2026-09-30' }] };
  assert.equal(cardDeadline(closed, now).date, '2026-09-30');
});
test('a card only lists dates a student can still act on, and keeps them all when none is left', () => {
  const mixed = { deadlines: [{ kind: 'school_deadline', precision: 'date', date: '2026-10-25' }, { kind: 'school_deadline', precision: 'date', date: '2026-09-30' }] };
  assert.deepEqual(cardDeadlineList(mixed, now).map((d) => d.date), ['2026-10-25']);
  assert.deepEqual(cardDeadlineList(cet, now).map((d) => d.date), ['2026-12-12']);
  const closed = { deadlines: [{ kind: 'school_deadline', precision: 'date', date: '2026-09-30' }] };
  assert.deepEqual(cardDeadlineList(closed, now).map((d) => d.date), ['2026-09-30']);
  const undated = { deadlines: [{ kind: 'school_deadline', precision: 'unknown', raw_text: '寒假' }, { kind: 'school_deadline', precision: 'minute', at: null }] };
  assert.deepEqual(cardDeadlineList(undated, now), []);
});
test('the list key is the next open deadline, with closed and undated notices last', () => {
  const d = (date) => ({ kind: 'school_deadline', precision: 'date', date });
  assert.equal(nextDeadlineKey(cet, now), '2026-12-12');
  assert.equal(nextDeadlineKey({ deadlines: [d('2026-09-30'), d('2026-11-05')] }, now), '2026-11-05');
  assert.equal(nextDeadlineKey({ deadlines: [d('2026-09-30')] }, now), '9999');
  assert.equal(nextDeadlineKey({ deadlines: [{ kind: 'event_time', precision: 'unknown', raw_text: '寒假' }] }, now), '9999');
  assert.equal(nextDeadlineKey({ deadlines: [] }, now), '9999');
  // A deadline that is today is still open (school-day rule shared with deadlineHasPassed).
  assert.equal(nextDeadlineKey({ deadlines: [d('2026-10-09')] }, now), '2026-10-09');
  const cards = [
    { title: 'undated', deadlines: [] },
    { title: 'closed', deadlines: [d('2026-09-30')] },
    { title: 'far', deadlines: [d('2026-11-05')] },
    { title: 'cet', deadlines: cet.deadlines },
    { title: 'near', deadlines: [d('2026-10-25')] },
  ];
  const order = cards.map((event, i) => ({ event, i, key: nextDeadlineKey(event, now) }))
    .sort((a, b) => a.key.localeCompare(b.key) || a.i - b.i)
    .map((row) => row.event.title);
  assert.deepEqual(order, ['near', 'far', 'cet', 'undated', 'closed']);
});

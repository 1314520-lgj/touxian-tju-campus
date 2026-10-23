"""Supplementary Chinese relative-time parsing.

``content.date_values`` is intentionally conservative: it only speaks when it
finds a deadline cue and a literal date, and it deliberately refuses to invent
an anchor. That leaves a real gap — notices frequently say "下周三下午三点",
"即日起五个工作日内" or "月底前", which carry a usable meaning but no calendar
date at all.

This module fills that gap without ever overriding the existing extractor:

* every candidate is marked ``derived`` and ``needs_review`` so it still passes
  through the human verification queue rather than being trusted outright;
* :func:`supplement` only returns candidates the existing extraction did not
  already cover, and is the function callers should use.

Calendar values require an explicit publication anchor. Term-relative phrases
can still be flagged without it, because the missing term calendar is itself a
reason for review, never a reason to invent a date.

Two rules keep that promise honest rather than merely confident:

* a month qualifier is consumed, not merely matched — 12月底 published in
  October means 31 December, and 下个月5号 means the fifth of the following
  month. Reading only the 月底 and assuming the anchor's month would return a
  well-formed date that is wrong by up to eleven months;
* a phrase anchored to the term (开学后两周内) is reported as needing the term
  start instead of being quietly measured from the publication time.

Holiday and term rules:

* 元旦 and 国庆节 mean 1 January and 1 October. An explicit year is retained;
  otherwise use the nearest occurrence to the publication's Shanghai date.
  今年 / 明年 / 去年 / 后年 / 前年 are consumed as year qualifiers.
  前 names that date boundary, not an invented 23:59 deadline on the day before.
* 上旬 / 中旬 / 下旬 have calendar bounds 1–10 / 11–20 / 21–month end.
  The rich candidate carries ``precision='period'`` and ``date_range`` rather
  than pretending the notice selected one day. A following 前 names the start
  boundary (11月上旬前 → 1 November), still requiring human interpretation.
  Numeric, Chinese and relative month qualifiers are actually consumed.
* 寒假, 暑假 and 学期末 require the institution's term calendar. Report
  ``term_relative_requires_term_start`` with no date, even without publication.
* Compound offsets such as 元旦后三天内 or 下月中旬后三天内 are left unknown
  with ``calendar_relative_requires_review``; they are never publication offsets.
* Every derived entry has ``needs_review=True`` and keeps a source substring.
  The frozen Deadline contract has no period precision or range fields, so
  periods project to ``unknown``; their bounds and reasons reach reviewers via
  the existing warnings field. No API shape or precision enum is extended.
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

NEXT_NEXT_WEEK = re.compile(r"下下周(?P<weekday>[一二三四五六日天])")
DAYS_LATER = re.compile(
    r"(?P<num>\d{1,3}|[一二两三四五六七八九十]{1,3}|半)\s*个?\s*"
    r"(?P<unit>个?工作日|天|日|周|星期|个月|月)\s*(?:之?后|以后|之内|内)"
)
# The qualifier stands *before* the 月 that 月底/月初 already carries: 本月底 is
# 本 + 月底, 十一月底 is 十一 + 月底, 下个月初 is 下个 + 月初. Capturing it is
# not enough — it has to be consumed when the value is computed, otherwise
# 12月底 silently resolves to the anchor's month.
MONTH_QUALIFIER = (
    r"(?P<qual>下下个|下个|下下|下|这个|这|本|"
    r"\d{1,2}|[一二两三四五六七八九十]{1,3})"
)
# The 年 after a year is optional so that 2027年底 keeps its 年 for the 年底 edge
# instead of losing it to the year group and falling back to the anchor's year.
MONTH_EDGE = re.compile(
    r"(?:(?P<year>20\d{2})\s*(?:年\s*)?)?" + MONTH_QUALIFIER + r"?"
    r"(?P<edge>月底|月末|月初|月头|年底|年末|年终|季度末)"
)
# A month qualifier followed by a day of that month: 下个月5号, 下下个月3号,
# 十一月20日. A bare digit month (10月12日) is deliberately left to the primary
# extractor, which already owns literal calendar dates.
MONTH_DAY = re.compile(
    r"(?:(?P<year>20\d{2})\s*年\s*)?"
    r"(?P<qual>下下个|下个|下下|下|这个|这|本|[一二两三四五六七八九十]{1,3})?"
    r"月\s*(?P<day>\d{1,2}|[一二两三四五六七八九十]{1,3})\s*[日号]"
)
MONTH_PERIOD = re.compile(
    r"(?<![\d一二两三四五六七八九十])(?:(?P<year>20\d{2}|今|明|去|后|前)\s*年\s*)?"
    + MONTH_QUALIFIER
    + r"月\s*(?P<period>上旬|中旬|下旬)(?P<before>之?前)?"
)
FIXED_HOLIDAY = re.compile(
    r"(?<!\d)(?:(?P<year>20\d{2}|今|明|去|后|前)\s*年\s*)?"
    r"(?P<holiday>元旦|国庆节?)(?P<before>之?前)?"
)
TERM_RELATIVE = re.compile(r"(?:(?:本|这|下|上)个?)?(?:寒假|暑假|学期末|学期结束)(?:之?前|之?后|期间)?")
RECURRING_WEEKLY = re.compile(r"每\s*(?:个)?\s*周\s*(?P<weekday>[一二三四五六日天])?")
RECURRING_MONTHLY = re.compile(r"每\s*个?\s*月\s*(?:的\s*)?(?P<day>\d{1,2}|[一二两三四五六七八九十]{1,3})\s*[日号]")
# 开学/入学 is a different anchor from the publication time: a notice published in
# October cannot turn "开学后两周内" into a date, so it is reported as needing the
# term start rather than quietly anchored to when the notice was published.
TERM_ANCHOR = re.compile(r"开学|新学期|入学|报到|学期开始|学期初|返校|寒假|暑假|学期末|学期结束")
TERM_WEEK = re.compile(r"第\s*(?P<week>\d{1,2}|[一二两三四五六七八九十]{1,3})\s*(?:教学)?周")
CLOCK = re.compile(r"(?:(?P<period>上午|中午|下午|晚上|晚间)\s*)?(?P<hour>\d{1,2})\s*[:：]\s*(?P<minute>\d{2})")
CLOCK_CN = re.compile(
    r"(?:(?P<period>上午|中午|下午|晚上|晚间)\s*)?(?P<hour>\d{1,2}|[一二两三四五六七八九十]{1,3})\s*点(?P<half>半)?"
)

DEADLINE_HINT = re.compile(r"截止|截至|前完成|前提交|前报送|之内|内完成|逾期")


def cn_number(token):
    """Parse the small Chinese numerals that actually appear in deadlines.

    Returns ``None`` for anything it cannot read confidently rather than
    guessing, and ``0.5`` for 半.
    """
    token = str(token or "").strip()
    if not token:
        return None
    if token == "半":
        return 0.5
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if token.startswith("十"):
        return 10 + CN_DIGITS.get(token[1:2], 0)
    if "十" in token:
        head, _, tail = token.partition("十")
        tens = CN_DIGITS.get(head)
        ones = CN_DIGITS.get(tail, 0) if tail else 0
        return None if tens is None else tens * 10 + ones
    digits = [CN_DIGITS.get(char) for char in token]
    if any(digit is None for digit in digits):
        return None
    if len(digits) == 1:
        return digits[0]
    # 二五 is not a number; refuse instead of concatenating.
    return None


def _resolve_year(token, reference):
    if not token:
        return None
    shift = {"今": 0, "明": 1, "去": -1, "后": 2, "前": -2}.get(token)
    return reference.year + shift if shift is not None else int(token)


def _resolve_month(qualifier, year_token, reference):
    """The ``(year, month)`` a month qualifier names, or ``None`` for no qualifier.

    A bare month number means the *nearest* occurrence of that month rather than
    the next one. A notice published in October that says 12月底 means December,
    one that says 1月底 means the following January, and one that says 9月底
    means the September that has just passed — not eleven months away.
    """
    explicit = _resolve_year(year_token, reference)
    qualifier = (qualifier or "").strip()
    if not qualifier:
        # No qualifier: 月底 belongs to the anchor's own month.
        return (explicit, reference.month) if explicit else None
    if qualifier in {"本", "这个", "这"}:
        return explicit or reference.year, reference.month
    shift = {"下": 1, "下个": 1, "下下": 2, "下下个": 2}.get(qualifier)
    if shift is not None:
        total = reference.month - 1 + shift
        return (explicit or reference.year + total // 12), total % 12 + 1
    number = cn_number(qualifier)
    if number is None or not 1 <= number <= 12:
        return None
    if explicit:
        return explicit, int(number)
    delta = (int(number) - reference.month) % 12
    if delta > 6:
        delta -= 12
    total = reference.month - 1 + delta
    return reference.year + total // 12, total % 12 + 1


def _anchor(anchor):
    """Accept the ISO publication string the pipeline stores, or a datetime.

    A publication time that cannot be read is treated as no anchor at all,
    because a relative phrase without a reference point is not a date.
    """
    if anchor is None:
        return None
    if isinstance(anchor, str):
        try:
            anchor = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
        except ValueError:
            return None
    if anchor.tzinfo is None:
        return anchor.replace(tzinfo=TZ)
    return anchor.astimezone(TZ)


def _clock_after(text, position, base):
    """Attach a clock that immediately follows the matched phrase, if any."""
    tail = text[position : position + 16]
    match = CLOCK.search(tail)
    if match is not None and match.start() <= 6:
        hour, minute = int(match["hour"]), int(match["minute"])
    else:
        match = CLOCK_CN.search(tail)
        if match is None or match.start() > 6:
            return base, "date"
        hour = cn_number(match["hour"])
        if hour is None:
            return base, "date"
        minute = 30 if match["half"] else 0
    period = match["period"]
    if period in {"下午", "晚上", "晚间"} and hour < 12:
        hour += 12
    if period == "中午" and hour < 11:
        hour += 12
    if period == "上午" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return base, "date"
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0), "minute"


def _candidate(text, match, value, rule, precision="date", recurring=False, kind=None, span=None):
    # ``span`` widens the quoted evidence to include the phrase that justifies the
    # rule (开学后两周内 rather than just 两周内), so a reviewer can see the reason.
    start, end = span or (match.start(), match.end())
    raw = text[start:end]
    at = value.isoformat() if value and precision == "minute" else None
    day = value.date().isoformat() if value else None
    return {
        "kind": kind or ("school_deadline" if DEADLINE_HINT.search(text) else "event_time"),
        "raw_text": raw,
        "precision": precision if value else "unknown",
        "at": at,
        "date": day,
        "timezone": "Asia/Shanghai",
        "evidence_ids": [],
        "rule": rule,
        "derived": True,
        "recurring": recurring,
        "needs_review": True,
    }


def candidates(text, anchor):
    """Every relative-time candidate this module can justify, in text order."""
    reference = _anchor(anchor)
    if not text:
        return []
    found = []

    for match in TERM_RELATIVE.finditer(text):
        entry = _candidate(text, match, None, "term_relative_requires_term_start")
        entry["requires_term_start"] = True
        entry["review_reason"] = "缺少学校学期或假期日历，不能按通知发布时间推算日期"
        found.append((match.start(), entry))

    if reference is None:
        for pattern in (FIXED_HOLIDAY, MONTH_PERIOD):
            for match in pattern.finditer(text):
                entry = _candidate(text, match, None, "publication_anchor_required")
                entry["requires_publication_anchor"] = True
                entry["review_reason"] = "缺少有效的通知发布时间，无法确定该相对时间的年份或月份"
                found.append((match.start(), entry))
        return [entry for _, entry in sorted(found, key=lambda item: item[0])]

    for match in FIXED_HOLIDAY.finditer(text):
        month = 1 if match["holiday"] == "元旦" else 10
        years = (
            [_resolve_year(match["year"], reference)]
            if match["year"]
            else range(reference.year - 1, reference.year + 2)
        )
        dates = [datetime(year, month, 1, tzinfo=TZ) for year in years if 1 <= year <= 9999]
        if not dates:
            continue
        value = min(dates, key=lambda day: abs((day.date() - reference.date()).days))
        entry = _candidate(
            text, match, value, "fixed_gregorian_holiday", kind="school_deadline" if match["before"] else None
        )
        if match["before"]:
            entry["boundary"] = "before"
        entry["review_reason"] = "固定公历节日边界，年份及具体截止含义仍需人工核对"
        found.append((match.start(), entry))

    for match in MONTH_PERIOD.finditer(text):
        resolved = _resolve_month(match["qual"], match["year"], reference)
        if resolved is None:
            continue
        year, month = resolved
        first, last = {"上旬": (1, 10), "中旬": (11, 20), "下旬": (21, calendar.monthrange(year, month)[1])}[
            match["period"]
        ]
        start = datetime(year, month, first, tzinfo=TZ)
        end = datetime(year, month, last, tzinfo=TZ)
        entry = _candidate(
            text,
            match,
            start if match["before"] else None,
            "month_period_requires_review",
            kind="school_deadline" if match["before"] else None,
        )
        entry["date_range"] = {"start": start.date().isoformat(), "end": end.date().isoformat()}
        if match["before"]:
            entry["boundary"] = "before"
            entry["review_reason"] = "以旬的首日作为“前”的边界，具体截止含义需人工核对"
        else:
            entry["precision"] = "period"
            entry["review_reason"] = f"原文仅确定{start.date().isoformat()}至{end.date().isoformat()}，未指定具体日期"
        found.append((match.start(), entry))

    for match in NEXT_NEXT_WEEK.finditer(text):
        value = reference + timedelta(days=(WEEKDAYS[match["weekday"]] - reference.weekday()) + 14)
        value = value.replace(hour=0, minute=0, second=0, microsecond=0)
        value, precision = _clock_after(text, match.end(), value)
        found.append((match.start(), _candidate(text, match, value, "next_next_week", precision)))

    for match in DAYS_LATER.finditer(text):
        count = cn_number(match["num"])
        if count is None:
            continue
        calendar_anchor = next(
            (
                (start, entry)
                for start, entry in found
                if entry["rule"] in {"fixed_gregorian_holiday", "month_period_requires_review"}
                and start + len(entry["raw_text"]) <= match.start()
                and text[start + len(entry["raw_text"]) : match.start()].strip() in {"", "前", "之前", "后", "之后"}
            ),
            None,
        )
        if calendar_anchor is not None:
            # A count following a holiday/period belongs to that calendar phrase,
            # never to publication. Do not expose its anchor as a final deadline.
            start, _ = calendar_anchor
            found.remove(calendar_anchor)
            entry = _candidate(text, match, None, "calendar_relative_requires_review", span=(start, match.end()))
            entry["requires_calendar_anchor"] = True
            entry["review_reason"] = "节日或旬后的偏移需人工确认，不能按通知发布时间计数"
            found.append((start, entry))
            continue
        window_start = max(0, match.start() - 8)
        term_anchor = TERM_ANCHOR.search(text[window_start : match.start()])
        if term_anchor is not None:
            # 开学后两周内 is measured from the term start, which this module does
            # not know. Reporting the phrase without a date is honest; anchoring
            # it to the publication time would be a confident wrong answer.
            start = window_start + term_anchor.start()
            entry = _candidate(text, match, None, "term_relative_requires_term_start", span=(start, match.end()))
            entry["requires_term_start"] = True
            found.append((start, entry))
            continue
        unit = match["unit"]
        approximate = False
        if "工作日" in unit:
            days = int(count * 7 / 5)  # business days, expressed as calendar days
            approximate = True
        elif unit in {"周", "星期"}:
            days = count * 7
        elif unit in {"个月", "月"}:
            days = round(count * 30)
            approximate = True
        else:
            days = count
        value = (reference + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        value, precision = _clock_after(text, match.end(), value)
        entry = _candidate(text, match, value, "business_days_approximated" if approximate else "days_after", precision)
        entry["approximate"] = approximate
        # These counts are measured from the publication time, not from the event
        # they are attached to; say so instead of leaving the anchor implicit.
        entry["anchor"] = "publication"
        found.append((match.start(), entry))

    for match in MONTH_EDGE.finditer(text):
        edge = match["edge"]
        resolved = _resolve_month(match["qual"], match["year"], reference)
        year, month = resolved or (reference.year, reference.month)
        if edge in {"月底", "月末"}:
            value = datetime(year, month, calendar.monthrange(year, month)[1], tzinfo=TZ)
        elif edge in {"月初", "月头"}:
            value = datetime(year, month, 1, tzinfo=TZ)
        elif edge in {"年底", "年末", "年终"}:
            value = datetime(year, 12, 31, tzinfo=TZ)
        else:
            quarter_end = (month - 1) // 3 * 3 + 3
            value = datetime(year, quarter_end, calendar.monthrange(year, quarter_end)[1], tzinfo=TZ)
        found.append((match.start(), _candidate(text, match, value, "month_or_year_edge")))

    for match in MONTH_DAY.finditer(text):
        day = cn_number(match["day"])
        resolved = _resolve_month(match["qual"], match["year"], reference)
        # Without a qualifier this is a bare 月12日 fragment of a literal date the
        # primary extractor already owns; leave it alone.
        if day is None or resolved is None or not 1 <= day <= 31:
            continue
        year, month = resolved
        try:
            value = datetime(year, month, int(day), tzinfo=TZ)
        except ValueError:
            continue
        value, precision = _clock_after(text, match.end(), value)
        found.append((match.start(), _candidate(text, match, value, "month_day_qualified", precision)))

    for match in RECURRING_MONTHLY.finditer(text):
        day = cn_number(match["day"])
        if day is None or not 1 <= day <= 31:
            continue
        entry = _candidate(text, match, None, "recurring_monthly", recurring=True)
        entry["day_of_month"] = day
        found.append((match.start(), entry))

    for match in RECURRING_WEEKLY.finditer(text):
        weekday = match["weekday"]
        entry = _candidate(text, match, None, "recurring_weekly", recurring=True)
        if weekday:
            entry["weekday"] = WEEKDAYS[weekday]
        found.append((match.start(), entry))

    for match in TERM_WEEK.finditer(text):
        week = cn_number(match["week"])
        if week is None or not 1 <= week <= 30:
            continue
        # A term week cannot be resolved without the term start date; report the
        # week number and let the reviewer supply the anchor.
        entry = _candidate(text, match, None, "term_week_requires_term_start")
        entry["term_week"] = week
        found.append((match.start(), entry))

    found.sort(key=lambda item: item[0])
    return [entry for _, entry in found]


def _covered(existing):
    """Identity keys already established by the primary extractor."""
    return {(value.get("date"), value.get("at")) for value in existing or [] if value.get("date") or value.get("at")}


# The shared Deadline contract accepts exactly these keys and nothing else.
DEADLINE_KEYS = ("kind", "raw_text", "precision", "at", "date", "timezone", "evidence_ids")


def to_deadline(entry, evidence_id):
    """Project a rich candidate onto the frozen Deadline contract."""
    projected = {key: entry.get(key) for key in DEADLINE_KEYS}
    if projected["precision"] == "period":
        projected.update(precision="unknown", date=None, at=None)
    elif projected["precision"] == "minute":
        projected["date"] = None
    projected["evidence_ids"] = [evidence_id] if evidence_id else []
    return projected


def supplement(text, anchor, existing, evidence_id):
    """Return only the candidates the primary extractor did not already cover.

    Anything the primary extractor resolved is trusted as-is; this function
    never re-states or contradicts it. Candidates that carry no date at all are
    dropped unless they carry a recurrence, term reference or calendar range.
    An unknown primary row cannot suppress a more informative review reason.

    Results are projected onto the Deadline contract so a caller can merge them
    directly, and every one of them still enters the human review queue.
    """
    covered = _covered(existing)
    result = []
    for entry in candidates(text, anchor):
        projected = to_deadline(entry, evidence_id)
        if (projected.get("date"), projected.get("at")) in covered:
            continue
        # A resolved value, a recurrence, a term week or a term-relative phrase all
        # carry reviewable meaning even without a calendar date. Everything else is
        # dropped rather than passed on as an empty deadline.
        if (
            not entry.get("date")
            and not entry.get("recurring")
            and "term_week" not in entry
            and not entry.get("requires_term_start")
            and not entry.get("date_range")
            and not entry.get("requires_calendar_anchor")
            and not entry.get("requires_publication_anchor")
        ):
            continue
        result.append(projected)
    return result

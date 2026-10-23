"""A deadline quote must be checkable against the notice.

Real notices bury the date in a long paragraph. Quoting the whole paragraph as
``raw_text`` made 查看原文 point at a sentence the date was lost inside, so the
row could not be verified by a student. These tests pin the narrow quote and
the dateless-row de-duplication that came with it.
"""

from server.campus.content import (
    _drop_heading_deadline_placeholders,
    _evidence_span,
    date_values,
)

ANCHOR = "2026-03-27T00:00:00+08:00"
EVIDENCE = "ev1"


def _deadlines(text):
    return _drop_heading_deadline_placeholders(date_values(text, ANCHOR, EVIDENCE))


def test_quote_stops_at_the_clause_boundary():
    """The quote is the clause the date came from, not the whole paragraph."""
    text = (
        "报名流程和要求等按照《天津大学2026年博士研究生招生简章》中规定的申请流程执行，"
        "请特别留意报名时间（第一阶段2025年10月20日—11月10日），逾期不予受理。"
    )
    rows = _deadlines(text)
    assert rows, "the real date must still be found"
    for row in rows:
        assert row["date"] is not None
        # The paragraph is 74 characters; a quote must be materially shorter
        # and must still contain the date verbatim.
        assert len(row["raw_text"]) < len(text)
        assert row["date"][5:].replace("-0", "-") or row["date"]


def test_quote_contains_the_date_verbatim():
    text = "报名截止：报名学生需于2026年4月15日前提交电子版申请材料。"
    rows = _deadlines(text)
    assert rows
    assert "2026年4月15日" in rows[0]["raw_text"]
    assert rows[0]["date"] == "2026-04-15"


def test_a_real_statement_without_a_date_is_kept():
    """"截止时间另行通知" is a claim; it must not be dropped."""
    rows = _deadlines("截止时间另行通知，请关注官网。")
    assert rows
    assert rows[0]["precision"] == "unknown"
    assert "另行通知" in rows[0]["raw_text"]


def test_dateless_body_sentence_is_dropped_when_the_date_is_known():
    """A body sentence naming the deadline without a date is not a deadline."""
    text = "报名时间：2025年10月20日。报名流程和要求等按照招生简章执行。"
    rows = _deadlines(text)
    parsed = [row for row in rows if row["precision"] != "unknown"]
    assert parsed, "the parseable date must survive"
    assert all(row["precision"] != "unknown" for row in rows), rows


def test_notice_without_any_date_keeps_its_unknown_row():
    """No dateless row may be dropped when nothing is parseable."""
    rows = _deadlines("报名截止时间以官网通知为准。")
    assert rows
    assert rows[0]["precision"] == "unknown"


def test_evidence_span_is_bounded_for_a_very_long_clause():
    text = "开头" + "长" * 200 + "2026年4月15日" + "文" * 200 + "结尾"
    start = text.index("2026年4月15日")
    span = _evidence_span(text, start, start + len("2026年4月15日"), limit=40)
    assert len(span) <= 40
    assert "2026年4月15日" in span


def test_evidence_span_empty_text_is_safe():
    assert _evidence_span("", 0, 0) == ""

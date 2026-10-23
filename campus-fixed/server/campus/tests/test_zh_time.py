"""Relative-time supplement: additive, reviewable and never overriding."""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from server.campus import zh_time
from server.campus.content import date_values
from server.campus.platform import APIError
from server.campus.schema import uid, validate
from server.campus.tests import test_pipeline as pipeline_fixtures

TZ = timezone(timedelta(hours=8))
ANCHOR = datetime(2026, 10, 8, 12, 0, tzinfo=TZ)  # a Thursday


def test_cn_number_reads_only_unambiguous_numerals():
    assert zh_time.cn_number("三") == 3
    assert zh_time.cn_number("十") == 10
    assert zh_time.cn_number("十五") == 15
    assert zh_time.cn_number("二十") == 20
    assert zh_time.cn_number("两") == 2
    assert zh_time.cn_number("半") == 0.5
    assert zh_time.cn_number("二五") is None
    assert zh_time.cn_number("abc") is None


def test_no_anchor_means_no_candidate():
    assert zh_time.candidates("下周三下午三点截止", None) == []


def test_next_next_week_resolves_two_weeks_ahead():
    rows = zh_time.candidates("下下周三下午三点前提交", ANCHOR)
    entry = next(row for row in rows if row["rule"] == "next_next_week")
    # 2026-10-08 is a Thursday; the Wednesday of the week after next is 10-21.
    assert entry["date"] == "2026-10-21"
    assert entry["precision"] == "minute"
    assert entry["at"].endswith("15:00:00+08:00")


def test_business_days_are_marked_approximate():
    rows = zh_time.candidates("即日起五个工作日内提交", ANCHOR)
    entry = next(row for row in rows if row["rule"] == "business_days_approximated")
    assert entry["approximate"] is True
    assert entry["date"] == "2026-10-15"


def test_calendar_days_are_not_marked_approximate():
    rows = zh_time.candidates("报名后十天内提交", ANCHOR)
    entry = next(row for row in rows if row["rule"] == "days_after")
    assert entry["approximate"] is False
    assert entry["date"] == "2026-10-18"


def test_month_end_resolves_to_last_day_of_month():
    rows = zh_time.candidates("请于本月底前完成", ANCHOR)
    entry = next(row for row in rows if row["rule"] == "month_or_year_edge")
    assert entry["date"] == "2026-10-31"


def test_year_end_resolves_to_december_31():
    rows = zh_time.candidates("年底前完成结项", ANCHOR)
    entry = next(row for row in rows if row["rule"] == "month_or_year_edge")
    assert entry["date"] == "2026-12-31"


def test_recurring_weekly_is_flagged_and_carries_no_invented_date():
    rows = zh_time.candidates("每周二下午举行组会", ANCHOR)
    entry = next(row for row in rows if row["rule"] == "recurring_weekly")
    assert entry["recurring"] is True
    assert entry["weekday"] == 1
    assert entry["date"] is None
    assert entry["precision"] == "unknown"


def test_term_week_reports_the_week_without_guessing_a_date():
    rows = zh_time.candidates("第8周进行期中考核", ANCHOR)
    entry = next(row for row in rows if row["rule"] == "term_week_requires_term_start")
    assert entry["term_week"] == 8
    assert entry["date"] is None


def test_every_candidate_requires_review():
    for entry in zh_time.candidates("下周三下午三点截止，月底前提交", ANCHOR):
        assert entry["derived"] is True
        assert entry["needs_review"] is True
        assert entry["timezone"] == "Asia/Shanghai"


def test_supplement_projects_onto_the_frozen_deadline_contract():
    rows = zh_time.supplement("下下周三下午三点前提交", ANCHOR, [], "ev-1")
    assert rows
    for row in rows:
        assert set(row) == set(zh_time.DEADLINE_KEYS)
        assert row["evidence_ids"] == ["ev-1"]
        assert row["kind"] in {"school_deadline", "college_deadline", "event_time", "other"}
        assert row["precision"] in {"date", "minute", "unknown"}


def test_supplement_never_restates_what_the_primary_extractor_found():
    existing = [
        {
            "kind": "school_deadline",
            "raw_text": "月底",
            "precision": "date",
            "at": None,
            "date": "2026-10-31",
            "timezone": "Asia/Shanghai",
            "evidence_ids": ["ev-1"],
        }
    ]
    rows = zh_time.supplement("请于本月底前完成", ANCHOR, existing, "ev-2")
    assert rows == []


def test_supplement_still_adds_a_distinct_date_alongside_an_existing_one():
    existing = [
        {
            "kind": "school_deadline",
            "raw_text": "10月10日",
            "precision": "date",
            "at": None,
            "date": "2026-10-10",
            "timezone": "Asia/Shanghai",
            "evidence_ids": ["ev-1"],
        }
    ]
    rows = zh_time.supplement("10月10日开始，下下周三下午三点截止", ANCHOR, existing, "ev-2")
    # A minute-precision Deadline must use at and leave date null (frozen schema).
    assert [(row["date"], row["at"]) for row in rows] == [(None, "2026-10-21T15:00:00+08:00")]


def test_absolute_dates_are_left_to_the_primary_extractor():
    # A literal date with no relative phrasing must not produce a derived row.
    rows = zh_time.supplement("截止时间：2026年10月12日", ANCHOR, [], "ev-1")
    assert rows == []


def test_month_qualifier_is_consumed_not_swallowed():
    # 12月底 published in October means December. Matching only the 月底 and
    # assuming the anchor's month returns a well-formed date that is months wrong,
    # which is worse than returning nothing at all.
    for text, expected in [
        ("十一月底前提交材料", "2026-11-30"),
        ("十月底前完成申报", "2026-10-31"),
        ("请在12月底之前反馈", "2026-12-31"),
        ("9月底前完成", "2026-09-30"),
        ("四月底前完成", "2027-04-30"),
    ]:
        entry = next(row for row in zh_time.candidates(text, ANCHOR) if row["rule"] == "month_or_year_edge")
        assert entry["date"] == expected, text


def test_bare_month_number_takes_the_nearest_occurrence():
    # January is nearer going forward, September is nearer going back.
    assert zh_time.candidates("1月底前完成", ANCHOR)[0]["date"] == "2027-01-31"
    assert zh_time.candidates("9月底前完成", ANCHOR)[0]["date"] == "2026-09-30"


def test_year_qualified_month_edge_keeps_its_year():
    # 2027年底 must not lose the 年 to the year group and fall back to the anchor.
    entry = zh_time.candidates("2027年底前完成", ANCHOR)[0]
    assert entry["raw_text"] == "2027年底"
    assert entry["date"] == "2027-12-31"


def test_relative_month_with_a_day_resolves():
    assert zh_time.candidates("下个月5号前报送", ANCHOR)[0]["date"] == "2026-11-05"
    assert zh_time.candidates("下下个月3号前", ANCHOR)[0]["date"] == "2026-12-03"
    assert zh_time.candidates("本月20日前", ANCHOR)[0]["date"] == "2026-10-20"


def test_recurring_monthly_accepts_the_possessive():
    entry = next(row for row in zh_time.candidates("每月的15日截止", ANCHOR) if row["rule"] == "recurring_monthly")
    assert entry["day_of_month"] == 15
    assert entry["date"] is None


def test_term_relative_is_not_anchored_to_publication():
    # 开学后两周内 is measured from the term start. Anchoring it to the publication
    # time would be a confident answer that is months wrong.
    entry = zh_time.candidates("开学后两周内提交", ANCHOR)[0]
    assert entry["rule"] == "term_relative_requires_term_start"
    assert entry["date"] is None
    assert entry["raw_text"] == "开学后两周内"


def test_term_relative_still_reaches_the_review_queue():
    rows = zh_time.supplement("开学后两周内提交", ANCHOR, [], "ev-1")
    assert rows and rows[0]["precision"] == "unknown"


def test_literal_month_day_still_belongs_to_the_primary_extractor():
    for text in ("截止时间：2026年10月12日", "报名截止时间：11月20日"):
        assert zh_time.supplement(text, ANCHOR, [], "ev-1") == []


def test_publication_anchored_counts_say_so():
    entry = next(row for row in zh_time.candidates("报名后十天内提交", ANCHOR) if row["rule"] == "days_after")
    assert entry["anchor"] == "publication"


# Task 2: exercise the literal extractor, supplement and stored review candidate.
TASK2_CASES = [
    ("元旦前", "2027-01-01", "fixed_gregorian_holiday"),
    ("国庆节前", "2026-10-01", "fixed_gregorian_holiday"),
    ("寒假前", None, "term_relative_requires_term_start"),
    ("暑假前", None, "term_relative_requires_term_start"),
    ("十月中旬", None, "month_period_requires_review"),
    ("下月中旬", None, "month_period_requires_review"),
    ("11月上旬前", "2026-11-01", "month_period_requires_review"),
    ("本学期末", None, "term_relative_requires_term_start"),
]


@pytest.mark.parametrize(("phrase", "expected", "rule"), TASK2_CASES)
def test_task2_full_extraction_chain(phrase, expected, rule):
    text = f"请于{phrase}提交申请表"
    anchor = ANCHOR.isoformat()
    evidence_id = uid()
    primary = date_values(text, anchor, evidence_id)
    derived = zh_time.supplement(text, anchor, primary, evidence_id)
    entries = zh_time.candidates(text, anchor)
    assert derived, phrase  # An unknown primary row must not suppress the rule.
    assert entries and all(entry["needs_review"] is True for entry in entries)
    assert entries[0]["rule"] == rule
    assert {row["date"] for row in derived} == {expected}
    for row in primary + derived:
        validate("Deadline", row)
        assert row["raw_text"] in text
        assert row["evidence_ids"] == [evidence_id]
    if rule == "term_relative_requires_term_start":
        assert entries[0]["requires_term_start"] is True
        assert all(row["precision"] == "unknown" and row["at"] is None for row in derived)


@pytest.mark.parametrize(
    ("text", "anchor", "expected"),
    [
        ("元旦前", "2026-12-28", "2027-01-01"),
        ("元旦前", "2027-01-03", "2027-01-01"),
        ("国庆节前", "2026-09-20", "2026-10-01"),
        ("国庆节前", "2026-10-10", "2026-10-01"),
        ("2027年国庆节前", "2026-10-10", "2027-10-01"),
        ("2026年元旦前", "2026-12-28", "2026-01-01"),
        ("元旦前", "2026-12-31T16:30:00Z", "2027-01-01"),
    ],
)
def test_task2_holiday_calendar_boundary(text, anchor, expected):
    (entry,) = zh_time.candidates(text, anchor)
    assert (entry["date"], entry["precision"], entry["at"]) == (expected, "date", None)
    assert entry["needs_review"] is True
    assert entry["raw_text"] == text
    assert entry["boundary"] == "before"


@pytest.mark.parametrize(
    ("text", "anchor", "start", "end"),
    [
        ("十月中旬", "2026-10-10", "2026-10-11", "2026-10-20"),
        ("下月中旬", "2026-12-28", "2027-01-11", "2027-01-20"),
        ("11月上旬", "2026-10-10", "2026-11-01", "2026-11-10"),
        ("二月下旬", "2028-02-01", "2028-02-21", "2028-02-29"),
        ("二月下旬", "2027-02-01", "2027-02-21", "2027-02-28"),
        ("2028年2月下旬", "2026-10-10", "2028-02-21", "2028-02-29"),
    ],
)
def test_task2_month_period_is_a_range_not_an_invented_day(text, anchor, start, end):
    (entry,) = zh_time.candidates(text, anchor)
    assert entry["precision"] == "period"
    assert entry["date_range"] == {"start": start, "end": end}
    assert entry["date"] is None and entry["at"] is None
    assert entry["needs_review"] is True
    assert start in entry["review_reason"] and end in entry["review_reason"]
    (projected,) = zh_time.supplement(text, anchor, [], uid())
    validate("Deadline", projected)
    assert projected["precision"] == "unknown"
    assert projected["date"] is None and projected["at"] is None


@pytest.mark.parametrize("anchor", [None, "not-a-date"])
@pytest.mark.parametrize("text", ["寒假前", "暑假前", "本学期末"])
def test_task2_term_flag_does_not_require_a_publication_date(text, anchor):
    (entry,) = zh_time.candidates(text, anchor)
    assert entry["rule"] == "term_relative_requires_term_start"
    assert entry["requires_term_start"] and entry["needs_review"]
    assert entry["date"] is None and entry["at"] is None
    assert entry["raw_text"] == text


@pytest.mark.parametrize("text", ["元旦前", "下月中旬", "十月中旬"])
def test_task2_calendar_phrases_without_year_anchor_never_guess(text):
    assert not any(row["date"] or row["at"] for row in zh_time.candidates(text, None))


@pytest.mark.parametrize("text", ["0月中旬", "13月上旬前", "二五月中旬"])
def test_task2_invalid_month_is_not_replaced_by_publication_month(text):
    assert zh_time.candidates(text, ANCHOR) == []


@pytest.fixture
def task2_pipeline():
    case = pipeline_fixtures.PipelineTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


@pytest.mark.parametrize(("phrase", "expected", "rule"), TASK2_CASES)
def test_task2_pipeline_stores_pending_evidence_with_reason(task2_pipeline, phrase, expected, rule):
    case = task2_pipeline
    text = f"全体本科生请于{phrase}提交申请表。"
    snapshot = case.notice(text)
    original = (case.core.data_dir / snapshot["object_key"]).read_bytes()
    candidate = case.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    validate("ExtractionCandidate", candidate)
    assert candidate["review_status"] == "pending" and candidate["review_required"]
    assert rule in candidate["warnings"]
    assert "relative_time_requires_review" in candidate["warnings"]
    dates = {row["date"] for row in candidate["deadlines"] if row["date"]}
    assert dates == ({expected} if expected else set())
    for row in candidate["deadlines"]:
        assert row["raw_text"] in text
    for evidence in candidate["evidence"]:
        assert evidence["quote"] in original.decode()
    assert (case.core.data_dir / snapshot["object_key"]).read_bytes() == original
    assert not case.core.store.list("events")
    assert not any(event["topic"] == "candidate.accepted" for event in case.core.store.pending_outbox())
    if phrase in {"十月中旬", "下月中旬"}:
        assert any("11" in warning and "20" in warning for warning in candidate["warnings"])


def test_task2_resolved_literal_does_not_hide_term_review(task2_pipeline):
    case = task2_pipeline
    snapshot = case.notice("全体本科生请于10月12日前提交申请表，寒假前提交成绩单。")
    candidate = case.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    assert "term_relative_requires_term_start" in candidate["warnings"]
    assert candidate["review_status"] == "pending"
    assert any(row["date"] == "2026-10-12" for row in candidate["deadlines"])
    assert any("寒假前" in row["raw_text"] and row["precision"] == "unknown" for row in candidate["deadlines"])


@pytest.mark.parametrize("phrase", ["元旦前", "十月中旬", "本学期末"])
def test_task2_review_cannot_invent_a_derived_deadline(task2_pipeline, phrase):
    case = task2_pipeline
    snapshot = case.notice(f"全体本科生请于{phrase}提交申请表。")
    candidate = case.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    replacement = copy.deepcopy(candidate)
    replacement["deadlines"][0].update(precision="date", date="2026-10-15", at=None)
    with pytest.raises(APIError) as caught:
        case.core.pipeline.review(
            candidate["candidate_id"],
            {
                "expected_candidate_revision": 1,
                "decision": "accept",
                "reason": "synthetic invented date",
                "replacement_candidate": replacement,
            },
            {"id": uid()},
        )
    assert caught.value.code == "fabricated_deadline"


def test_task2_fixed_holiday_can_be_manually_reviewed(task2_pipeline):
    case = task2_pipeline
    snapshot = case.notice("全体本科生请于元旦前提交申请表。")
    candidate = case.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    assert any(row["date"] == "2027-01-01" for row in candidate["deadlines"])
    accepted = case.core.pipeline.review(
        candidate["candidate_id"],
        {
            "expected_candidate_revision": 1,
            "decision": "accept",
            "reason": "synthetic verified boundary",
            "replacement_candidate": None,
        },
        {"id": uid()},
    )
    assert accepted["review_status"] == "accepted"
    assert not case.core.store.list("events")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("今年元旦前", "2026-01-01"),
        ("明年国庆节前", "2027-10-01"),
        ("去年国庆节前", "2025-10-01"),
        ("后年元旦前", "2028-01-01"),
        ("前年元旦前", "2024-01-01"),
        ("明年11月上旬前", "2027-11-01"),
    ],
)
def test_task2_year_qualifiers_are_consumed(text, expected):
    (entry,) = zh_time.candidates(text, ANCHOR)
    assert entry["date"] == expected
    assert entry["raw_text"] == text


@pytest.mark.parametrize("text", ["元旦后三天内", "国庆节后五个工作日内", "下月中旬后三天内"])
def test_task2_offsets_from_calendar_phrases_never_use_publication(text):
    entries = zh_time.candidates(text, ANCHOR)
    assert entries and all(entry["date"] is None and entry["at"] is None for entry in entries)
    assert entries[0]["rule"] == "calendar_relative_requires_review"
    assert entries[0]["raw_text"] == text
    assert zh_time.supplement(text, ANCHOR, [], uid())[0]["precision"] == "unknown"


def test_task2_minute_supplement_matches_deadline_contract():
    (row,) = zh_time.supplement("下下周三下午三点前提交", ANCHOR, [], uid())
    validate("Deadline", row)
    assert row["date"] is None
    assert row["at"] == "2026-10-21T15:00:00+08:00"


def test_task2_legacy_month_edge_uses_full_evidence_for_validation(task2_pipeline):
    case = task2_pipeline
    snapshot = case.notice("全体本科生请于本月底前提交申请表。")
    candidate = case.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    assert candidate["review_status"] == "pending"
    assert any(row["date"] == "2026-10-31" for row in candidate["deadlines"])


def test_task2_minute_supplement_does_not_duplicate_literal_clock():
    evidence_id = uid()
    existing = [
        {
            "kind": "school_deadline",
            "raw_text": "截止2026年10月21日15:00",
            "precision": "minute",
            "at": "2026-10-21T15:00:00+08:00",
            "date": None,
            "timezone": "Asia/Shanghai",
            "evidence_ids": [evidence_id],
        }
    ]
    assert zh_time.supplement("下下周三下午三点前提交", ANCHOR, existing, evidence_id) == []


@pytest.mark.parametrize("phrase", ["元旦前", "十月中旬"])
def test_task2_missing_publication_keeps_calendar_phrase_reviewable(task2_pipeline, phrase):
    case = task2_pipeline
    snapshot = case.notice(f"全体本科生请于{phrase}提交申请表。", published=None)
    candidate = case.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    assert candidate["review_status"] == "pending"
    assert "publication_anchor_required" in candidate["warnings"]
    assert candidate["deadlines"]
    assert all(row["date"] is None and row["at"] is None for row in candidate["deadlines"])

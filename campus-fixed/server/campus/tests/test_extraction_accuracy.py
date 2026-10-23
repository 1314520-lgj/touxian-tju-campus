"""Synthetic counterexamples for conservative extraction; not real-world accuracy."""

import copy

import pytest

from server.campus.content import date_values, material_values
from server.campus.platform import APIError
from server.campus.schema import uid
from server.campus.sources import FetchResult
from server.campus.tests import test_pipeline as pipeline_fixtures


@pytest.fixture
def pipeline():
    case = pipeline_fixtures.PipelineTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def extract(case, text, title="合成申请通知", published="2026-10-08T12:00:00+08:00"):
    snapshot = case.notice(text, title=title, published=published)
    return case.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})


def test_range_with_omitted_month_selects_explicit_end():
    rows = date_values("报名时间：2026年10月10日至12日", None, uid())
    assert [(row["date"], row["precision"]) for row in rows] == [("2026-10-12", "date")]


def test_explicit_cross_year_range_inherits_year_from_start():
    rows = date_values("申请时间：2026年12月25日至1月5日", None, uid())
    assert [row["date"] for row in rows] == ["2027-01-05"]


def test_cross_year_month_day_uses_nearest_year():
    rows = date_values("请于1月5日前提交申请表", "2026-12-25T12:00:00+08:00", uid())
    assert [(row["date"], row["precision"]) for row in rows] == [("2027-01-05", "date")]


@pytest.mark.parametrize(
    ("published", "text", "expected"),
    [
        ("2026-12-28", "请于1月5日前提交", ["2027-01-05"]),
        ("2027-01-03", "截止日期：12月30日", ["2026-12-30"]),
        ("2026-10-10", "截止日期：10月12日", ["2026-10-12"]),
        ("2026-12-28", "报名时间：12月30日至1月3日", ["2027-01-03"]),
        ("2027-01-03", "活动时间：12月30日至1月3日", ["2026-12-30", "2027-01-03"]),
        ("2027-12-28", "截止日期：2月29日", ["2028-02-29"]),
        ("2027-08-30", "截止日期：2月29日", ["2028-02-29"]),  # Exactly 183 days.
    ],
)
def test_yearless_dates_select_nearest_valid_date(published, text, expected):
    evidence_id = uid()
    rows = date_values(text, published, evidence_id)
    assert [row["date"] for row in rows] == expected
    assert all(row["precision"] == "date" for row in rows)
    assert all(row["raw_text"] == text and row["evidence_ids"] == [evidence_id] for row in rows)


@pytest.mark.parametrize(
    ("published", "text"),
    [
        (None, "请于1月5日前提交"),
        ("2026-12-28", "截止日期：2月30日"),
        ("2027-08-29", "截止日期：2月29日"),  # Nearest valid date is 184 days away.
    ],
)
def test_yearless_dates_without_a_reasonable_valid_anchor_stay_unknown(published, text):
    rows = date_values(text, published, uid())
    assert [(row["date"], row["at"], row["precision"]) for row in rows] == [(None, None, "unknown")]


def test_deadline_and_event_time_in_same_sentence_keep_roles():
    rows = date_values("报名截止2026年10月20日，活动时间2026年10月25日", None, uid())
    assert [(row["kind"], row["date"]) for row in rows] == [
        ("school_deadline", "2026-10-20"),
        ("event_time", "2026-10-25"),
    ]


def test_clock_cannot_leak_from_next_date():
    rows = date_values("报名截止2026年10月20日，活动时间2026年10月21日15:00", None, uid())
    assert rows[0]["precision"] == "date"
    assert rows[1]["at"] == "2026-10-21T15:00:00+08:00"


def test_date_only_publication_is_local_calendar_date():
    rows = date_values("请于明天提交申请表", "2026-10-08", uid())
    assert rows[0]["date"] == "2026-10-09"


def test_chinese_numeral_clock_is_resolved_like_digits():
    # 下午三点 is the same instruction as 下午3点; dropping the clock would lose the
    # only time of day the notice states.
    rows = date_values("请于下周三下午三点前提交", "2026-10-10T09:00:00+08:00", uid())
    assert [(row["precision"], row["at"]) for row in rows] == [("minute", "2026-10-14T15:00:00+08:00")]


def test_chinese_numeral_half_hour_is_resolved():
    rows = date_values("会议时间：10月20日上午九点半", "2026-10-10T09:00:00+08:00", uid())
    assert rows[0]["at"] == "2026-10-20T09:30:00+08:00"


def test_enumeration_point_is_not_a_clock():
    # 第三点 is a list item, not three o'clock.
    rows = date_values("第一点、第二点、第三点说明，报名截止2026年10月20日", None, uid())
    assert [row["precision"] for row in rows] == ["date"]


def test_parenthesized_weekday_does_not_create_an_earlier_deadline():
    rows = date_values("报名截止时间为9月7日（周日）13：00。", "2025-08-30", uid())
    assert [(row["date"], row["at"]) for row in rows] == [(None, "2025-09-07T13:00:00+08:00")]


def test_event_interval_retains_both_endpoints_as_event_times():
    rows = date_values("项目时间：2026年7月20日-8月2日", "2026-03-13", uid())
    assert [(row["kind"], row["date"]) for row in rows] == [("event_time", "2026-07-20"), ("event_time", "2026-08-02")]


def test_system_closure_is_a_deadline_cue():
    rows = date_values("4 月 28 日 16:00，逾期系统关闭不再受理", "2026-04-21", uid())
    assert [(row["kind"], row["at"]) for row in rows] == [("school_deadline", "2026-04-28T16:00:00+08:00")]


def test_generic_time_label_does_not_become_application_deadline():
    rows = date_values("时间：2026年9月18日（星期五）", "2026-08-20", uid())
    assert [(row["kind"], row["date"]) for row in rows] == [("event_time", "2026-09-18")]


def test_spaced_explicit_year_does_not_borrow_publication_year():
    rows = date_values("申请截止日期为202 6年4月1日前", "2025-04-01", uid())
    assert [row["date"] for row in rows] == ["2026-04-01"]


def test_material_negation_does_not_remove_positive_items():
    assert material_values("请提交申请表和成绩单，无需提交身份证复印件") == ["申请表和成绩单"]


def test_materials_do_not_swallow_next_time_instruction():
    assert material_values("请提交申请表和成绩单，活动时间2026年10月25日") == ["申请表和成绩单"]


def test_named_application_form_to_fill_is_a_material():
    assert material_values("完整填写附件《天津大学境外交流申请表》") == ["《天津大学境外交流申请表》"]
    assert material_values("请填写个人信息") == []


def test_optional_application_is_not_every_students_obligation(pipeline):
    candidate = extract(pipeline, "有意向的本科生请于2026年10月20日前提交申请表。")
    assert candidate["action_required"] is False


def test_negated_submission_is_not_required(pipeline):
    candidate = extract(pipeline, "全体本科生无需提交申请表。")
    assert candidate["action_required"] is not True
    assert not candidate["materials"]


def test_excluded_education_level_never_becomes_positive_audience(pipeline):
    candidate = extract(pipeline, "本科生不得申请，仅限硕士生申请，请于2026年10月20日前提交申请表。")
    assert candidate["audience"]["education_levels"] == ["master"]
    assert candidate["audience"]["certainty"] == "partial"
    assert "audience_conditions_require_review" in candidate["warnings"]
    assert "本科生不得申请" in candidate["audience"]["conditions_text"]


def test_manual_excluded_audience_cannot_be_accepted(pipeline):
    candidate = extract(pipeline, "本科生不得申请，仅限硕士生申请，请于2026年10月20日前提交申请表。")
    replacement = copy.deepcopy(candidate)
    replacement["audience"]["education_levels"].append("undergraduate")
    with pytest.raises(APIError) as caught:
        pipeline.core.pipeline.review(
            candidate["candidate_id"],
            {
                "expected_candidate_revision": 1,
                "decision": "accept",
                "reason": "synthetic invalid review",
                "replacement_candidate": replacement,
            },
            {"id": uid()},
        )
    assert caught.value.code == "fabricated_audience"


def test_review_cannot_change_event_time_into_application_deadline(pipeline):
    candidate = extract(pipeline, "活动时间：2026年10月25日。", title="合成活动通知")
    replacement = copy.deepcopy(candidate)
    replacement["deadlines"][0]["kind"] = "school_deadline"
    with pytest.raises(APIError) as caught:
        pipeline.core.pipeline.review(
            candidate["candidate_id"],
            {
                "expected_candidate_revision": 1,
                "decision": "accept",
                "reason": "synthetic invalid date role",
                "replacement_candidate": replacement,
            },
            {"id": uid()},
        )
    assert caught.value.code == "fabricated_deadline"


def test_disqualification_is_not_notice_cancellation(pipeline):
    candidate = extract(pipeline, "全体本科生请于2026年10月20日前提交申请表，逾期将取消报名资格。")
    assert candidate["proposed_status"] == "active"


def test_not_cancelled_is_not_cancelled(pipeline):
    candidate = extract(pipeline, "活动未取消，有意向的本科生可报名。", title="合成活动说明")
    assert candidate["proposed_status"] == "active"


def test_correction_cannot_auto_publish_new_deadline(pipeline):
    candidate = extract(pipeline, "全体本科生请于2026年10月22日前提交申请表。", title="合成申请截止时间更正通知")
    assert candidate["review_status"] == "pending"
    assert "correction_requires_review" in candidate["warnings"]


def test_required_missing_attachment_cannot_auto_publish(pipeline):
    candidate = extract(pipeline, "全体本科生请于2026年10月20日前提交申请表，具体申请条件和材料清单详见附件。")
    assert candidate["review_status"] == "pending"
    assert "referenced_attachment_missing_requires_review" in candidate["warnings"]


def test_attached_form_word_alone_does_not_imply_missing_attachment(pipeline):
    candidate = extract(pipeline, "全体本科生请于2026年10月20日前提交申请表。")
    assert candidate["review_status"] == "pending"
    assert "referenced_attachment_missing_requires_review" not in candidate["warnings"]


def test_old_candidate_version_stays_available_after_rule_upgrade(pipeline):
    candidate = extract(pipeline, "全体本科生请于2026年10月20日前提交申请表。")
    candidate = pipeline.core.pipeline.review(
        candidate["candidate_id"],
        {
            "expected_candidate_revision": candidate["revision"],
            "decision": "accept",
            "reason": "核对旧版候选",
            "replacement_candidate": None,
        },
        {"id": uid()},
    )
    prior = copy.deepcopy(candidate)
    prior["extractor_version"] = "campus-rules-1.1"
    pipeline.core.store.put("candidates", candidate["candidate_id"], prior)
    pipeline.core.store.put("candidate_versions", f"{candidate['candidate_id']}:2", prior)
    updated = pipeline.core.pipeline.extract_candidates({"snapshot_id": candidate["snapshot_ids"][0]})
    assert updated["revision"] == 3
    assert updated["review_status"] == "pending"
    assert pipeline.core.pipeline.get_accepted_candidate(candidate["candidate_id"], 2) == prior


def test_ingestion_warning_change_reparses_and_revises_candidate(pipeline):
    candidate = extract(pipeline, "全体本科生请于2026年10月20日前提交申请表。")
    sid = candidate["snapshot_ids"][0]
    snapshot = pipeline.core.store.get("snapshots", sid)
    document = pipeline.core.store.get("documents", snapshot["document_id"])
    document["snapshot_warnings"] = {sid: ["attachment_fetch_timeout"]}
    pipeline.core.store.put("documents", snapshot["document_id"], document)
    changed = pipeline.core.pipeline.extract_candidates({"snapshot_id": sid})
    assert changed["revision"] == 2
    assert changed["review_status"] == "pending"
    assert "attachment_fetch_timeout" in changed["warnings"]
    assert pipeline.core.pipeline.parsed(sid)["state"] == "partial"
    assert {e["id"] for e in candidate["evidence"]} <= {e["id"] for e in pipeline.core.pipeline.parsed(sid)["evidence"]}
    document["snapshot_warnings"][sid] = []
    pipeline.core.store.put("documents", snapshot["document_id"], document)
    recovered = pipeline.core.pipeline.extract_candidates({"snapshot_id": sid})
    assert recovered["revision"] == 3
    assert pipeline.core.pipeline.parsed(sid)["state"] == "ok"
    assert "attachment_fetch_timeout" not in recovered["warnings"]
    assert "parsed_input_changed_requires_review" in recovered["warnings"]
    assert pipeline.core.store.get("candidate_versions", f"{candidate['candidate_id']}:1") == candidate
    assert pipeline.core.pipeline.get_accepted_candidate(candidate["candidate_id"], 1) is None
    assert pipeline.core.pipeline.extract_candidates({"snapshot_id": sid}) == recovered


def test_explicit_active_attachment_empty_set_removes_stale_evidence(pipeline):
    candidate = extract(pipeline, "全体本科生请于2026年10月20日前提交申请表。")
    sid = candidate["snapshot_ids"][0]
    with pipeline.core.store.transaction():
        child, _ = pipeline.core.pipeline.sources.persist(
            pipeline.source,
            FetchResult("https://public.test/attachment.txt", "材料清单：成绩单".encode(), "text/plain"),
            "附件",
            None,
            sid,
        )
    parent_snapshot = pipeline.core.store.get("snapshots", sid)
    document = pipeline.core.store.get("documents", parent_snapshot["document_id"])
    document["snapshot_attachments"] = {sid: [child["snapshot_id"]]}
    pipeline.core.store.put("documents", parent_snapshot["document_id"], document)
    attached = pipeline.core.pipeline.extract_candidates({"snapshot_id": sid})
    assert child["snapshot_id"] in attached["snapshot_ids"]
    assert "成绩单" in attached["materials"]
    document["snapshot_attachments"][sid] = []
    pipeline.core.store.put("documents", parent_snapshot["document_id"], document)
    detached = pipeline.core.pipeline.extract_candidates({"snapshot_id": sid})
    assert detached["revision"] == attached["revision"] + 1
    assert child["snapshot_id"] not in detached["snapshot_ids"]
    assert "成绩单" not in detached["materials"]
    assert all(e["snapshot_id"] != child["snapshot_id"] for e in detached["evidence"])
    assert (
        pipeline.core.store.get("candidate_versions", f"{attached['candidate_id']}:{attached['revision']}") == attached
    )


def test_reactivated_original_body_creates_review_revision_without_rewriting_history(pipeline):
    original = pipeline.notice("全体本科生请于2026年10月20日前提交申请表。")
    sid = original["snapshot_id"]
    document = pipeline.core.store.get("documents", original["document_id"])
    document["snapshot_observations"] = {sid: 1}
    document["snapshot_observed_at"] = {sid: "2026-10-08T12:00:00+08:00"}
    pipeline.core.store.put("documents", original["document_id"], document)
    first = pipeline.core.pipeline.extract_candidates({"snapshot_id": sid})
    first_input = pipeline.core.store.get("candidate_inputs", f"{first['candidate_id']}:1")
    changed = pipeline.notice("全体本科生请于2026年10月22日前提交申请表。")
    second = pipeline.core.pipeline.extract_candidates({"snapshot_id": changed["snapshot_id"]})
    assert first["review_status"] == second["review_status"] == "pending"
    # Sources owns the A -> B -> A observation change; emulate its persisted
    # boundary here so this test does not depend on the parallel fetcher edit.
    document = pipeline.core.store.get("documents", original["document_id"])
    document["latest_snapshot_id"] = sid
    document["snapshot_observations"][sid] = 2
    document["snapshot_observed_at"][sid] = "2026-10-08T13:00:00+08:00"
    pipeline.core.store.put("documents", original["document_id"], document)
    reactivated = pipeline.core.pipeline.extract_candidates({"snapshot_id": sid})
    assert reactivated["candidate_id"] == first["candidate_id"]
    assert reactivated["revision"] == first["revision"] + 1
    assert reactivated["review_status"] == "pending"
    assert "document_reactivation_requires_review" in reactivated["warnings"]
    assert pipeline.core.store.get("candidate_versions", f"{first['candidate_id']}:1") == first
    assert pipeline.core.pipeline.get_accepted_candidate(first["candidate_id"], 1) is None
    assert pipeline.core.pipeline.extract_candidates({"snapshot_id": sid}) == reactivated
    frozen = pipeline.core.store.get("candidate_inputs", f"{first['candidate_id']}:2")
    assert frozen[sid]["observed_at"] == "2026-10-08T13:00:00+08:00"
    assert pipeline.core.store.get("candidate_inputs", f"{first['candidate_id']}:1") == first_input
    assert first_input[sid]["observed_at"] == "2026-10-08T12:00:00+08:00"
    # Review copies the revision-specific input, even if a global alias differs.
    altered_alias = copy.deepcopy(frozen)
    altered_alias[sid]["observed_at"] = "2026-10-08T14:00:00+08:00"
    pipeline.core.store.put("candidate_inputs", first["candidate_id"], altered_alias)
    accepted = pipeline.core.pipeline.review(
        first["candidate_id"],
        {
            "expected_candidate_revision": 2,
            "decision": "accept",
            "reason": "核对原文已重新发布",
            "replacement_candidate": None,
        },
        {"id": uid()},
    )
    assert accepted["revision"] == 3
    assert pipeline.core.store.get("candidate_inputs", f"{first['candidate_id']}:3") == frozen


@pytest.mark.parametrize("extension", ["doc", "xls"])
def test_legacy_office_binary_is_not_decoded_as_plain_text(pipeline, extension):
    with pipeline.core.store.transaction():
        snapshot, _ = pipeline.core.pipeline.sources.persist(
            pipeline.source,
            FetchResult(
                f"https://public.test/legacy.{extension}",
                bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 200,
                "application/octet-stream",
            ),
            "旧版附件",
            None,
        )
    parsed = pipeline.core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
    assert parsed["state"] == "failed"
    assert parsed["segments"] == []
    assert "unsupported_legacy_office_requires_review" in parsed["warnings"]


# A numeric year/month/day date used to lose the first digit of a two-digit day.
# The day alternation tried ``[0-2]?\d`` before ``3[01]`` and had no trailing
# boundary, so ``2026.11.30`` matched as ``2026.11.3``: a well-formed date,
# reported at ``date`` precision, silently 27 days early. The shipped corpus
# contains ``毕业时间：2026.09.01-2027.08.31``, stored as 2027-08-03.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("报名截止：2026-11-30", "2026-11-30"),
        ("报名截止：2026/12/31", "2026-12-31"),
        ("报名截止：2026.10.30", "2026-10-30"),
        ("报名截止：2026.10.31", "2026-10-31"),
        ("报名截止：2026-1-30", "2026-01-30"),
    ],
)
def test_numeric_date_keeps_a_two_digit_day(text, expected):
    rows = date_values(text, None, uid())
    assert [(row["date"], row["precision"]) for row in rows] == [(expected, "date")]


def test_dotted_range_endpoint_keeps_its_two_digit_day():
    rows = date_values("毕业时间：2026.09.01-2027.08.31", None, uid())
    assert [row["date"] for row in rows] == ["2027-08-31"]


def test_day_thirty_one_is_not_read_as_the_third():
    # Both halves matter: 31 must survive whole, and the day before it must not
    # be pulled forward by the same alternation.
    rows = date_values("报名时间：2026-08-30至2026-08-31", None, uid())
    assert [row["date"] for row in rows] == ["2026-08-31"]


# 24:00 is a real end-of-day marker, not an impossible clock. Treating it as
# impossible dropped the date as well, so a notice that plainly said 11月30日
# was reported as 截止时间待确认.
def test_end_of_day_clock_keeps_the_date_it_belongs_to():
    rows = date_values("申请截止日期为11月30日24:00", "2026-10-11T10:00:00+08:00", uid())
    assert len(rows) == 1
    assert rows[0]["precision"] == "minute"
    # The end of 30 November is the start of 1 December, which is the same instant.
    assert rows[0]["at"] == "2026-12-01T00:00:00+08:00"


def test_impossible_clock_still_refuses_to_guess():
    rows = date_values("报名截止：11月30日25:00", "2026-10-11T10:00:00+08:00", uid())
    assert [(row["precision"], row["date"], row["at"]) for row in rows] == [("unknown", None, None)]


def test_end_of_day_clock_with_minutes_is_still_impossible():
    rows = date_values("报名截止：11月30日24:30", "2026-10-11T10:00:00+08:00", uid())
    assert [(row["precision"], row["date"], row["at"]) for row in rows] == [("unknown", None, None)]


# A table-of-contents line like "2. 网上报名及截止时间" fires the deadline cue
# while stating no date; the real deadline sits in the body under it. Keeping
# the heading as a second "待确认" row tells the student the opposite of the
# truth -- there IS a confirmed date. These cases pin both halves of the rule:
# drop the heading row when the same kind is already parseable, keep it when
# the heading is the notice's only deadline signal.
def _deadlines(case, text):
    candidate = extract(case, text)
    return candidate["deadlines"], candidate


def test_section_heading_placeholder_is_dropped_when_a_real_date_exists(pipeline):
    rows, _ = _deadlines(
        pipeline,
        "二、参会报名\n2. 网上报名及截止时间\n报名截止时间2026年3月14日17:00，请各单位尽快报名。",
    )
    assert any(row["precision"] == "minute" for row in rows)
    assert all(row["raw_text"] != "2. 网上报名及截止时间" for row in rows)


def test_bracketed_heading_placeholder_is_dropped_when_a_real_date_exists(pipeline):
    rows, _ = _deadlines(
        pipeline,
        "项目通知\n【招生截止时间】\n截止时间：2026年5月30日（以项目官方通知为准），各专业名额有限。",
    )
    assert any(row["precision"] == "date" for row in rows)
    assert all(row["raw_text"] != "【招生截止时间】" for row in rows)


def test_heading_noun_phrase_placeholder_is_dropped_when_a_real_date_exists(pipeline):
    rows, _ = _deadlines(
        pipeline,
        "项目报名\n报名流程及截止时间\n请于2026年5月30日前完成申请，逾期不再受理。",
    )
    assert any(row["date"] == "2026-05-30" for row in rows)
    assert all(row["raw_text"] != "报名流程及截止时间" for row in rows)


def test_heading_only_deadline_still_keeps_its_unknown_row(pipeline):
    # The heading is the notice's only deadline signal; "needs review" must
    # never silently become "no deadline".
    rows, candidate = _deadlines(pipeline, "一、报名安排\n2. 网上报名及截止时间\n请关注后续通知。")
    assert [(row["precision"], row["date"], row["at"]) for row in rows] == [("unknown", None, None)]
    assert "ambiguous_deadline_requires_review" in candidate["warnings"]


def test_real_unknown_deadline_statement_is_never_treated_as_a_heading(pipeline):
    rows, _ = _deadlines(pipeline, "截止时间另行通知，请以官网为准。")
    assert [(row["precision"], row["date"], row["at"]) for row in rows] == [("unknown", None, None)]


def test_real_headings_and_real_statements_are_told_apart():
    from server.campus.content import _is_heading_deadline_reference as heading

    # Real heading rows from the shipped sample, all of which had a parseable
    # deadline in the same notice.
    assert heading("2. 网上报名及截止时间")
    assert heading("（2）网上报名及截止时间")
    assert heading("报名流程及截止时间")
    assert heading("【招生截止时间】")
    # Real statements that must never be dropped.
    assert not heading("截止时间另行通知，请以官网为准。")
    assert not heading("（七）通过初选的学生名单将在学部和学部网站进行公示，公示期为7日，公示期间公示内容不得修改，")
    assert not heading("缴费情况：每家单位需缴纳场地费500元(费用含展板费、场地费、展位费)，欠发达地区的单位可以")
    assert not heading("")


def test_extractor_version_documents_the_current_rules(pipeline):
    # campus-rules-1.6 added BEFORE_DATE_CUE: "于<具体日期>…前" submission instructions
    # observed verbatim on real TJU pages (test_real_page_deadlines.py).
    # campus-rules-1.7 added SCHEDULE_CUE: "校考于6月30日…举行", "报到时间为6月30日13:00-13:30",
    # "可于6月28日12:00起登录" — schedule sentences, labelled event_time and never promoted
    # to a deadline (test_real_page_schedule.py).
    # campus-rules-1.8 fixes what a student actually reads on a real page:
    #   * the headline comes from the article metadata instead of the column
    #     heading the gs template printed inside the body container
    #     (test_body_quality.py, test_event_type_headline.py);
    #   * a deadline quote is the clause the date came from, not the whole
    #     paragraph, and a dateless body sentence no longer adds a duplicate
    #     "待确认" row (test_deadline_quote.py);
    #   * 招生/报名/申请/推免/招录 classify as a real type instead of "other",
    #     and a usable headline is never overridden by incidental body words
    #     (test_materials_event_type.py, test_event_type_headline.py);
    #   * 申请材料 / 报名材料 are extractable deliverables
    #     (test_materials_event_type.py);
    #   * 宣讲会 is no longer automatically a job event
    #     (test_event_type_talk.py).
    # campus-rules-1.11 narrows two more real-data failures, both found while
    # publishing 161 real notices:
    #   * an error/access page ("操作失败") is refused instead of becoming a
    #     notice (test_error_pages.py);
    #   * a notice that merely mentions 取消 (conditional "如遇…取消考试", or the
    #     student cancelling their own submission) is no longer proposed as a
    #     cancellation, which used to make it unpublishable
    #     (test_cancellation_mention.py);
    #   * a listing queues the pages the product can read before WeChat posts it
    #     cannot (test_listing_priority.py).
    # campus-rules-1.12 fixes a silent content loss found while pruning: a live
    # recruitment notice was proposed as a cancellation because of the standard
    # reservation-of-rights clause ("集团有权…调整、取消或终止个别岗位的招聘
    # 工作"). A cancellation without a linkable original can never be published,
    # so a real notice with a future deadline was dropped without a trace
    # (test_cancellation_disclaimer.py).
    # campus-rules-1.13 answers the queue's own numbers: of 127 pending candidates,
# 0 had a deadline while five states it as "10月16日12:00前…", "8月31日之前…" or
# "2026年6月30日前…" — the date carries the due wording and no cue word appears,
# so the sentence was invisible to the extractor:
#   * TRAILING_BEFORE_CUE recognizes 日期(±时刻)(之)前 without 于, and the
#     existing 于…前 form now tolerates a weekday/clock parenthesis between the
#     date and 前 ("于2024年9月27日(周五)16：00前") (test_real_page_deadlines.py);
#   * 前 inside a compound (前后/前期/前往/前几年) is not a deadline: a vague
#     "3月15日前后举行" used to be reported as a school deadline, which is exactly
#     the "不确定的日期不伪装成事实" promise being broken.
# A date mentioned with no due wording at all still yields nothing.
    candidate = extract(pipeline, "报名截止时间2026年3月14日17:00，请尽快报名。")
    assert candidate["extractor_version"] == "campus-rules-1.13"


# "11月30日24时" and "11月30日24:00" name the same instant (end of day) but
# previously landed in different precisions: the colon form rolled to the next
# day at 00:00 while the 时 form silently kept only the date. Durations are
# written 24小时/24个小时, where a 小 separates the digits from the 时 -- the
# notation difference is the guard.
def test_end_of_day_written_as_shi_matches_the_colon_form():
    rows = date_values("请于11月30日24时前提交", "2026-10-11T10:00:00+08:00", uid())
    assert [(row["precision"], row["date"], row["at"]) for row in rows] == [
        ("minute", None, "2026-12-01T00:00:00+08:00")
    ]
    colon = date_values("请于11月30日24:00前提交", "2026-10-11T10:00:00+08:00", uid())

    def semantic(rs):
        return [{k: v for k, v in r.items() if k not in ("raw_text", "evidence_ids")} for r in rs]

    assert semantic(rows) == semantic(colon)


def test_ordinary_shi_hour_keeps_its_clock():
    rows = date_values("请于11月30日18时前提交", "2026-10-11T10:00:00+08:00", uid())
    assert [(row["precision"], row["date"], row["at"]) for row in rows] == [
        ("minute", None, "2026-11-30T18:00:00+08:00")
    ]


def test_duration_in_hours_is_not_an_impossible_clock():
    # "24小时内" is a duration; the date alone is the deadline.
    rows = date_values("请于11月30日起24小时内提交", "2026-10-11T10:00:00+08:00", uid())
    assert [(row["precision"], row["date"], row["at"]) for row in rows] == [("date", "2026-11-30", None)]
    rows = date_values("请于11月30日起12个小时内提交", "2026-10-11T10:00:00+08:00", uid())
    assert [(row["precision"], row["date"], row["at"]) for row in rows] == [("date", "2026-11-30", None)]

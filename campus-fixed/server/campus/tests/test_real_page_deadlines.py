"""Deadline sentences taken verbatim from real Tianjin University pages.

Provenance (server-side fetch, 2026-10-13; see
verification/source-reachability-2026-10-13.json): the 教务处 notice listing
mixes site articles with WeChat articles, so both phrasings below belong to the
one ingestion chain the product actually runs. These are the first real-page
cases in this suite; the synthetic counterexamples stay in
test_extraction_accuracy.py and remain the primary safety net.
"""

from server.campus.content import date_values
from server.campus.schema import uid

PUBLISHED_CET = "2026-09-14T14:01:00+08:00"
PUBLISHED_CAMBRIDGE = "2026-03-27T00:00:00+08:00"


def rows(text, published):
    return date_values(text, published, uid())


def test_department_submission_deadline_before_a_full_date():
    # "请考生所在学院于2026年12月15日前统一将申请解除限报材料报送教务处"
    found = rows(
        "请考生所在学院于2026年12月15日前统一将申请解除限报材料报送教务处（材料包括考生本人向学院提出的书面申请，并附相关证明材料）",
        PUBLISHED_CET,
    )
    # 学院报送 → college_deadline（产品区分校级/院级截止）
    assert [(r["kind"], r["precision"], r["date"]) for r in found] == [("college_deadline", "date", "2026-12-15")]


def test_two_digit_year_with_clock_before_the_date():
    # "最晚于26年9月18日16:30前，将需要更新的学生信息…上报"
    found = rows(
        "最晚于26年9月18日16:30前，将需要更新的学生信息（学籍表和照片，学籍表为excel格式）上报。",
        PUBLISHED_CET,
    )
    # 分钟精度的时间戳落在 at 字段；两位数年份按发布年锚定
    assert [(r["kind"], r["precision"], r["at"]) for r in found] == [
        ("school_deadline", "minute", "2026-09-18T16:30:00+08:00")
    ]


def test_parenthesised_phrase_between_the_date_and_before():
    # "应于2026年9月24日（报名结束前）向教务处（部）提出正式书面申请"
    found = rows(
        "报名参加CET并申请提供合理便利的残疾考生，应于2026年9月24日（报名结束前）向教务处（部）提出正式书面申请。",
        PUBLISHED_CET,
    )
    assert [(r["kind"], r["precision"], r["date"]) for r in found] == [("school_deadline", "date", "2026-09-24")]


def test_narrative_yu_without_a_date_still_yields_nothing():
    found = rows("请同学们于校内各网点办理本学期注册手续，逾期不候。", PUBLISHED_CET)
    assert found == []


# --- Round 19 (t23): "date (+clock) 前" without any cue word -------------------
# The 127 pending candidates of the real queue contained 0 with a deadline; five of
# them state the deadline as "10月16日12:00前…" / "8月31日之前…" / "2026年6月30日前…".
# The date carries its own due wording, so the cue table missed it entirely.


def test_clock_then_before_without_a_cue_word_is_the_deadline():
    # "10月16日12:00前，有意向申请本项目的同学请登录境外院校官网查询…"（2027年春季学期交换生第二批）
    found = rows(
        "10月16日12:00前，有意向申请本项目的同学请登录境外院校官网查询相关专业及课程信息，"
        "确认拟就读专业及课程后，填写线上报名表。",
        "2026-09-20T10:00:00+08:00",
    )
    assert [(r["kind"], r["precision"], r["at"]) for r in found] == [
        ("school_deadline", "minute", "2026-10-16T12:00:00+08:00")
    ]


def test_date_then_zhizhiqian_is_the_deadline():
    # "8月31日之前将人事档案原件寄送至我校"（拟录取硕士研究生调档函通知）
    found = rows(
        "请登录天津大学招生管理系统下载调档函，8月31日之前将人事档案原件寄送至我校。",
        "2026-08-01T09:00:00+08:00",
    )
    assert [(r["kind"], r["precision"], r["date"]) for r in found] == [("school_deadline", "date", "2026-08-31")]


def test_date_then_before_in_a_consequence_sentence_is_the_deadline():
    # "2026年6月30日前未办理结业、肄业相关手续的，学校将依据…作退学处理"
    found = rows(
        "2026年6月30日前未办理结业、肄业相关手续的，学校将依据《天津大学研究生学籍管理办法》作退学处理。",
        "2026-03-01T09:00:00+08:00",
    )
    assert [(r["kind"], r["precision"], r["date"]) for r in found] == [("school_deadline", "date", "2026-06-30")]


def test_weekday_parenthesis_and_full_width_clock_before_a_department_deadline():
    # "请各学院(部)于2024年9月27日(周五)16：00前，将已审核通过的选题申报材料…发至教务处"
    # The existing BEFORE_DATE_CUE allowed only eight characters between the date and 前.
    found = rows(
        "请各学院(部)于2024年9月27日(周五)16：00前，将已审核通过的选题申报材料汇总后通过办公网发至教务处。",
        "2024-09-20T09:00:00+08:00",
    )
    assert [(r["kind"], r["precision"], r["at"]) for r in found] == [
        ("college_deadline", "minute", "2024-09-27T16:00:00+08:00")
    ]


def test_a_date_followed_by_a_compound_word_is_not_a_deadline():
    """前 inside 前后/前期/前往 states nothing about a deadline."""
    assert rows("本次座谈会将于3月15日前后举行，具体安排见附件。", "2026-03-01T09:00:00+08:00") == []
    assert rows("活动将于4月20日前往新校区举办。", "2026-04-01T09:00:00+08:00") == []


def test_a_date_mentioned_only_as_background_stays_uncertain():
    """The red line: a bare date carries no deadline and must stay unclaimed."""
    assert rows("新华社北京10月2日电，近日习近平总书记给天津大学全体师生回信。", "2026-10-03T09:00:00+08:00") == []
    assert rows("《关于规范研究生学位论文使用人工智能工具的倡议书》发布日期：2025年11月14日", "2025-11-14T09:00:00+08:00") == []

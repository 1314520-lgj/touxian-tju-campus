"""Schedule sentences taken verbatim from the real 强基计划 notice.

Provenance: the page staged for operator cycle 1,
``deploy/operator/staging/zs-notices/强基计划校考工作安排.txt`` (see
``verification/operator-cycle-1-2026-10-13.json``). The cycle-1 candidate taken
from that notice carried no deadlines at all, so a student reading "校考安排"
could not see when the 校考, the 报到 or the 准考证 printing window actually
happened — the notice told them to show up and never said when.

These are the first schedule-shaped real-page cases in this suite; the synthetic
counterexamples stay in ``test_extraction_accuracy.py`` and remain the primary
safety net.

A schedule sentence says when something *happens*; it is not a due date. The
invariant these cases pin is therefore two-sided: the date must appear, and it
must appear as ``event_time`` — never promoted to ``school_deadline``, which
would tell a student the 校考 has to be finished by then.
"""

from server.campus.content import date_values
from server.campus.schema import uid

PUBLISHED_CAMP = "2026-06-26T00:00:00+08:00"


def rows(text, published=PUBLISHED_CAMP):
    return date_values(text, published, uid())


def test_exam_day_written_as_yu_date_ju_xing():
    # "天津大学2026年强基计划校考于6月30日在天津大学卫津路校区（…）举行。"
    found = rows("天津大学2026年强基计划校考于6月30日在天津大学卫津路校区（天津市南开区卫津路92号）举行。")
    assert [(r["kind"], r["precision"], r["date"]) for r in found] == [("event_time", "date", "2026-06-30")]


def test_registration_window_reports_its_start_minute():
    # "2.报到时间为6月30日13:00-13:30,考生须按报到时间到达本人入围专业的报到地点…"
    found = rows("2.报到时间为6月30日13:00-13:30,考生须按报到时间到达本人入围专业的报到地点，具体报到教室信息如下：")
    assert [(r["kind"], r["precision"], r["at"]) for r in found] == [
        ("event_time", "minute", "2026-06-30T13:00:00+08:00")
    ]


def test_ticket_window_that_opens_with_qi():
    # "入围考生可于6月28日12:00起登录强基计划报名平台打印准考证。"
    found = rows("入围考生可于6月28日12:00起登录强基计划报名平台打印准考证。")
    assert [(r["kind"], r["precision"], r["at"]) for r in found] == [
        ("event_time", "minute", "2026-06-28T12:00:00+08:00")
    ]


def test_a_schedule_sentence_is_never_promoted_to_a_deadline():
    # The same three shapes, stated plainly: none is a due date, so none may be
    # reported as a deadline in either the school or the college sense.
    for text in (
        "天津大学2026年强基计划校考于6月30日在天津大学卫津路校区举行。",
        "报到时间为6月30日13:00-13:30,考生须按报到时间到达报到地点。",
        "入围考生可于6月28日12:00起登录强基计划报名平台打印准考证。",
    ):
        kinds = [r["kind"] for r in rows(text)]
        assert kinds, text
        assert all(kind == "event_time" for kind in kinds), (text, kinds)


def test_a_narrative_yu_without_a_date_still_yields_nothing():
    # Counterexample carried over from the round-7 file: "于〈地点〉办理" names no
    # date and must not become a schedule row either.
    assert rows("请同学们于校内各网点办理本学期注册手续，逾期不候。") == []

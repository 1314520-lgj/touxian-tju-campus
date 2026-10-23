"""宣讲会 is not automatically a job event.

An earlier round filed every 宣讲会 under 就业 on the base-rate argument that a
campus 宣讲会 usually means a company recruitment talk. Real data disagrees:
"2026年暑期剑桥大学移动课堂跨学科学术实践项目宣讲会" is an exchange-programme
information session, and filing it under 就业 hides it from the students who
want it. The career reading is kept for the unambiguous shapes (招聘 / 双选会 /
校招), which name a job event outright.
"""

import pytest

from server.campus.content import event_type_values


@pytest.mark.parametrize(
    "title",
    [
        "2026年暑期剑桥大学移动课堂跨学科学术实践项目宣讲会通知",
        "关于举办国家留学基金委项目宣讲会的通知",
        "2026年秋季学期交换生项目宣讲会",
    ],
)
def test_programme_info_session_is_not_employment(title):
    assert event_type_values(title, "") == "activity"


@pytest.mark.parametrize(
    "title",
    [
        "中国建筑集团2026届校园招聘宣讲会",
        "天津大学2026届“国优计划”毕业生双选会",
        "“万企进校园”天津大学2026年秋季大型双选会",
        "2026届毕业生秋季供需见面会",
        "某科技公司校园招聘会",
    ],
)
def test_a_job_event_stays_employment(title):
    assert event_type_values(title, "") == "employment"


def test_recruitment_talk_with_school_recruit_word_is_employment():
    """校招 names recruiting outright, even without 招聘."""
    assert event_type_values("某集团2026校园校招宣讲会", "") == "employment"

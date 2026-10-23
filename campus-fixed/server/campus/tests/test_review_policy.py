"""The review policy is the gate between the backlog and a student's list.

Measured on real data: 79 candidates were waiting, the policy accepted 78, and
only **one** of them had a deadline still ahead. Publishing the rest would have
put a 2021 exam schedule, a column fragment ("【考试安排】2021") and a news piece
about an anniversary meeting into a list that is supposed to answer "what do I do
today" — and one day later the expiry sweep would take most of them straight back
down again. That is churn with no benefit, so the policy needs two more
questions beyond "is there evidence?":

* does any deadline still lie ahead? (otherwise the notice is already past)
* is this a notice at all, or a column fragment / a news item?

Both rules must leave genuine notices alone, including the ones whose titles
carry a campus tag ("【北洋园校区】…宣讲会") or a result list ("拟录取名单公示").
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPT = ROOT / "scripts" / "review_and_publish.py"
spec = importlib.util.spec_from_file_location("review_and_publish", SCRIPT)
review_and_publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review_and_publish)

TODAY = date(2026, 10, 20)


def candidate(title, deadlines=(), evidence=True, **extra):
    row = {
        "title": title,
        "deadlines": list(deadlines),
        "evidence": [{"quote": "全体本科生请于2026年11月1日前提交。"}] if evidence else [],
    }
    row.update(extra)
    return row


def deadline(value, precision="date"):
    return {
        "kind": "school_deadline",
        "precision": precision,
        "date": value if precision == "date" else None,
        "at": None if precision == "date" else value,
        "raw_text": f"报名截止时间：{value}",
        "evidence_ids": ["e1"],
    }


def decision(row):
    return review_and_publish.classify(row, today=TODAY)


# --- the notice still has to be actionable ----------------------------------
def test_a_notice_whose_deadlines_all_passed_is_rejected():
    verdict, reason = decision(candidate("2021年研究生公共课考试安排", [deadline("2021-01-11")]))
    assert verdict == "reject"
    assert "过去" in reason or "过期" in reason


def test_a_notice_with_a_future_deadline_is_accepted():
    verdict, _ = decision(candidate("2027年寒假牛津项目报名通知", [deadline("2026-10-30")]))
    assert verdict == "accept"


def test_one_future_deadline_is_enough():
    verdict, _ = decision(
        candidate("四六级报名通知", [deadline("2026-09-24"), deadline("2026-12-12")])
    )
    assert verdict == "accept"


def test_an_undated_notice_is_left_for_a_human_not_auto_published():
    """Reference material and stale documents look identical to a regex.

    "【办事流程】关于研究生课程缓考申请的流程" is useful for years; a 2021 policy PDF
    is not. Both are undated, so the safe default is to keep the candidate
    pending rather than let a pattern decide what a student sees.
    """
    verdict, reason = decision(candidate("【办事流程】关于研究生课程缓考申请的流程"))
    assert verdict == "review"
    assert "人" in reason


# --- but it also has to be a notice, not a fragment or a news item ----------
def test_a_column_fragment_title_is_rejected():
    for title in ("【考试安排】2021", "【考试安排】2022", "【课程学习】2023"):
        verdict, reason = decision(candidate(title, [deadline("2026-12-01")]))
        assert verdict == "reject", title
        assert "栏目" in reason or "碎片" in reason


def test_a_campus_tag_prefix_is_not_a_fragment():
    """The tag is a real prefix, not the whole title — this is a notice."""
    verdict, _ = decision(
        candidate("【北洋园校区】中国船舶集团第七一八研究所2027年人才招聘宣讲会", [deadline("2026-11-05")])
    )
    assert verdict == "accept"


def test_a_news_headline_without_a_deadline_is_rejected():
    for title in (
        "天津大学天津医院召开建院165周年高质量发展大会",
        "2026年CCF中国智能机器人学术年会在天津盛大开幕",
        "天津大学研发产教融合信息平台亮相2026年世界职业技术教育发展大会",
    ):
        verdict, reason = decision(candidate(title))
        assert verdict == "reject", title
        assert "新闻" in reason or "宣传" in reason


def test_a_meeting_notice_is_not_mistaken_for_news():
    """"关于召开…会议的通知" is exactly the thing a student needs."""
    verdict, _ = decision(candidate("关于召开2026年学生代表会议的通知", [deadline("2026-11-20")]))
    assert verdict == "accept"


def test_a_document_or_form_title_is_rejected():
    """Listing pages link the attachment next to the notice; it is not a notice."""
    for title in (
        "学位审批书、评阅书、评议书及表决票202509",
        "学位论文评阅专家聘书",
        "优博基金验收报告",
        "一级学科博士硕士学位授权点申请基本条件（20170317）",
        "工程类专业学位类别和专业领域名称的中英文对照（建议稿）",
        "天津大学新版学位论文封面",
        "教育部高等教育司2023年工作要点",
    ):
        verdict, reason = decision(candidate(title))
        assert verdict == "reject", title
        assert "文档" in reason or "附件" in reason


def test_a_notice_about_a_document_is_still_accepted():
    verdict, _ = decision(candidate("关于组织填写学位审批书的通知", [deadline("2026-11-10")]))
    assert verdict == "accept"


def test_a_result_list_is_not_auto_published_but_not_rejected_either():
    """拟录取名单公示 matters to the students it names — the machine just cannot
    tell that from a stale list, so it hands the decision to a human."""
    verdict, _ = decision(
        candidate("天津大学机械工程学院2026年专业学位博士研究生拟录取名单公示（第三批）")
    )
    assert verdict == "review"


def test_only_a_notice_actionable_today_is_auto_published():
    actionable = candidate("2027年寒假剑桥项目报名通知", [deadline("2026-12-01")])
    assert decision(actionable)[0] == "accept"
    for row in (
        candidate("某通知", [deadline("2026-09-01")]),  # already past
        candidate("某参考手册"),  # undated
        candidate("【考试安排】2021", [deadline("2026-12-01")]),  # fragment
    ):
        assert decision(row)[0] != "accept", row["title"]


# --- the earlier guarantees still hold --------------------------------------
def test_a_column_name_title_is_still_rejected():
    verdict, _ = decision(candidate("通知公告", [deadline("2026-12-01")]))
    assert verdict == "reject"


def test_an_error_page_is_still_rejected():
    verdict, _ = decision(candidate("系统繁忙", [deadline("2026-12-01")]))
    assert verdict == "reject"


def test_a_candidate_without_evidence_is_still_rejected():
    verdict, _ = decision(candidate("某通知", [deadline("2026-12-01")], evidence=False))
    assert verdict == "reject"


def test_a_cancellation_without_a_target_is_still_rejected():
    verdict, _ = decision(
        candidate("关于取消某活动的通知", [deadline("2026-12-01")], change_kind="cancel", target_event_id=None)
    )
    assert verdict == "reject"
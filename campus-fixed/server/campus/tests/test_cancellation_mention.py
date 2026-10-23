"""A brand-new notice that mentions 取消 is not itself a cancellation.

Four real notices failed to publish with ``cancel_target_required``: a fresh
candidate had been labelled ``change_kind=cancel`` because its body contained
the word 取消 somewhere ("如遇疫情取消考试", "取消预约"). A cancellation has to
cancel something the product already knows; with no prior event there is nothing
to link, so the row could never be published at all — a real notice silently
became undeliverable.

The rule: propose a cancellation only when there is a prior event to cancel.
Otherwise keep the notice, keep the quote as status evidence, and flag it so a
reviewer sees the mention.
"""

import pytest

from server.campus.content import Pipeline
from server.campus.platform import Core, Settings
from server.campus.schema import uid, validate
from server.campus.sources import FetchResult
from server.campus.tests.test_integration import synthetic_source


@pytest.fixture
def core(tmp_path):
    instance = Core(Settings(data_dir=tmp_path, secret="cancel-mention-" * 3, dev_auth=True))
    instance.pipeline = Pipeline(instance)
    yield instance
    instance.store.close()


def extract(core, title, paragraphs):
    source = synthetic_source(core)
    body = (
        "<html><h1>" + title + "</h1><article>" + "".join("<p>" + p + "</p>" for p in paragraphs) + "</article></html>"
    ).encode()
    snapshot, _ = core.pipeline.sources.persist(
        source,
        FetchResult("https://example.com/" + uid(), body, "text/html"),
        title,
        "2026-10-08T12:00:00+08:00",
    )
    core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
    candidate = core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    validate("ExtractionCandidate", candidate)
    return candidate


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("天津大学考点关于雅思考试防疫要求的通知", "如遇疫情变化，考点可能取消当次考试并另行通知。"),
        ("关于导师助研金系统功能升级的温馨提示", "升级期间可取消已提交的申请并重新填写。"),
        ("2026级新生缴学宿费的通知", "缴费后如需取消订单，请于2026年9月10日前办理。"),
    ],
)
def test_a_new_notice_mentioning_cancellation_stays_publishable(core, title, body):
    candidate = extract(core, title, [body])
    assert candidate["change_kind"] == "new"
    assert candidate["proposed_status"] == "active"


def test_the_mention_is_still_captured_as_evidence(core):
    """The sentence is not thrown away: a reviewer can still read it."""
    candidate = extract(core, "关于雅思考试防疫要求的通知", ["如遇疫情变化，考点可能取消当次考试并另行通知。"])
    quotes = [item.get("quote") or "" for item in candidate["evidence"]]
    assert any("取消" in quote for quote in quotes)


def test_a_real_cancellation_is_still_a_cancellation(core):
    """The product's own rule is preserved: an actual cancellation needs a target."""
    candidate = extract(core, "合成活动取消通知", ["原活动取消，不再举办。"])
    assert candidate["change_kind"] == "cancel"
    assert candidate["proposed_status"] == "cancelled"
    assert "cancellation_target_requires_review" in candidate["warnings"]


@pytest.mark.parametrize(
    ("title", "body"),
    [
        # 研究生院 · 导师助研金系统功能升级: a UI button, not the event.
        ("关于导师助研金系统功能升级的温馨提示", "若已设置，可随时点击删除按钮取消"),
        # 研究生院 · 新生缴学宿费: cancelling a checkbox.
        ("2026级新生缴学宿费的通知", "（注：如不需要缓交，请取消勾选“计算缓缴款”选项）"),
        # 就业指导中心 · 供需对接会: cancelling a reservation.
        ("第三届全国大学生职业规划大赛总决赛专场招聘会暨“千校万企供需对接会”", "（三）预约取消"),
    ],
)
def test_a_user_interface_action_is_not_a_cancellation(core, title, body):
    """These are the exact sentences that made three real notices unpublishable."""
    candidate = extract(core, title, [body])
    assert candidate["change_kind"] != "cancel"
    assert candidate["proposed_status"] == "active"


def test_a_notice_cancelling_a_real_event_is_still_recognised(core):
    """The predicate form "X取消" with a real object stays a cancellation."""
    candidate = extract(
        core,
        "关于取消雅思考试的通知",
        ["根据防疫要求，天津大学研究生院取消2022年12月份针对社会面的雅思考试。"],
    )
    assert candidate["change_kind"] == "cancel"
"""A notice that tells the reader to submit something *requires* action.

``action_required`` stayed ``None`` unless the sentence happened to contain a
strong modal (必须 / 务必 / 请于), so almost every published notice rendered as
「待核实」 in the student feed — a badge that appeared on 100% of rows and
therefore told nobody anything. The product already shows those sentences as
action steps; if they are good enough to show, the answer to "does this ask me
to do something?" is yes.

Optional wording (自愿 / 欢迎 / 有意向) stays ``False``, and a notice with no
instruction at all stays ``None`` rather than guessing.
"""

import pytest

from server.campus.content import Pipeline
from server.campus.platform import Core, Settings
from server.campus.schema import uid, validate
from server.campus.sources import FetchResult
from server.campus.tests.test_integration import synthetic_source


@pytest.fixture
def core(tmp_path):
    instance = Core(Settings(data_dir=tmp_path, secret="action-required-" * 3, dev_auth=True))
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
        ("2027年春季学期交换生（第二批）项目通知", "报名截止时间：2026年11月20日，请在系统内报名。"),
        ("智能与计算学部关于开展硕博连读研究生选拔的通知", "符合条件的同学请提交申请表与成绩单。"),
        ("关于2026年暑期项目报名的通知", "请在报名系统中填写报名表并于2026年5月10日前上传材料。"),
    ],
)
def test_an_instruction_requires_action(core, title, body):
    candidate = extract(core, title, [body])
    assert candidate["action_steps"], "the instruction should also be an action step"
    assert candidate["action_required"] is True


def test_interest_conditional_wording_is_optional(core):
    """"有意参加的同学…" addresses whoever is interested, so it is not an obligation."""
    candidate = extract(core, "关于2026年暑期项目报名的通知", ["有意参加的同学填写报名表并上传材料。"])
    assert candidate["action_required"] is False


def test_optional_wording_is_not_an_obligation(core):
    candidate = extract(core, "关于举办讲座的通知", "欢迎有兴趣的同学报名参加，无需提交材料。")
    assert candidate["action_required"] is not True


def test_a_notice_with_no_instruction_stays_unknown(core):
    candidate = extract(core, "关于2026年暑期项目的通知", "现将有关事项通知如下，详见附件。")
    assert candidate["action_required"] is None


def test_a_news_report_is_not_an_instruction(core):
    candidate = extract(core, "天津大学团队获评全国创新团队", "该团队长期从事相关研究，成果丰硕。")
    assert candidate["action_required"] is False

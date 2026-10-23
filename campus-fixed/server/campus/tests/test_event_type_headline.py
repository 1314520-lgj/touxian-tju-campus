"""Body evidence must not classify a notice the headline already answered.

The gs template printed its channel name ("培养工作") as the headline, so the
body has to decide the type in that case. That fallback was then applied to
*every* headline that matched no keyword, and a long news article about a
university achievement was filed as "academic" because its body contained the
word 成绩 once. A misleading category is worse for a student than "other": it
hides the notice inside a filter it does not belong to.

These fixtures are verbatim excerpts of the real pages named in the comments.
"""

import pytest

from server.campus.content import Pipeline, event_type_values
from server.campus.platform import Core, Settings
from server.campus.schema import uid, validate
from server.campus.sources import FetchResult
from server.campus.tests.test_integration import synthetic_source


@pytest.fixture
def core(tmp_path):
    instance = Core(Settings(data_dir=tmp_path, secret="event-type-headline-" * 3, dev_auth=True))
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


# news.tju.edu.cn/info/1003/563819.htm — a news report, not an actionable notice.
NEWS_TITLE = "习近平给天津大学全体师生回信强调 聚焦国家重大战略需求提高人才培养质量 更好服务经济社会发展"
NEWS_BODY = [
    "近日，习近平总书记给天津大学全体师生回信，肯定学校取得的办学成绩。",
    "回信强调，要聚焦国家重大战略需求，不断提高人才培养质量。",
]


def test_news_report_is_not_filed_as_academic(core):
    """A real headline that matched nothing must stay "other".

    The body mentions 成绩 and 人才培养, but the notice is a news report; filing
    it under 教务/学业 would put it in a filter it does not belong to.
    """
    assert extract(core, NEWS_TITLE, NEWS_BODY)["event_type"] == "other"


def test_news_report_with_incidental_scholarship_word_is_not_scholarship():
    assert event_type_values(
        "天津大学团队研制新型碳捕集膜", "该研究获得奖学金支持，相关成绩已发表。"
    ) == "other"


def test_channel_name_headline_still_lets_the_body_decide(core):
    """The original reason for body evidence must survive."""
    candidate = extract(
        core,
        "培养工作",
        ["本学期非全日制研究生公共课考试时间见下表。", "考试安排如下，请考生携带研究生证入场。"],
    )
    assert candidate["event_type"] == "academic"


def test_empty_headline_lets_the_body_decide():
    assert event_type_values("", "请于2026年10月20日前报名参加讲座。") == "activity"


@pytest.mark.parametrize(
    ("title", "body"),
    [
        # A real headline that matched a rule keeps winning over the body.
        ("天津大学2026年本科生转专业结果公示", "本次转专业工作已结束，成绩见附件。"),
        ("2026年秋季学期研究生课程考试安排", "考试安排如下。"),
        ("关于举办“艺数融合”第三届民俗艺术节的通知", "欢迎师生参加。"),
    ],
)
def test_headline_still_outranks_body(title, body):
    first = event_type_values(title, body)
    # The body alone would give the same or a different answer; the headline
    # must not be overridden by body text.
    assert first == event_type_values(title, "")

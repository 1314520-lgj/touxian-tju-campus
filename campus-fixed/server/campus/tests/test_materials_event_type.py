"""Real-notice regressions for materials and event type; fixtures are verbatim excerpts.

Each fixture is a shortened but literal excerpt of an official 天津大学 notice body
(or of the synthetic sentence named in docs/DIAGNOSIS-REAL-USE.md). No network is
used: the notice text is persisted through the same pipeline a capture would use.
"""

import pytest

from server.campus.content import Pipeline, material_values
from server.campus.platform import Core, Settings
from server.campus.schema import uid, validate
from server.campus.sources import FetchResult
from server.campus.tests.test_integration import synthetic_source


@pytest.fixture
def core(tmp_path):
    instance = Core(Settings(data_dir=tmp_path, secret="materials-event-type-" * 3, dev_auth=True))
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


# --- 缺陷 4：材料类别词 -----------------------------------------------------


def test_material_category_word_is_an_extractable_material():
    """The real sentence from the exchange-programme notice returned [].

    "申请材料" is a category, not a document name, but a student still has to
    submit it; the notice says so explicitly ("提交电子版申请材料").
    """
    assert material_values("报名截止：报名学生需于2026年4月15日前提交电子版申请材料。") == ["电子版申请材料"]


@pytest.mark.parametrize(
    "sentence",
    [
        "请提交电子版申请材料。",
        "请于2026年4月15日前提交申请材料。",
        "需提交以下申请材料：申请表、成绩单。",
        "请提交报名材料。",
        "请提交证明材料。",
    ],
)
def test_material_category_variants_are_extracted(sentence):
    assert material_values(sentence), sentence


@pytest.mark.parametrize(
    "sentence",
    [
        # A material *mention* is still not a submission requirement.
        "欢迎同学参观材料研究成果展，了解材料的发展。",
        "此次活动介绍申请表设计经验与材料整理方法，欢迎参加。",
        "请同学注意材料通知，具体要求另行通知。",
        "本次活动无需提交申请表与成绩单。",
        "本次活动无需提交申请材料。",
        # Nothing is asked for at all.
        "关于申请材料的最新说明。",
    ],
)
def test_material_category_word_does_not_invent_requirements(sentence):
    assert material_values(sentence) == [], sentence


def test_material_category_is_literal_in_the_candidate(core):
    candidate = extract(
        core,
        "2026年秋季学期交换生（第二批）项目通知",
        ["报名截止：报名学生需于2026年4月15日前提交电子版申请材料。"],
    )
    assert candidate["materials"] == ["电子版申请材料"]
    proofs = {e["id"]: e for e in candidate["evidence"]}
    for evidence_id in candidate["field_evidence"]["materials"]:
        quote = proofs[evidence_id]["quote"]
        assert "电子版申请材料" in quote


# --- 缺陷 5：事件类型 -------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "天津大学智能与计算学部2026年博士研究生招生办法",
        "天津大学智能与计算学部2026年工程博士招生办法",
        "机械工程学院2027年推荐优秀应届本科毕业生免试攻读硕士学位研究生和直接攻读博士学位研究生招生录取工作办法",
        "天津大学智能与计算学部招收2026级推荐免试研究生（含直博生）工作的说明",
    ],
)
def test_admission_notice_is_not_other(core, title):
    candidate = extract(core, title, ["招生工作按照学校有关规定执行，请关注研究生招生网。"])
    assert candidate["event_type"] == "activity", candidate["event_type"]


@pytest.mark.parametrize(
    "title",
    [
        "25261学期【非全日制】研究生公共课考试安排",
        "关于2026年秋季学期研究生课程考试安排的通知",
        "2025级计算机科学与技术（拔尖班）选拔面试通知",
    ],
)
def test_exam_and_selection_notice_is_academic(core, title):
    candidate = extract(core, title, ["考试安排如下，请考生携带研究生证入场。"])
    assert candidate["event_type"] == "academic", candidate["event_type"]


def test_polluted_column_title_falls_back_to_body(core):
    """A channel-name title must not decide the type on its own.

    The gs template put "培养工作" in the headline; the body is an exam notice.
    """
    candidate = extract(
        core,
        "培养工作",
        ["本学期非全日制研究生公共课考试时间见下表。", "考试安排如下，请考生携带研究生证入场。"],
    )
    assert candidate["event_type"] == "academic", candidate["event_type"]


@pytest.mark.parametrize(
    ("title", "body", "expected"),
    [
        ("天津大学2026届“国优计划”毕业生双选会", "请毕业生参加双选会。", "employment"),
        ("“万企进校园”天津大学2026年秋季大型双选会", "请毕业生参加双选会。", "employment"),
        ("天津大学2025届优秀毕业生获奖名单公示", "现将名单予以公示。", "other"),
        ("天津大学团队研制新型碳捕集膜", "该成果已发表。", "other"),
        ("关于举办“艺数融合”第三届民俗艺术节的通知", "欢迎师生参加。", "activity"),
        # 转专业 is an academic-affairs matter; "academic" beats "other".
        ("天津大学2026年本科生转专业结果公示", "现将结果予以公示。", "academic"),
    ],
)
def test_event_type_keeps_existing_enumerations(core, title, body, expected):
    assert extract(core, title, [body])["event_type"] == expected


def test_event_type_never_leaves_the_frozen_enumeration(core):
    """campus-contracts/domain-v1.schema.json fixes the enum; no new value may appear."""
    allowed = {"academic", "assignment", "scholarship", "competition", "activity", "employment", "meeting", "other"}
    for title in (
        "天津大学智能与计算学部2026年博士研究生招生办法",
        "25261学期【非全日制】研究生公共课考试时间安排",
        "奖学金评审结果公示",
        "关于举办学术讲座的通知",
        "培养工作",
    ):
        assert extract(core, title, ["请于2026年10月20日前提交申请材料。"])["event_type"] in allowed

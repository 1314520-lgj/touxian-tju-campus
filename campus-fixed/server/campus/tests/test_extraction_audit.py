"""Extraction regressions use real immutable HTML snapshots and schema validation."""

from unittest.mock import patch

import pytest

from server.campus.content import Pipeline
from server.campus.platform import Core, Settings
from server.campus.schema import uid, validate
from server.campus.sources import FetchResult
from server.campus.tests.test_integration import synthetic_source


@pytest.fixture
def core(tmp_path):
    instance = Core(Settings(data_dir=tmp_path, secret="material-extraction-audit-" * 3, dev_auth=True))
    instance.pipeline = Pipeline(instance)
    yield instance
    instance.store.close()


def extract(core, title, paragraphs):
    source = synthetic_source(core)
    body = (
        "<html><h1>" + title + "</h1><article>" + "".join("<p>" + p + "</p>" for p in paragraphs) + "</article></html>"
    ).encode()
    snapshot, _ = core.pipeline.sources.persist(
        source, FetchResult("https://example.com/" + uid(), body, "text/html"), title, "2030-01-01T00:00:00Z"
    )
    core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
    candidate = core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    validate("ExtractionCandidate", candidate)
    return snapshot, candidate


def assert_literal_materials(core, candidate):
    proofs = {e["id"]: e for e in candidate["evidence"]}
    for value in candidate["materials"]:
        matching = [proofs[eid] for eid in candidate["field_evidence"]["materials"] if value in proofs[eid]["quote"]]
        assert matching
        for proof in matching:
            document = core.pipeline.parsed(proof["snapshot_id"])
            segment = next(s for s in document["segments"] if s["id"] == proof["segment_id"])
            assert value in proof["quote"] and proof["quote"] in segment["text"]
            assert proof["segment_id"] != "title"


def test_title_is_not_material_and_submit_object_is_concise_literal(core):
    _, candidate = extract(
        core,
        "【演示】奖学金材料提交",
        ["全体本科生请于2030-01-03前提交奖学金申请表、成绩单。", "办理入口：https://example.com/scholarship。"],
    )
    assert candidate["materials"] == ["奖学金申请表、成绩单"]
    assert candidate["title"] not in candidate["materials"]
    assert_literal_materials(core, candidate)


@pytest.mark.parametrize(
    "body",
    [
        "欢迎同学参观材料研究成果展，了解材料的发展。",
        "此次活动介绍申请表设计经验与材料整理方法，欢迎参加。",
        "本次活动无需提交申请表与成绩单。",
        "请同学注意材料通知，具体要求另行通知。",
    ],
)
def test_mention_promotion_or_negative_requirement_does_not_invent_materials(core, body):
    _, candidate = extract(core, "材料整理宣传活动", [body])
    assert candidate["materials"] == []
    assert candidate["field_evidence"]["materials"] == []


def test_explicit_material_list_keeps_actual_continuous_value(core):
    _, candidate = extract(
        core,
        "合成申请通知",
        [
            "全体本科生请于2030-01-03前提交申请。",
            "材料：项目设计方案、团队说明，报名入口：https://example.com/signup。",
        ],
    )
    assert candidate["materials"] == ["项目设计方案、团队说明"]
    assert_literal_materials(core, candidate)


def test_current_extractor_cache_is_idempotent(core):
    snapshot, candidate = extract(core, "合成申请通知", ["全体本科生请于2030-01-03前提交申请表、成绩单。"])
    assert core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]}) == candidate


def test_explicit_old_rule_reextraction_requires_review_and_preserves_old_accepted(core):
    with patch("server.campus.content.EXTRACTOR_VERSION", "campus-rules-1.0"):
        snapshot, original = extract(core, "合成申请通知", ["全体本科生请于2030-01-03前提交申请表、成绩单。"])
    if original["review_status"] != "accepted":
        original = core.pipeline.review(
            original["candidate_id"],
            {
                "expected_candidate_revision": original["revision"],
                "decision": "accept",
                "reason": "本地合成原文核对",
                "replacement_candidate": None,
            },
            {"id": uid()},
        )
    before_outbox = len([row for row in core.store.pending_outbox() if row["topic"] == "candidate.accepted"])
    upgraded = core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    assert upgraded["extractor_version"] != "campus-rules-1.0"
    assert upgraded["revision"] == original["revision"] + 1
    assert upgraded["review_status"] == "pending"
    assert upgraded["review_required"] is True
    assert "extractor_upgrade_requires_review" in upgraded["warnings"]
    assert len([row for row in core.store.pending_outbox() if row["topic"] == "candidate.accepted"]) == before_outbox
    assert core.pipeline.get_accepted_candidate(original["candidate_id"], original["revision"]) == original
    reviewed = core.pipeline.review(
        upgraded["candidate_id"],
        {
            "expected_candidate_revision": upgraded["revision"],
            "decision": "accept",
            "reason": "核对升级后的连续材料片段",
            "replacement_candidate": None,
        },
        {"id": uid()},
    )
    assert reviewed["review_status"] == "accepted"
    assert reviewed["materials"] == ["申请表、成绩单"]

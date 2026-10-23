"""Re-extracting stale candidates is what makes a rule upgrade reachable.

``extract_candidates`` already upgrades a candidate whose ``extractor_version``
is older than the running rules and flags it for review, but nothing called it
for snapshots that were extracted before. After the round-14 rule changes four
real notices stayed labelled ``change_kind=cancel`` — and therefore could never
be published — because no supported command re-ran extraction on them. An
operator-facing product needs that command.
"""

import pytest

from server.campus.content import EXTRACTOR_VERSION, Pipeline
from server.campus.platform import Core, Settings
from server.campus.schema import uid
from server.campus.sources import FetchResult
from server.campus.tests.test_integration import synthetic_source
from scripts import campus_collect as collect


@pytest.fixture
def core(tmp_path):
    instance = Core(Settings(data_dir=tmp_path, secret="reextract-" * 4, dev_auth=True))
    instance.pipeline = Pipeline(instance)
    yield instance
    instance.store.close()


def make_candidate(core, title, body_text):
    source = synthetic_source(core)
    body = f"<html><h1>{title}</h1><article><p>{body_text}</p></article></html>".encode()
    snapshot, _ = core.pipeline.sources.persist(
        source, FetchResult("https://example.com/" + uid(), body, "text/html"), title, "2026-10-08T12:00:00+08:00"
    )
    core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
    return core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})


def test_a_candidate_from_older_rules_is_re_extracted(core):
    candidate = make_candidate(core, "关于举办讲座的通知", "请于2026年10月20日前报名参加讲座。")
    stale = dict(candidate)
    stale["extractor_version"] = "campus-rules-1.0"
    core.store.put("candidates", candidate["candidate_id"], stale)

    summary = collect.reextract(core)

    assert summary["scanned"] == 1
    assert summary["upgraded"] == 1
    refreshed = core.store.get("candidates", candidate["candidate_id"])
    assert refreshed["extractor_version"] == EXTRACTOR_VERSION
    assert refreshed["revision"] > candidate["revision"]


def test_a_current_candidate_is_left_alone(core):
    candidate = make_candidate(core, "关于举办讲座的通知", "请于2026年10月20日前报名参加讲座。")
    summary = collect.reextract(core)
    assert summary["scanned"] == 0
    assert core.store.get("candidates", candidate["candidate_id"])["revision"] == candidate["revision"]


def test_the_upgrade_is_flagged_for_a_human(core):
    """A silent rule change would rewrite what a student sees."""
    candidate = make_candidate(core, "关于举办讲座的通知", "请于2026年10月20日前报名参加讲座。")
    stale = dict(candidate)
    stale["extractor_version"] = "campus-rules-1.0"
    core.store.put("candidates", candidate["candidate_id"], stale)

    collect.reextract(core)

    refreshed = core.store.get("candidates", candidate["candidate_id"])
    assert "extractor_upgrade_requires_review" in refreshed["warnings"]
    assert refreshed["review_status"] == "pending"


def test_reextraction_inside_one_version_is_a_no_op(core):
    """Re-running the same rules on the same text must not fabricate a revision.

    The pipeline keys a candidate on its input and its version, so a rerun that
    would produce the same row is a no-op. That is why a rule change must bump
    ``EXTRACTOR_VERSION`` — the version is the signal that stored rows are stale.
    """
    candidate = make_candidate(core, "关于雅思考试防疫要求的通知", "如遇疫情变化，考点可能取消当次考试。")
    assert candidate["extractor_version"] == EXTRACTOR_VERSION

    summary = collect.reextract(core, selectors=["雅思考试防疫要求"])

    assert summary["scanned"] == 1
    assert summary["upgraded"] == 0
    assert core.store.get("candidates", candidate["candidate_id"])["revision"] == candidate["revision"]


def test_an_unknown_selector_changes_nothing(core):
    candidate = make_candidate(core, "关于举办讲座的通知", "请于2026年10月20日前报名参加讲座。")
    summary = collect.reextract(core, selectors=["不存在的通知标题"])
    assert summary["scanned"] == 0
    assert core.store.get("candidates", candidate["candidate_id"])["revision"] == candidate["revision"]


def test_a_duplicate_of_an_already_upgraded_row_is_superseded_not_failed(core):
    """A stale duplicate cannot be refreshed, and that is not a failure.

    Extraction resolves a candidate by (snapshot, rules). When two rows describe
    the same document — which really happens, e.g. the same notice reached through
    two listing pages — the newer-rules row takes that slot, so the older duplicate
    has nothing to update. Its content is covered, and reporting it as a failed
    extraction made a healthy 249-candidate run report 8 failures.
    """
    fresh = make_candidate(core, "关于举办讲座的通知", "请于2026年10月20日前报名参加讲座。")
    duplicate = dict(fresh)
    duplicate["candidate_id"] = uid()
    duplicate["extractor_version"] = "campus-rules-1.0"
    duplicate["revision"] = 2
    core.store.put("candidates", duplicate["candidate_id"], duplicate)

    summary = collect.reextract(core)

    assert summary["scanned"] == 1
    assert summary["failed"] == []
    assert [row["candidate_id"] for row in summary["superseded"]] == [duplicate["candidate_id"]]

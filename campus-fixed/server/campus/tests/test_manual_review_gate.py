"""Automatic extraction and replayed queues must never authorize publication."""

import copy

import pytest

from server.campus.platform import APIError
from server.campus.schema import uid
from server.campus.tests.test_data_audit import (  # noqa: F401 -- env/notice are pytest fixtures registered by importing them into this module namespace; parameters do not count as "usage" for linters.
    env,
    notice,
)
from server.campus.worker import Worker


def test_clear_notice_stays_pending_until_a_human_accepts(env):
    _, core, _, accept = env
    fixture = notice(core)
    candidate = fixture["candidate"]
    assert candidate["review_status"] == "pending"
    assert candidate["review_required"] is True
    assert core.pipeline.get_accepted_candidate(candidate["candidate_id"], 1) is None
    assert not any(e["topic"] == "candidate.accepted" for e in core.store.pending_outbox())
    reviewed = accept(fixture)
    assert reviewed["accepted"]["revision"] == 2
    assert core.pipeline.get_accepted_candidate(candidate["candidate_id"], 2) == reviewed["accepted"]
    assert core.store.get("candidate_versions", f"{candidate['candidate_id']}:1") == candidate


def test_worker_cannot_publish_an_unreviewed_notice(env):
    _, core, _, accept = env
    fixture = notice(core)
    worker = Worker(core)
    for _ in range(5):
        assert worker.run_once(max_jobs=100, sweep=False)["failed"] == 0
    assert core.store.list("events") == []
    assert core.store.list("reviews") == []
    accept(fixture, apply=False)
    for _ in range(5):
        assert worker.run_once(max_jobs=100, sweep=False)["failed"] == 0
    assert len(core.store.list("events")) == 1


@pytest.mark.parametrize("audit", ["missing", "wrong_revision", "wrong_candidate", "rejected"])
def test_legacy_accepted_flag_does_not_replace_a_matching_accept_review(env, audit):
    _, core, _, _ = env
    candidate = copy.deepcopy(notice(core)["candidate"])
    candidate.update(review_status="accepted", review_required=False)
    key = f"{candidate['candidate_id']}:{candidate['revision']}"
    core.store.put("candidates", candidate["candidate_id"], candidate)
    core.store.put("candidate_versions", key, candidate)
    if audit != "missing":
        review_id = uid()
        core.store.put(
            "reviews",
            review_id,
            {
                "id": review_id,
                "candidate_id": uid() if audit == "wrong_candidate" else candidate["candidate_id"],
                "revision": candidate["revision"] + (audit == "wrong_revision"),
                "decision": "reject" if audit == "rejected" else "accept",
            },
        )
    with pytest.raises(APIError) as caught:
        core.events.reconcile_candidate(candidate["candidate_id"], candidate["revision"])
    assert caught.value.code == "candidate_not_accepted"
    assert core.store.list("events") == []
    assert core.store.get("candidate_versions", key) == candidate


def test_legacy_accepted_queue_job_is_blocked(env):
    _, core, _, _ = env
    candidate = copy.deepcopy(notice(core)["candidate"])
    candidate.update(review_status="accepted", review_required=False)
    core.store.put("candidates", candidate["candidate_id"], candidate)
    core.store.put("candidate_versions", f"{candidate['candidate_id']}:1", candidate)
    job = core.store.enqueue(
        "reconcile_event",
        {
            "candidate_id": candidate["candidate_id"],
            "candidate_revision": 1,
            "correlation_id": uid(),
        },
        "legacy-auto-accepted",
    )
    worker = Worker(core, handlers={"reconcile_event": core.handlers["reconcile_event"]})
    result = worker.run_once(max_jobs=100, sweep=False)
    assert result["failed"] == 1
    assert core.store.get_job(job["id"])["last_error_code"] == "candidate_not_accepted"
    assert core.store.list("events") == []


def test_reextracting_a_legacy_automatic_accept_requires_a_new_manual_review(env):
    _, core, _, _ = env
    fixture = notice(core)
    candidate = copy.deepcopy(fixture["candidate"])
    candidate.update(extractor_version="campus-rules-1.3", review_status="accepted", review_required=False)
    core.store.put("candidates", candidate["candidate_id"], candidate)
    core.store.put("candidate_versions", f"{candidate['candidate_id']}:1", candidate)
    updated = core.pipeline.extract_candidates({"snapshot_id": fixture["snapshot"]["snapshot_id"]})
    assert updated["revision"] == 2
    assert updated["review_status"] == "pending"
    assert updated["review_required"] is True
    assert core.store.get("candidate_versions", f"{candidate['candidate_id']}:1") == candidate


def test_unaudited_newer_flag_cannot_supersede_a_human_decision(env):
    _, core, _, accept = env
    reviewed = accept(notice(core), apply=False)["accepted"]
    legacy = copy.deepcopy(reviewed)
    legacy["revision"] += 1
    core.store.put("candidates", legacy["candidate_id"], legacy)
    core.store.put("candidate_versions", f"{legacy['candidate_id']}:{legacy['revision']}", legacy)
    event = core.events.reconcile_candidate(reviewed["candidate_id"], reviewed["revision"])
    assert event is not None
    assert event["publication_state"] == "published"

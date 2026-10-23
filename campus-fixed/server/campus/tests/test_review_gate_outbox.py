"""The legacy *outbox row*, not only a legacy queue job, must hit the review gate.

`test_manual_review_gate` covers a queue job that names an auto-accepted
candidate. The shape a real 1.3 database is left in is slightly different:
extraction emitted a ``candidate.accepted`` outbox row and let the dispatcher
fan it out later. That row can still be undispatched when the new code starts,
so the dispatcher — not just the handler — has to route it through the gate.

The positive control below matters as much as the negative one: it shows the
same path still publishes when a human really did accept, so the gate is not
merely blocking everything.
"""

import copy

from server.campus.schema import uid
from server.campus.tests.test_data_audit import (  # noqa: F401 -- env/notice are pytest fixtures registered by importing them into this module namespace; parameters do not count as "usage" for linters.
    env,
    notice,
)
from server.campus.worker import Worker


def legacy_auto_accepted(core):
    """A candidate as the 1.3 extractor left it: accepted flag, no review record."""
    candidate = copy.deepcopy(notice(core)["candidate"])
    candidate.update(review_status="accepted", review_required=False, extractor_version="campus-rules-1.3")
    core.store.put("candidates", candidate["candidate_id"], candidate)
    core.store.put("candidate_versions", f"{candidate['candidate_id']}:{candidate['revision']}", candidate)
    return candidate


def test_legacy_auto_accepted_outbox_row_cannot_publish(env):
    _, core, _, _ = env
    candidate = legacy_auto_accepted(core)
    core.store.emit(
        "candidate.accepted",
        candidate["candidate_id"],
        candidate["revision"],
        candidate_id=candidate["candidate_id"],
        candidate_revision=candidate["revision"],
    )
    assert "candidate.accepted" in [event["topic"] for event in core.store.pending_outbox()]

    worker = Worker(core)
    for _ in range(5):
        worker.run_once(max_jobs=100, sweep=False)

    assert core.store.list("events") == []
    assert core.store.pending_outbox() == [], "the row must be dispatched, not replayed forever"
    assert core.store.list("reviews") == []


def test_the_same_outbox_row_publishes_once_a_human_really_accepts(env):
    """Positive control: the dispatcher path is not broken, only gated."""
    _, core, _, accept = env
    fixture = accept(notice(core))
    assert fixture["event"]["publication_state"] == "published"
    assert any(event["topic"] == "candidate.accepted" for event in core.store.pending_outbox())

    worker = Worker(core)
    for _ in range(5):
        worker.run_once(max_jobs=100, sweep=False)

    assert core.store.pending_outbox() == []
    published = [event for event in core.store.list("events") if event["publication_state"] == "published"]
    assert len(published) == 1
    assert published[0]["id"] == fixture["event"]["id"]


def test_a_rejected_review_never_authorizes_publication(env):
    """A reject decision is a review record, but not an accept one."""
    _, core, _, _ = env
    fixture = notice(core)
    candidate = fixture["candidate"]
    reviewed = core.pipeline.review(
        candidate["candidate_id"],
        {
            "expected_candidate_revision": candidate["revision"],
            "decision": "reject",
            "reason": "仅合成用例：材料不足",
            "replacement_candidate": None,
        },
        {"id": uid()},
    )
    assert reviewed["review_status"] == "rejected"
    assert core.pipeline.get_accepted_candidate(candidate["candidate_id"], reviewed["revision"]) is None
    worker = Worker(core)
    for _ in range(3):
        worker.run_once(max_jobs=100, sweep=False)
    assert core.store.list("events") == []

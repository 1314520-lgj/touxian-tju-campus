"""Expired notices must disappear from everything a student can see.

The product is meant for daily use: a notice whose every deadline has passed is
clutter, and one stale notice at the top of 今日清单 is enough for a student to
stop opening it. Withdrawal uses the product's own ``withdrawn`` publication
state, so the notice leaves the feed, the public list, search and answers, while
the publication audit keeps a record of who removed it and why.

Undated notices are never withdrawn: they have no deadline to expire
("学位认证操作步骤" stays useful for years). A notice with any future deadline
stays too — one 报名截止 plus one 考试时间 in the past is still actionable.
"""

import datetime as dt
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.campus.platform import Settings
from server.campus.schema import uid
from server.campus.sources import FetchResult
from server.campus.tests.test_integration import synthetic_source
from server.campus_app import create_app

TODAY = dt.date(2026, 10, 20)


@pytest.fixture
def env(tmp_path):
    with patch.dict(os.environ, {"CAMPUS_MEILI_URL": "", "CAMPUS_DEMO": "false"}):
        app = create_app(
            Settings(data_dir=tmp_path, secret="expired-prune-" * 4, dev_auth=True, admin_emails={"admin@example.com"}),
            start_worker=False,
        )
        client = TestClient(app)
        yield app, app.state.core, client
        client.close()
        app.state.core.store.close()


def publish(core, paragraphs, title):
    source = synthetic_source(core)
    body = (
        "<html><h1>"
        + title
        + "</h1><article>"
        + "".join("<p>" + text + "</p>" for text in ["全体本科生请注意。", *paragraphs])
        + "</article></html>"
    ).encode()
    snapshot, _ = core.pipeline.sources.persist(
        source, FetchResult("https://example.com/" + uid(), body, "text/html"), title, "2026-09-01T00:00:00+08:00"
    )
    core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
    candidate = core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    candidate = core.pipeline.review(
        candidate["candidate_id"],
        {
            "expected_candidate_revision": candidate["revision"],
            "decision": "accept",
            "reason": "核对本地合成原文",
            "replacement_candidate": None,
        },
        {"id": uid()},
    )
    return core.events.reconcile_candidate(candidate["candidate_id"], candidate["revision"])


def expired_notice(core, title="过期通知"):
    return publish(core, ["报名截止时间：2020年1月5日。"], title)


def future_notice(core, title="未来通知"):
    return publish(core, ["报名截止时间：2027年3月5日。"], title)


def undated_notice(core, title="长期有效通知"):
    return publish(core, ["研究生学位认证请前往一站式服务大厅办理。"], title)


def test_an_expired_notice_is_withdrawn(env):
    _, core, _ = env
    event = expired_notice(core)
    report = core.events.withdraw_expired(TODAY, actor_id=uid(), apply=True)
    assert [row["id"] for row in report["expired"]] == [event["id"]]
    assert core.store.get("events", event["id"])["publication_state"] == "withdrawn"


def test_an_undated_notice_is_never_withdrawn(env):
    _, core, _ = env
    event = undated_notice(core)
    report = core.events.withdraw_expired(TODAY, actor_id=uid(), apply=True)
    assert report["expired"] == []
    assert core.store.get("events", event["id"])["publication_state"] == "published"


def test_a_notice_with_any_future_deadline_is_kept(env):
    _, core, _ = env
    event = publish(core, ["报名截止时间：2020年1月5日。", "考试时间：2027年6月7日。"], "半过期通知")
    report = core.events.withdraw_expired(TODAY, actor_id=uid(), apply=True)
    assert report["expired"] == []
    assert core.store.get("events", event["id"])["publication_state"] == "published"


def test_a_future_notice_is_kept(env):
    _, core, _ = env
    event = future_notice(core)
    core.events.withdraw_expired(TODAY, actor_id=uid(), apply=True)
    assert core.store.get("events", event["id"])["publication_state"] == "published"


def test_dry_run_reports_without_changing_anything(env):
    _, core, _ = env
    event = expired_notice(core)
    report = core.events.withdraw_expired(TODAY, actor_id=uid(), apply=False)
    assert [row["id"] for row in report["expired"]] == [event["id"]]
    assert report["applied"] is False
    assert core.store.get("events", event["id"])["publication_state"] == "published"


def test_rerunning_does_not_withdraw_twice(env):
    _, core, _ = env
    event = expired_notice(core)
    core.events.withdraw_expired(TODAY, actor_id=uid(), apply=True)
    version = core.store.get("events", event["id"])["version"]
    second = core.events.withdraw_expired(TODAY, actor_id=uid(), apply=True)
    assert second["expired"] == []
    assert core.store.get("events", event["id"])["version"] == version


def test_each_withdrawal_is_audited_with_a_reason(env):
    _, core, _ = env
    event = expired_notice(core)
    actor = uid()
    core.events.withdraw_expired(TODAY, actor_id=actor, reason="截止已过，自动下架", apply=True)
    audits = [
        row
        for row in core.store.list("publication_audits")
        if row["event_id"] == event["id"] and row["publication_state"] == "withdrawn"
    ]
    assert len(audits) == 1
    assert audits[0]["actor_id"] == actor
    assert audits[0]["reason"] == "截止已过，自动下架"


def test_a_withdrawn_notice_leaves_the_public_surfaces(env):
    _, core, client = env
    event = expired_notice(core, title="过期四六级通知")
    future_notice(core, title="未来四六级通知")
    core.events.withdraw_expired(TODAY, actor_id=uid(), apply=True)

    listing = client.get("/api/v1/events", params={"limit": 50}).json()
    ids = [item["id"] for item in listing.get("items") or []]
    assert event["id"] not in ids
    assert any("未来四六级" in (item.get("title") or "") for item in listing.get("items") or [])

    assert client.get(f"/api/v1/events/{event['id']}").status_code == 404
    hits = client.get("/api/v1/search", params={"q": "过期四六级"}).json().get("hits") or []
    assert all(hit.get("event_id") != event["id"] for hit in hits)
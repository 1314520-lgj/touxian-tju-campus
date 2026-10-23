"""The daily list must lead with the deadline that still matters.

The feed sorted by the *earliest* deadline an event mentions. Once expired
notices were withdrawn that still misfired: a notice carrying an old date next to
a future one (a 报名时间 in September and a 考试时间 in December) was ordered by
the September date, so the student's first card was headed by a date that had
already passed.

Ordering by the next upcoming deadline puts the soonest actionable item first and
keeps undated notice last, where they belong for a "what do I do today" list.
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


@pytest.fixture
def env(tmp_path):
    with patch.dict(os.environ, {"CAMPUS_MEILI_URL": "", "CAMPUS_DEMO": "false"}):
        app = create_app(
            Settings(data_dir=tmp_path, secret="feed-order-" * 4, dev_auth=True, admin_emails={"admin@example.com"}),
            start_worker=False,
        )
        client = TestClient(app)
        challenge = client.post("/api/v1/auth/challenges", json={"email": "student@example.com"})
        session = client.post(
            "/api/v1/auth/sessions",
            json={"challenge_id": challenge.json()["challenge_id"], "code": challenge.headers["X-Dev-Login-Code"]},
        ).json()
        yield app, app.state.core, client, {"X-CSRF-Token": session["csrf_token"]}
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


def feed_titles(client, cookie_headers):
    items = client.get("/api/v1/me/feed", params={"limit": 30}).json()["items"]
    return [(item.get("event") or {}).get("id") for item in items]


def test_the_next_upcoming_deadline_decides_the_order(env):
    _, core, client, _ = env
    # Mixed notice: a September registration window already closed, a December
    # exam ahead. Its next actionable date is December.
    mixed = publish(core, ["报名截止时间：2026年9月30日。", "考试时间：2026年12月20日。"], "混合期限通知")
    # A single deadline that lands sooner than the mixed notice's next one.
    sooner = publish(core, ["报名截止时间：2026年11月1日。"], "较早的通知")
    order = feed_titles(client, None)
    assert sooner["id"] in order and mixed["id"] in order
    assert order.index(sooner["id"]) < order.index(mixed["id"]), order


def test_an_undated_notice_comes_after_dated_ones(env):
    _, core, client, _ = env
    dated = publish(core, ["报名截止时间：2026年12月1日。"], "有期限的通知")
    undated = publish(core, ["研究生学位认证请前往一站式服务大厅办理。"], "无期限的通知")
    order = feed_titles(client, None)
    assert dated["id"] in order and undated["id"] in order
    assert order.index(dated["id"]) < order.index(undated["id"]), order


def test_a_past_deadline_never_leads_a_live_notice(env):
    """The card's label must not open with a date that has already gone."""
    _, core, client, _ = env
    publish(core, ["报名截止时间：2026年9月30日。", "考试时间：2026年12月20日。"], "混合期限通知")
    items = client.get("/api/v1/me/feed", params={"limit": 30}).json()["items"]
    first = items[0]
    label = first.get("deadline_label") or ""
    assert not label.startswith("2026-09-30"), label
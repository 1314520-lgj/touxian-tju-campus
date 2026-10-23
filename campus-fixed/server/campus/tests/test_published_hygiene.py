"""Published junk must leave the student's list, not just the review queue.

Opening the app on a phone showed it: "【考试安排】2022" three times, "总体情况",
"下载中心", a bare "文件来源：http://www.moe.gov.cn/…" and several news pieces were
sitting on the public list. They were published under the earlier, lenient gate;
hardening the gate only governs new candidates, so the old ones stayed.

Sweeping them needs one safety rule: never withdraw a notice that still has a
deadline ahead. Content a student can still act on outranks tidiness.
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
            Settings(data_dir=tmp_path, secret="published-hygiene-" * 4, dev_auth=True, admin_emails={"admin@example.com"}),
            start_worker=False,
        )
        client = TestClient(app)
        yield app, app.state.core, client
        client.close()
        app.state.core.store.close()


def publish(core, title, paragraphs=("本页为栏目汇总，具体安排见各通知。",)):
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


def test_a_column_fragment_is_withdrawn(env):
    _, core, _ = env
    event = publish(core, "【考试安排】2022")
    report = core.events.withdraw_unusable(actor_id=uid(), apply=True)
    assert [row["id"] for row in report["unusable"]] == [event["id"]]
    assert core.store.get("events", event["id"])["publication_state"] == "withdrawn"


def test_a_navigation_page_title_is_withdrawn(env):
    _, core, _ = env
    event = publish(core, "下载中心")
    core.events.withdraw_unusable(actor_id=uid(), apply=True)
    assert core.store.get("events", event["id"])["publication_state"] == "withdrawn"


def test_a_bare_url_title_is_withdrawn(env):
    _, core, _ = env
    event = publish(core, "文件来源：http://www.moe.gov.cn/srcsite/A22/x.html")
    core.events.withdraw_unusable(actor_id=uid(), apply=True)
    assert core.store.get("events", event["id"])["publication_state"] == "withdrawn"


def test_a_real_notice_survives(env):
    _, core, _ = env
    event = publish(core, "2027年寒假剑桥大学学术科研项目报名工作的通知")
    core.events.withdraw_unusable(actor_id=uid(), apply=True)
    assert core.store.get("events", event["id"])["publication_state"] == "published"


def test_junk_with_a_future_deadline_is_left_alone(env):
    """Tidiness never outranks something a student can still act on."""
    _, core, _ = env
    event = publish(core, "【考试安排】2027", ("报名截止时间：2027年3月5日。",))
    core.events.withdraw_unusable(actor_id=uid(), apply=True)
    assert core.store.get("events", event["id"])["publication_state"] == "published"


def test_dry_run_changes_nothing(env):
    _, core, _ = env
    event = publish(core, "总体情况")
    report = core.events.withdraw_unusable(actor_id=uid(), apply=False)
    assert [row["id"] for row in report["unusable"]] == [event["id"]]
    assert core.store.get("events", event["id"])["publication_state"] == "published"


def test_the_sweep_is_audited_and_idempotent(env):
    _, core, _ = env
    event = publish(core, "总体情况")
    actor = uid()
    core.events.withdraw_unusable(actor_id=actor, apply=True)
    audits = [
        row
        for row in core.store.list("publication_audits")
        if row["event_id"] == event["id"] and row["publication_state"] == "withdrawn"
    ]
    assert audits and audits[0]["actor_id"] == actor
    version = core.store.get("events", event["id"])["version"]
    again = core.events.withdraw_unusable(actor_id=actor, apply=True)
    assert again["unusable"] == []
    assert core.store.get("events", event["id"])["version"] == version


def test_withdrawn_junk_leaves_the_public_list(env):
    _, core, client = env
    junk = publish(core, "下载中心")
    publish(core, "2027年寒假剑桥大学学术科研项目报名工作的通知")
    core.events.withdraw_unusable(actor_id=uid(), apply=True)
    ids = [item["id"] for item in client.get("/api/v1/events", params={"limit": 50}).json()["items"]]
    assert junk["id"] not in ids
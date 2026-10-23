"""A notice with several different deadlines is not a conflicting notice.

Real 四六级 notice carries five school-level dates with different purposes:
笔试报名截止 9/24 17:00, 笔试考试 12/12, 口语报名截止 9/24 17:00,
学院报送材料 12/15, 解除限报申请 9/24. Asking 「四六级什么时候报名截止」
answered 「原文存在同类期限冲突，请核对来源」 — technically guarded, practically
useless: the notice states its deadlines clearly, and the student asked for them.

A real conflict is one *purpose* stated twice with different dates, and that is
visible because the two deadlines quote the same sentence.
"""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.campus.platform import Settings
from server.campus.schema import uid, validate
from server.campus.sources import FetchResult
from server.campus.tests.test_integration import synthetic_source
from server.campus_app import create_app


@pytest.fixture
def env(tmp_path):
    with patch.dict(os.environ, {"CAMPUS_MEILI_URL": "", "CAMPUS_DEMO": "false"}):
        app = create_app(
            Settings(data_dir=tmp_path, secret="answer-conflict-" * 4, dev_auth=True, admin_emails={"admin@example.com"}),
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


def publish(core, paragraphs, title="2026年下半年全国大学英语四、六级考试报名通知"):
    source = synthetic_source(core)
    # The audience line is what makes the event visible to an undergraduate
    # profile; without it the answer filter correctly drops the event.
    paragraphs = ["全体本科生请注意以下安排。", *paragraphs]
    body = (
        "<html><h1>"
        + title
        + "</h1><article>"
        + "".join("<p>" + text + "</p>" for text in paragraphs)
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


def ask(client, headers, question="四六级什么时候报名截止"):
    response = client.post(
        "/api/v1/answers",
        json={
            "question": question,
            "college_id": None,
            "education_level": "undergraduate",
            "include_historical": False,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    validate("Answer", response.json())
    return response.json()


def test_several_deadlines_of_different_purposes_are_listed(env):
    _, core, client, headers = env
    publish(
        core,
        [
            "四、六级笔试考试报名时间：2026年9月22日7:00—9月24日17:00。",
            "笔试考试时间：2026年12月12日。",
            "请考生所在学院于2026年12月15日前统一报送材料。",
        ],
    )
    answer = ask(client, headers)
    assert answer["status"] == "grounded", answer
    assert answer["citations"], answer
    assert "09-24" in answer["text"] or "9月24" in answer["text"], answer["text"]


def test_the_same_purpose_stated_twice_is_still_flagged(env):
    """Two different dates for the same quoted instruction is a real conflict."""
    _, core, client, headers = env
    publish(
        core,
        [
            "报名截止时间：2026年9月24日17:00。",
            "报名截止时间：2026年9月28日17:00。",
        ],
    )
    answer = ask(client, headers)
    assert answer["status"] == "conflicting_sources", answer
    assert "冲突" in answer["text"]


def test_three_school_level_dates_of_different_purposes_are_listed(env):
    """The production shape: several school-level dates, each with its own purpose.

    Every one of these extracts to ``school_deadline``, so grouping by kind alone
    reports a conflict for a notice that simply states three different things.
    """
    _, core, client, headers = env
    publish(
        core,
        [
            "四、六级笔试考试报名时间：2026年9月22日7:00—9月24日17:00。",
            "请考生所在学院于2026年12月15日前统一将申请解除限报材料报送教务处。",
            "应于2026年9月24日（报名结束前）向所在学院提出正式书面申请。",
        ],
    )
    answer = ask(client, headers)
    assert answer["status"] == "grounded", answer
    assert answer["citations"], answer


def test_one_conflicting_notice_does_not_blank_the_whole_answer(env):
    """A clean notice must still be answered when another hit is self-conflicting.

    Returning on the first conflict threw away every other notice in the result
    set, so one ambiguous document made the question unanswerable even though a
    perfectly clear notice was right next to it.
    """
    _, core, client, headers = env
    publish(
        core,
        ["报名截止时间：2026年9月24日17:00。", "报名截止时间：2026年9月28日17:00。"],
        title="四、六级报名时间冲突版",
    )
    publish(
        core,
        ["四、六级笔试考试报名时间：2026年12月20日17:00。"],
        title="四、六级报名时间清晰版",
    )
    answer = ask(client, headers)
    assert answer["status"] == "grounded", answer
    assert answer["citations"], answer
    joined = " ".join(c["quote"] for c in answer["citations"])
    assert "12月20日" in joined or "12-20" in joined, joined


def test_skipped_conflict_is_reported_so_the_student_knows(env):
    _, core, client, headers = env
    publish(
        core,
        ["报名截止时间：2026年9月24日17:00。", "报名截止时间：2026年9月28日17:00。"],
        title="四、六级报名时间冲突版",
    )
    publish(core, ["四、六级笔试考试报名时间：2026年12月20日17:00。"], title="四、六级报名时间清晰版")
    answer = ask(client, headers)
    assert "冲突" in answer["text"] or any("冲突" in (c.get("note") or "") for c in answer["citations"])


def test_a_single_deadline_answers_normally(env):
    _, core, client, headers = env
    publish(core, ["报名截止时间：2026年9月24日17:00。"])
    answer = ask(client, headers)
    assert answer["status"] == "grounded"
    assert any("09-24" in c["quote"] or "9月24" in c["quote"] for c in answer["citations"])

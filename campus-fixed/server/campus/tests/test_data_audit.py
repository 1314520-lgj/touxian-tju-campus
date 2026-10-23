"""Data correctness regressions use real SQLite, HTTP and immutable parsed notices."""

import copy
import os
from collections import Counter
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.campus.platform import Settings
from server.campus.schema import uid, validate
from server.campus.tests.test_integration import synthetic_source
from server.campus_app import create_app


@pytest.fixture
def env(tmp_path):
    with patch.dict(os.environ, {"CAMPUS_MEILI_URL": "", "CAMPUS_DEMO": "false"}):
        app = create_app(
            Settings(
                data_dir=tmp_path, secret="audit-data-secret-" * 4, dev_auth=True, admin_emails={"admin@example.com"}
            ),
            start_worker=False,
        )
        clients = []

        def auth(email="student@example.com"):
            client = TestClient(app)
            clients.append(client)
            challenge = client.post("/api/v1/auth/challenges", json={"email": email})
            session = client.post(
                "/api/v1/auth/sessions",
                json={"challenge_id": challenge.json()["challenge_id"], "code": challenge.headers["X-Dev-Login-Code"]},
            ).json()
            return client, session["user"], {"X-CSRF-Token": session["csrf_token"]}

        core = app.state.core

        def accept(fixture, replacement=None, apply=True):
            candidate = fixture["candidate"]
            if candidate["review_status"] != "accepted" or replacement is not None:
                candidate = core.pipeline.review(
                    candidate["candidate_id"],
                    {
                        "expected_candidate_revision": candidate["revision"],
                        "decision": "accept",
                        "reason": "核对本地合成原文",
                        "replacement_candidate": replacement,
                    },
                    {"id": uid()},
                )
            event = core.events.reconcile_candidate(candidate["candidate_id"], candidate["revision"]) if apply else None
            return {**fixture, "accepted": candidate, "event": event}

        yield app, core, auth, accept
        for client in clients:
            client.close()
        core.store.close()


def notice(core, source=None, url=None, title="合成专业确认", level="本科", date="2030-01-03", extra=""):
    source = source or synthetic_source(core)
    from server.campus.sources import FetchResult

    body = (
        f"<html><h1>{title}</h1><article><p>全体{level}生请于{date}前提交确认表。</p>"
        f"<p>材料：确认表。</p>{extra}</article></html>"
    ).encode()
    snapshot, _ = core.pipeline.sources.persist(
        source, FetchResult(url or "https://example.com/" + uid(), body, "text/html"), title, "2030-01-01T00:00:00Z"
    )
    core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
    candidate = core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    return {"source": source, "url": snapshot["source_url"], "snapshot": snapshot, "candidate": candidate}


def test_out_of_order_older_accepted_snapshot_cannot_undo_new_deadline(env):
    _, core, _, accept = env
    older = accept(notice(core), apply=False)
    newer = accept(notice(core, source=older["source"], url=older["url"], date="2030-01-05"))
    replay = core.events.reconcile_candidate(older["accepted"]["candidate_id"], older["accepted"]["revision"])
    current = core.events.get_public(newer["event"]["id"])
    assert current["version"] == 1
    assert current["deadlines"][0]["date"] == "2030-01-05"
    assert replay == current
    assert len(core.store.list("events")) == 1


def test_out_of_order_old_review_revision_is_skipped(env):
    _, core, _, accept = env
    first = accept(notice(core), apply=False)
    # Legal re-review increments a candidate revision without fabricating fields.
    later = accept({**first, "candidate": first["accepted"]}, replacement=first["accepted"])
    current = core.events.reconcile_candidate(first["accepted"]["candidate_id"], first["accepted"]["revision"])
    assert current == later["event"]
    assert current["version"] == 1


@pytest.mark.parametrize("level", ["硕士", "博士"])
def test_cross_source_same_title_deadline_different_level_not_merged(env, level):
    _, core, _, accept = env
    a = accept(notice(core))
    b = accept(notice(core, level=level))
    assert a["event"]["id"] != b["event"]["id"]


def test_current_evidence_excludes_superseded_same_document(env):
    app, core, _, accept = env
    old = accept(notice(core))
    new = accept(notice(core, source=old["source"], url=old["url"], date="2030-01-05"))
    assert old["snapshot"]["snapshot_id"] not in {e["snapshot_id"] for e in new["event"]["evidence"]}
    assert old["snapshot"]["snapshot_id"] in {
        e["snapshot_id"] for e in core.events.get_public(old["event"]["id"], 1)["evidence"]
    }
    with TestClient(app) as client:
        assert client.get("/api/v1/evidence/" + old["event"]["evidence"][0]["id"]).status_code == 200


def test_server_attention_change_invalidates_old_state_cas(env):
    _, core, auth, accept = env
    original = accept(notice(core))
    client, _, headers = auth()
    path = "/api/v1/me/events/" + original["event"]["id"] + "/state"
    state = client.patch(
        path, json={"expected_state_revision": 0, "acknowledged_event_version": 1, "saved": True}, headers=headers
    ).json()
    accept(notice(core, source=original["source"], url=original["url"], date="2030-01-05"))
    stale = client.patch(
        path,
        json={"expected_state_revision": state["state_revision"], "acknowledged_event_version": 1, "read": True},
        headers=headers,
    )
    assert stale.status_code == 409


def test_semantic_repost_does_not_create_attention_from_stale_ack(env):
    _, core, auth, accept = env
    original = accept(notice(core))
    client, _, headers = auth()
    path = "/api/v1/me/events/" + original["event"]["id"] + "/state"
    state = client.patch(
        path, json={"expected_state_revision": 0, "acknowledged_event_version": 1, "saved": True}, headers=headers
    ).json()
    repost = accept(notice(core))
    assert repost["event"]["changed_fields"] == []
    changed = client.patch(
        path,
        json={"expected_state_revision": state["state_revision"], "acknowledged_event_version": 1, "read": True},
        headers=headers,
    )
    assert changed.status_code == 200
    assert changed.json()["needs_attention"] is False


def test_unacknowledged_important_change_survives_later_repost(env):
    _, core, auth, accept = env
    original = accept(notice(core))
    client, _, headers = auth()
    path = "/api/v1/me/events/" + original["event"]["id"] + "/state"
    client.patch(
        path, json={"expected_state_revision": 0, "acknowledged_event_version": 1, "saved": True}, headers=headers
    )
    updated = accept(notice(core, source=original["source"], url=original["url"], date="2030-01-05"))
    repost = accept(notice(core, date="2030-01-05"))
    assert repost["event"]["changed_fields"] == []
    assert "deadlines" in core.events.changed_since(original["event"]["id"], 1, repost["event"])
    assert core.events.changed_since(original["event"]["id"], updated["event"]["version"], repost["event"]) == set()


def test_search_folds_segments_without_losing_unextracted_body_match(env):
    app, core, _, accept = env
    paragraphs = "".join(f"<p>银河计数器编号{i:02}。完成测试。</p>" for i in range(30))
    a = accept(notice(core, extra=paragraphs))
    b = accept(notice(core, title="另一合成专业确认", extra="<p>银河计数器特别标记。</p>"))
    with TestClient(app) as client:
        response = client.get("/api/v1/search", params={"q": "银河计数器", "limit": 2})
        assert response.status_code == 200
        hits = response.json()["hits"]
        assert {h["event_id"] for h in hits} == {a["event"]["id"], b["event"]["id"]}
        assert all("银河计数器" in h["excerpt"] for h in hits)
        assert response.json()["next_cursor"] is None
        for hit in hits:
            proof = client.get("/api/v1/evidence/" + hit["evidence_id"]).json()
            assert proof["quote"] == hit["excerpt"]
            assert proof["source_url"] == hit["source_url"]


def test_search_only_materializes_bounded_best_proofs(env):
    _, core, _, accept = env
    accept(notice(core, extra="".join(f"<p>星河标记{i:02}号。</p>" for i in range(50))))
    original = core.pipeline.register_evidence
    with patch.object(core.pipeline, "register_evidence", wraps=original) as register:
        rows = core.search.hits("星河标记")
    assert 1 <= len(rows) <= 3
    assert register.call_count <= 3


def test_search_whitespace_is_rejected_instead_of_matching_everything(env):
    app, core, _, accept = env
    accept(notice(core))
    with TestClient(app) as client:
        response = client.get("/api/v1/search", params={"q": "   "})
        assert response.status_code == 422


def test_historical_deadline_disagreement_is_flagged(env):
    _, core, auth, accept = env
    old = accept(notice(core))
    accept(notice(core, source=old["source"], url=old["url"], date="2030-01-05"))
    client, _, headers = auth()
    response = client.post(
        "/api/v1/answers",
        json={
            "question": "合成专业确认什么时候截止",
            "college_id": None,
            "education_level": "undergraduate",
            "include_historical": True,
        },
        headers=headers,
    )
    assert response.status_code == 200
    validate("Answer", response.json())
    assert response.json()["status"] == "conflicting_sources"
    assert "历史" in response.json()["text"]


def test_missing_evidence_is_not_replaced_by_a_bare_date_answer(env):
    _, core, auth, accept = env
    accept(notice(core))
    client, _, headers = auth()
    response = client.post(
        "/api/v1/answers",
        json={
            "question": "火星飞船登记什么时候截止",
            "college_id": None,
            "education_level": None,
            "include_historical": False,
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "insufficient_evidence"
    assert response.json()["citations"] == []


def test_snapshot_access_scans_versions_once(env):
    _, core, _, accept = env
    records = [accept(notice(core, title=f"合成独立通知{i}", date=f"2030-01-{i + 3:02}")) for i in range(5)]
    original = core.store.list
    calls = Counter()

    def counted(collection):
        calls[collection] += 1
        return original(collection)

    with patch.object(core.store, "list", side_effect=counted):
        assert core.events.can_read_snapshot(records[-1]["snapshot"]["snapshot_id"])
    assert calls["event_versions"] <= 1


def test_key_change_removes_other_source_stale_current_body(env):
    app, core, _, accept = env
    old_marker = "<p>旧甲乙丙丁戊。</p>"
    first = accept(notice(core, extra=old_marker))
    repost = accept(notice(core, extra=old_marker))
    assert first["event"]["id"] == repost["event"]["id"]
    newer = accept(
        notice(core, source=first["source"], url=first["url"], date="2030-01-05", extra="<p>新己庚辛壬癸。</p>")
    )
    assert core.events.current_snapshot_ids(newer["event"]["id"]) == [newer["snapshot"]["snapshot_id"]]
    with TestClient(app) as client:
        assert client.get("/api/v1/search", params={"q": "旧甲乙丙丁戊"}).json()["hits"] == []
        assert client.get("/api/v1/search", params={"q": "旧甲乙丙丁戊", "include_historical": True}).json()["hits"]
        fresh = client.get("/api/v1/search", params={"q": "新己庚辛壬癸"}).json()["hits"]
        assert len(fresh) == 1
        assert "新己庚辛壬癸" in fresh[0]["excerpt"]


@pytest.mark.parametrize("manual_field", ["action_required", "audience"])
def test_reviewed_requirement_or_audience_correction_flags_saved_item(env, manual_field):
    _, core, auth, accept = env
    from server.campus.sources import FetchResult

    source = synthetic_source(core)
    title = "合成专业确认"
    # Simulate a manually accepted 1.1 record, including its persisted decision.
    # Historical rules allowed these errors; today's validator must reject them.
    text = (
        "全体本科生请于2030-01-03前自愿提交确认表。"
        if manual_field == "action_required"
        else "全体具有本科学历的硕士生请于2030-01-03前提交确认表。"
    )
    snapshot, _ = core.pipeline.sources.persist(
        source,
        FetchResult(
            "https://example.com/" + uid(),
            f"<html><h1>{title}</h1><article><p>{text}</p></article></html>".encode(),
            "text/html",
        ),
        title,
        "2030-01-01T00:00:00Z",
    )
    core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
    candidate = core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    candidate = copy.deepcopy(candidate)
    candidate.update(extractor_version="campus-rules-1.1", review_status="accepted", review_required=False)
    if manual_field == "action_required":
        candidate["action_required"] = True
    else:
        candidate["audience"]["education_levels"] = ["undergraduate", "master"]
    validate("ExtractionCandidate", candidate)
    core.store.put("candidates", candidate["candidate_id"], candidate)
    core.store.put("candidate_versions", f"{candidate['candidate_id']}:{candidate['revision']}", candidate)
    review_id = uid()
    core.store.put(
        "reviews",
        review_id,
        {
            "id": review_id,
            "candidate_id": candidate["candidate_id"],
            "revision": candidate["revision"],
            "decision": "accept",
            "reviewer_id": uid(),
            "reason": "Synthetic historical review under version 1.1",
        },
    )
    fixture = {"candidate": candidate}
    first = accept(fixture)
    candidate = first["accepted"]
    fixture = {"candidate": candidate}
    client, user, headers = auth()
    path = "/api/v1/me/events/" + first["event"]["id"] + "/state"
    client.patch(
        path, json={"expected_state_revision": 0, "acknowledged_event_version": 1, "saved": True}, headers=headers
    )
    corrected = copy.deepcopy(candidate)
    if manual_field == "action_required":
        corrected["action_required"] = False
    else:
        corrected["audience"]["education_levels"] = ["master"]
    changed = accept(fixture, replacement=corrected)
    assert changed["event"]["changed_fields"] == [manual_field]
    state = client.get(path).json()["state"]
    assert state["needs_attention"] is True
    assert manual_field in core.events.IMPORTANT_FIELDS


def test_acknowledged_version_cannot_move_backwards(env):
    _, core, auth, accept = env
    original = accept(notice(core))
    client, _, headers = auth()
    path = "/api/v1/me/events/" + original["event"]["id"] + "/state"
    client.patch(
        path, json={"expected_state_revision": 0, "acknowledged_event_version": 1, "saved": True}, headers=headers
    )
    updated = accept(notice(core, source=original["source"], url=original["url"], date="2030-01-05"))
    state = client.get(path).json()["state"]
    accepted = client.patch(
        path,
        json={
            "expected_state_revision": state["state_revision"],
            "acknowledged_event_version": updated["event"]["version"],
            "read": True,
        },
        headers=headers,
    ).json()
    stale_ack = client.patch(
        path,
        json={"expected_state_revision": accepted["state_revision"], "acknowledged_event_version": 1, "read": False},
        headers=headers,
    )
    assert stale_ack.status_code == 409


def test_reviewed_cross_source_late_job_cannot_restore_old_deadline(env):
    _, core, _, accept = env
    original = accept(notice(core))
    late = notice(core)
    linked = copy.deepcopy(late["candidate"])
    linked["target_event_id"] = original["event"]["id"]
    late = accept(late, replacement=linked, apply=False)
    updated = accept(notice(core, source=original["source"], url=original["url"], date="2030-01-05"))
    result = core.events.reconcile_candidate(late["accepted"]["candidate_id"], late["accepted"]["revision"])
    assert result["version"] == updated["event"]["version"]
    assert result["deadlines"][0]["date"] == "2030-01-05"


def test_late_index_finish_does_not_roll_back_new_receipt(env):
    _, core, _, accept = env
    fixture = accept(notice(core))
    event = fixture["event"]
    core.search.url = "http://search.fixture.invalid"

    def fake_meili(method, path, body=None):
        # Protocol fake; this test makes no claim that Meili is live locally.
        if path == "/indexes":
            return {"results": [{"uid": core.search.index}]}
        if path.endswith("/documents/delete"):
            changed = core.events.publication(
                event["id"],
                {"expected_event_version": 1, "publication_state": "withdrawn", "reason": "模拟后续索引已经完成"},
                uid(),
            )
            core.store.put("search_receipts", event["id"], {"event_id": event["id"], "version": changed["version"]})
        return {}

    with patch.object(core.search, "_http", side_effect=fake_meili):
        core.search.index_event({"subject": {"kind": "public_event", "id": event["id"], "revision": 1}})
    assert core.store.get("search_receipts", event["id"])["version"] == 2


def test_meili_excerpt_uses_actual_matching_body_not_first_500_characters(env):
    _, core, _, accept = env
    rare = "量子蓝田登记代号"
    fixture = accept(notice(core, extra="<p>" + ("普通叙述" * 160) + rare + "。</p>"))
    event = fixture["event"]
    sid = fixture["snapshot"]["snapshot_id"]
    segment = next(s for s in core.pipeline.segments([sid]) if rare in s["text"])
    core.search.url = "http://search.fixture.invalid"
    response = {
        "hits": [
            {
                "event_id": event["id"],
                "event_version": 1,
                "snapshot_id": sid,
                "segment_id": segment["id"],
                "text": segment["text"],
            }
        ]
    }
    with patch.object(core.search, "_http", return_value=response):
        hits = core.search.search(rare)["hits"]
    assert len(hits) == 1
    assert rare in hits[0]["excerpt"]
    assert hits[0]["excerpt"] in segment["text"]


def test_newer_pending_attachment_does_not_suppress_last_approved_parent(env):
    _, core, _, accept = env
    approved = accept(notice(core), apply=False)
    from server.campus.sources import FetchResult

    parent = approved["snapshot"]
    attachment, _ = core.pipeline.sources.persist(
        approved["source"],
        FetchResult(
            "https://example.com/attachment-" + uid() + ".txt", "请于周五前提交补充材料。".encode(), "text/plain"
        ),
        "合成待核对附件",
        None,
        parent["snapshot_id"],
    )
    core.pipeline.parse_document({"snapshot_id": attachment["snapshot_id"]})
    pending = core.pipeline.extract_candidates({"snapshot_id": parent["snapshot_id"]})
    assert pending["revision"] > approved["accepted"]["revision"]
    assert pending["review_status"] == "pending"
    event = core.events.reconcile_candidate(approved["accepted"]["candidate_id"], approved["accepted"]["revision"])
    assert event is not None
    assert event["deadlines"][0]["date"] == "2030-01-03"
    assert core.events.current_snapshot_ids(event["id"]) == [parent["snapshot_id"]]


def test_multi_field_question_returns_each_proven_field_and_names_missing_entry(env):
    _, core, auth, accept = env
    first = accept(notice(core))
    client, _, headers = auth()
    body = {
        "question": "合成专业确认什么时候截止，需要什么材料，报名入口在哪里",
        "college_id": None,
        "education_level": "undergraduate",
        "include_historical": False,
    }
    missing = client.post("/api/v1/answers", json=body, headers=headers)
    assert missing.status_code == 200
    assert missing.json()["status"] == "grounded"
    assert "2030-01-03" in missing.json()["text"]
    assert "确认表" in missing.json()["text"]
    assert "入口" in missing.json()["text"] and "未找到" in missing.json()["text"]
    assert missing.json()["text"].count(first["event"]["title"]) == 1
    url = "https://example.com/signup-audit"
    updated = accept(notice(core, source=first["source"], url=first["url"], extra="<p>办理入口：" + url + "。</p>"))
    answer = client.post("/api/v1/answers", json=body, headers=headers)
    assert answer.status_code == 200
    validate("Answer", answer.json())
    assert answer.json()["status"] == "grounded"
    assert "2030-01-03" in answer.json()["text"]
    assert "确认表" in answer.json()["text"]
    assert url in answer.json()["text"]
    assert answer.json()["text"].count(updated["event"]["title"]) == 1
    quotes = [c["quote"] for c in answer.json()["citations"]]
    assert any("2030-01-03" in q for q in quotes)
    assert any("材料" in q for q in quotes)
    assert any(url in q for q in quotes)

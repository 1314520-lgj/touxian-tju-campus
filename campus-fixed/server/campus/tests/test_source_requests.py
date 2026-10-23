"""Real HTTP/SQLite checks for student choice and operator source admission."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.campus.platform import Settings
from server.campus.schema import uid, validate
from server.campus.tests.test_integration import prepare_notice, synthetic_source
from server.campus.worker import Worker
from server.campus_app import create_app


@pytest.fixture
def live(tmp_path):
    app = create_app(
        Settings(
            data_dir=tmp_path, secret="source-request-tests-" * 3, dev_auth=True, admin_emails={"admin@example.com"}
        ),
        start_worker=False,
    )
    clients = []

    def login(email):
        client = TestClient(app)
        clients.append(client)
        challenge = client.post("/api/v1/auth/challenges", json={"email": email})
        result = client.post(
            "/api/v1/auth/sessions",
            json={"challenge_id": challenge.json()["challenge_id"], "code": challenge.headers["X-Dev-Login-Code"]},
        ).json()
        return client, result["user"], {"X-CSRF-Token": result["csrf_token"]}

    first = login("first@example.com")
    second = login("second@example.com")
    admin = login("admin@example.com")
    yield app.state.core, first, second, admin
    for client in clients:
        client.close()
    app.state.core.store.close()


def submit(account, name="合成网站申请", kind="website", url="https://example.com/notices", auto=True, key=None):
    client, user, headers = account
    return client.post(
        "/api/v1/me/source-requests",
        json={"name": name, "kind": kind, "url": url, "auto_subscribe": auto},
        headers={**headers, "Idempotency-Key": key or uid()},
    )


def resolve(account, request, source_id=None, decision="connect", note="核验并完成合成采集测试", key=None):
    client, user, headers = account
    return client.post(
        f"/api/v1/admin/source-requests/{request['id']}/resolve",
        json={"decision": decision, "source_id": source_id, "note": note},
        headers={**headers, "Idempotency-Key": key or uid()},
    )


def test_request_is_private_and_does_not_fetch_or_register_student_url(live):
    core, first, second, admin = live
    with patch.object(
        core.pipeline.sources.fetcher, "fetch", side_effect=AssertionError("Student requests must not fetch")
    ):
        response = submit(first)
    assert response.status_code == 201
    record = response.json()
    validate("SourceRequest", record)
    assert record["status"] == "pending" and record["user_id"] == first[1]["id"]
    assert core.store.list("sources") == [] and core.store.jobs() == []
    assert first[0].get("/api/v1/me/source-requests").json()["items"] == [record]
    assert second[0].get("/api/v1/me/source-requests").json()["items"] == []
    assert second[0].get("/api/v1/admin/source-requests").status_code == 403
    assert admin[0].get("/api/v1/admin/source-requests").json()["items"] == [record]


def test_idempotent_and_normalized_requests_do_not_duplicate(live):
    core, first, *_ = live
    record = submit(first, name="  合成网站申请  ", key="stable").json()
    assert submit(first, name="合成网站申请", key="stable").json()["id"] == record["id"]
    assert submit(first, name="合成网站申请", key="another").json()["id"] == record["id"]
    assert submit(first, name="不同申请", key="stable").status_code == 409
    assert len(core.store.list("source_requests")) == 1


def test_maximum_idempotency_key_fits_postgresql_record_key_and_replays(live):
    core, first, _, admin = live
    key = "k" * 200
    record = submit(first, key=key).json()
    assert submit(first, key=key).json()["id"] == record["id"]
    result = resolve(admin, record, decision="reject", key=key)
    assert result.status_code == 200
    assert resolve(admin, record, decision="reject", key=key).json() == result.json()
    from sqlalchemy import select

    with core.store.engine.connect() as connection:
        keys = (
            connection.execute(
                select(core.store.records.c.key).where(core.store.records.c.collection == "source_request_receipts")
            )
            .scalars()
            .all()
        )
    assert len(keys) == 2 and all(len(value) <= 255 for value in keys)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/",
        "http://localhost/",
        "http://metadata.google.internal/",
        "https://user:password@example.com/",
        "https://example.com/?access_token=private",
        "javascript:alert(1)",
    ],
)
def test_unsafe_or_secret_urls_are_rejected_without_network(live, url):
    core, first, *_ = live
    result = submit(first, url=url)
    assert result.status_code == 422
    validate("Error", result.json())
    assert core.store.list("source_requests") == []


def test_wechat_name_or_public_article_is_accepted_but_session_parameters_are_not_stored(live):
    _, first, *_ = live
    assert submit(first, name="合成公众号申请", kind="wechat", url=None).status_code == 201
    result = submit(
        first,
        name="合成公众号文章",
        kind="wechat",
        url="https://mp.weixin.qq.com/s?__biz=PUBLIC&mid=123&sn=ARTICLE&uin=PRIVATE&pass_ticket=PRIVATE",
    )
    assert result.status_code == 201
    assert result.json()["url"] == "https://mp.weixin.qq.com/s?__biz=PUBLIC&mid=123&sn=ARTICLE"
    assert submit(first, kind="wechat", url="https://example.com/article").status_code == 422


def test_student_cannot_forge_owner_resolve_or_bypass_csrf(live):
    _, first, second, admin = live
    record = submit(first).json()
    assert resolve(second, record, decision="reject").status_code == 403
    assert (
        first[0]
        .post(
            "/api/v1/me/source-requests",
            json={"name": "x", "kind": "website", "url": "https://example.com/", "auto_subscribe": True},
            headers={"Idempotency-Key": uid()},
        )
        .status_code
        == 403
    )
    assert (
        first[0]
        .post(
            "/api/v1/me/source-requests",
            json={
                "name": "x",
                "kind": "website",
                "url": "https://example.com/",
                "auto_subscribe": True,
                "user_id": second[1]["id"],
            },
            headers={**first[2], "Idempotency-Key": uid()},
        )
        .status_code
        == 422
    )


def test_resolution_requires_verified_successful_source_then_follows_only_requester(live):
    core, first, second, admin = live
    for account in [first, second]:
        assert (
            account[0]
            .put(
                "/api/v1/me/subscriptions",
                headers=account[2],
                json={"source_ids": [], "event_types": ["scholarship"], "source_mode": "selected"},
            )
            .status_code
            == 200
        )
    record = submit(first).json()
    source = synthetic_source(core)
    source.update(health="never_checked", last_success_at=None, verification="unverified")
    core.store.put("sources", source["id"], source)
    assert resolve(admin, record, source["id"]).status_code == 409
    assert first[0].get("/api/v1/me/source-requests").json()["items"][0]["status"] == "pending"
    assert first[0].get("/api/v1/me/subscriptions").json()["source_ids"] == []
    source.update(health="healthy", last_success_at="2030-01-01T00:00:00Z", verification="verified_organization")
    core.store.put("sources", source["id"], source)
    done = resolve(admin, record, source["id"], key="connect-once")
    assert done.status_code == 200
    validate("SourceRequest", done.json())
    assert resolve(admin, record, source["id"], key="connect-once").json() == done.json()
    assert resolve(admin, record, decision="reject").status_code == 409
    assert first[0].get("/api/v1/me/subscriptions").json() == {
        "source_ids": [source["id"]],
        "event_types": ["scholarship"],
        "source_mode": "selected",
    }
    assert second[0].get("/api/v1/me/subscriptions").json()["source_ids"] == []


def test_no_auto_follow_and_rejection_preserve_preferences(live):
    core, first, _, admin = live
    source = synthetic_source(core)
    before = first[0].get("/api/v1/me/subscriptions").json()
    record = submit(first, auto=False).json()
    assert resolve(admin, record, source["id"]).status_code == 200
    rejected = submit(first, name="另一合成申请", url="https://example.com/other").json()
    result = resolve(admin, rejected, decision="reject", note="合成示例尚无授权")
    assert result.status_code == 200 and result.json()["status"] == "rejected"
    assert first[0].get("/api/v1/me/subscriptions").json() == before


def test_wechat_request_cannot_attach_to_website(live):
    core, first, _, admin = live
    record = submit(first, kind="wechat", url=None).json()
    source = synthetic_source(core)
    assert resolve(admin, record, source["id"]).status_code == 409
    source["kind"] = "wechat"
    core.store.put("sources", source["id"], source)
    assert resolve(admin, record, source["id"]).status_code == 200


def test_empty_selection_is_distinct_from_all_and_keeps_saved_events(live):
    core, first, *_ = live
    notice = prepare_notice(core)
    candidate = notice["candidate"]
    if candidate["review_status"] != "accepted":
        candidate = core.pipeline.review(
            candidate["candidate_id"],
            {
                "expected_candidate_revision": candidate["revision"],
                "decision": "accept",
                "reason": "合成测试",
                "replacement_candidate": None,
            },
            first[1],
        )
    event = core.events.reconcile_candidate(candidate["candidate_id"], candidate["revision"])

    def choose(mode, ids=None):
        ids = [] if ids is None else ids
        response = first[0].put(
            "/api/v1/me/subscriptions",
            headers=first[2],
            json={"source_ids": ids, "event_types": [], "source_mode": mode},
        )
        assert response.status_code == 200
        assert response.json()["source_mode"] == mode
        return first[0].get("/api/v1/me/feed").json()["items"]

    assert len(choose("all")) == 1
    assert choose("selected") == []
    assert len(choose("selected", [notice["source"]["id"]])) == 1
    assert (
        first[0]
        .patch(
            f"/api/v1/me/events/{event['id']}/state",
            headers=first[2],
            json={"expected_state_revision": 0, "acknowledged_event_version": event["version"], "saved": True},
        )
        .status_code
        == 200
    )
    assert len(choose("selected")) == 1
    assert (
        first[0]
        .put("/api/v1/me/subscriptions", headers=first[2], json={"source_ids": [], "event_types": []})
        .status_code
        == 200
    )
    assert len(first[0].get("/api/v1/me/feed").json()["items"]) == 1


def test_request_rate_limit_does_not_block_existing_replay(live):
    core, first, *_ = live
    for number in range(10):
        assert (
            submit(
                first, name=f"合成申请{number}", url=f"https://example.com/{number}", key=f"request-{number}"
            ).status_code
            == 201
        )
    assert submit(first, name="第11个新来源").status_code == 429
    assert submit(first, name="合成申请0", url="https://example.com/0", key="request-0").status_code == 201


def test_source_refresh_cadence_is_bounded_and_shared_across_students(live):
    core, *_ = live
    now = datetime(2030, 1, 1, 4, tzinfo=UTC)
    source = synthetic_source(core)
    source["last_checked_at"] = (now - timedelta(minutes=10)).isoformat()
    core.store.put("sources", source["id"], source)
    worker = Worker(core, clock=lambda: now, source_refresh_seconds=1800)
    worker.sweep()
    assert not [j for j in core.store.jobs() if j["kind"] == "fetch_source"]
    source["last_checked_at"] = (now - timedelta(minutes=31)).isoformat()
    core.store.put("sources", source["id"], source)
    worker.sweep()
    worker.sweep()
    assert len([j for j in core.store.jobs() if j["kind"] == "fetch_source"]) == 1
    with pytest.raises(ValueError):
        Worker(core, source_refresh_seconds=0)

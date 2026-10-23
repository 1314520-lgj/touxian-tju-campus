"""Campus integration regressions: real factory, SQLite and pipeline; no network/mail.

When reviewing another checkout, set CAMPUS_INTEGRATION_SOURCE to its
desktop/attention-desk directory and execute this file directly. After merging,
normal unittest discovery uses the current checkout.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(os.getenv("CAMPUS_INTEGRATION_SOURCE", str(Path(__file__).resolve().parents[3])))
sys.path.insert(0, str(SOURCE))

from fastapi.testclient import TestClient

from server.campus.delivery import DEFAULTS
from server.campus.platform import Settings
from server.campus.schema import now_iso, uid, validate
from server.campus.sources import FetchResult
from server.campus_app import create_app

FIXED_NOW = datetime(2030, 1, 1, 4, tzinfo=UTC)


def synthetic_source(core, source_id=None):
    """Schema-valid test source; persist() is local and performs no DNS/fetch."""
    source_id = source_id or uid()
    existing = core.store.get("sources", source_id)
    if existing:
        return existing
    source = {
        "id": source_id,
        "name": "合成集成测试源（非学校真实通知）",
        "kind": "website",
        "verification": "verified_organization",
        "origin_url": "https://example.com/integration",
        "enabled": True,
        "health": "healthy",
        "last_success_at": now_iso(),
        "last_checked_at": now_iso(),
        "adapter": "website",
        "source_identity": None,
        "verification_basis": "本地自动化测试合成来源",
        "last_error_code": None,
    }
    validate("AdminSource", source)
    core.store.put("sources", source_id, source)
    return source


def prepare_notice(core, date="2030-01-03", marker="唯旧材料", source=None, url=None, title="合成集成测试专业确认"):
    """Exercise real snapshot persistence, HTML parsing and evidence extraction."""
    source = source or synthetic_source(core)
    url = url or "https://example.com/integration/" + uid()
    y, m, d = date.split("-")
    body = (
        f"<html><head><meta charset='utf-8'></head><body><h1>{title}</h1><article>"
        f"<p>全体本科生请于{y}年{int(m)}月{int(d)}日前提交确认表。</p>"
        f"<p>材料：确认表与{marker}。</p></article></body></html>"
    ).encode()
    snapshot, _ = core.pipeline.sources.persist(
        source, FetchResult(url, body, "text/html; charset=utf-8"), title, "2030-01-01T00:00:00Z"
    )
    parsed = core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
    candidate = core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    validate("DocumentSnapshot", snapshot)
    validate("ParsedDocument", parsed)
    validate("ExtractionCandidate", candidate)
    return {"source": source, "snapshot": snapshot, "parsed": parsed, "candidate": candidate, "url": url}


class CampusIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"CAMPUS_MEILI_URL": "", "CAMPUS_DEMO": "false"})
        self.environment.start()
        self.app = create_app(
            Settings(
                data_dir=Path(self.temp.name),
                secret="integration-test-secret-" * 3,
                dev_auth=True,
                admin_emails={"admin@example.com"},
            ),
            start_worker=False,
        )
        self.core = self.app.state.core
        self.core.delivery.clock = lambda: FIXED_NOW
        self.client = TestClient(self.app)
        self.clients = [self.client]

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.core.store.close()
        self.environment.stop()
        self.temp.cleanup()

    def auth(self, email="student@example.com"):
        client = TestClient(self.app)
        self.clients.append(client)
        challenge = client.post("/api/v1/auth/challenges", json={"email": email})
        self.assertEqual(challenge.status_code, 201, challenge.text)
        verify = client.post(
            "/api/v1/auth/sessions",
            json={"challenge_id": challenge.json()["challenge_id"], "code": challenge.headers["X-Dev-Login-Code"]},
        )
        self.assertEqual(verify.status_code, 200, verify.text)
        validate("SessionResponse", verify.json())
        return client, verify.json()["user"], {"X-CSRF-Token": verify.json()["csrf_token"]}

    def approved_notice(self, fixture=None, admin=None):
        fixture = fixture or prepare_notice(self.core)
        candidate = fixture["candidate"]
        if admin is None:
            admin = self.auth("admin@example.com")
        client, user, headers = admin
        accepted = client.post(
            f"/api/v1/admin/reviews/{candidate['candidate_id']}/decision",
            json={
                "expected_candidate_revision": candidate["revision"],
                "decision": "accept",
                "reason": "本地合成资料核对",
                "replacement_candidate": None,
            },
            headers=headers,
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        validate("ExtractionCandidate", accepted.json())
        event = self.core.events.reconcile_candidate(candidate["candidate_id"], accepted.json()["revision"])
        validate("EventVersion", event)
        return {**fixture, "accepted": accepted.json(), "event": event, "admin": admin}

    def private(self, client, headers, title="合成个人事项", excerpt=None, date="2030-01-02", key=None):
        draft = {
            "title": title,
            "action_steps": ["办理本地合成事项"],
            "materials": [],
            "entry_urls": [],
            "deadline_at": None,
            "deadline_date": date,
            "timezone": "Asia/Shanghai",
            "evidence_excerpt": excerpt,
            "excerpt_sync_consent": excerpt is not None,
        }
        headers = {**headers, "Idempotency-Key": key or uid()}
        response = client.post("/api/v1/me/items", json=draft, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        validate("PersonalItem", response.json())
        return response.json(), draft, headers

    def token(self, client, headers):
        response = client.post("/api/v1/me/calendar-token", headers={**headers, "Idempotency-Key": uid()})
        self.assertEqual(response.status_code, 200, response.text)
        validate("CalendarToken", response.json())
        return response.json()["token"]

    def test_factory_has_exact_contract_routes_and_no_desktop_routes(self):
        contract = json.loads((SOURCE / "campus-contracts/openapi.v1.json").read_text(encoding="utf-8-sig"))
        expected = {
            (m.upper(), "/api/v1" + p)
            for p, methods in contract["paths"].items()
            for m in methods
            if m in {"get", "post", "put", "patch", "delete"}
        }
        actual = {
            (m.upper(), p)
            for p, methods in self.app.openapi()["paths"].items()
            for m in methods
            if m in {"get", "post", "put", "patch", "delete"}
        }
        self.assertEqual(len(expected), 59)
        self.assertEqual(actual, expected)
        self.assertEqual(self.client.get("/api/v1/health/ready").json()["demo_data"], False)
        self.assertEqual(self.core.store.list("users"), [])
        self.assertEqual(self.core.store.list("sources"), [])
        self.assertEqual(self.client.get("/api/local/archive").status_code, 404)

    def test_real_pipeline_and_public_evidence_contracts(self):
        notice = self.approved_notice()
        event = notice["event"]
        snapshot = notice["snapshot"]
        raw = (self.core.data_dir / snapshot["object_key"]).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), snapshot["content_hash"])
        self.assertEqual(notice["parsed"]["state"], "ok")
        self.assertEqual(event["deadlines"][0]["date"], "2030-01-03")
        self.assertIsNone(event["deadlines"][0]["at"])
        self.assertEqual(event["action_required"], True)
        self.assertEqual(event["audience"]["education_levels"], ["undergraduate"])
        response = self.client.get("/api/v1/events/" + event["id"])
        self.assertEqual(response.status_code, 200)
        validate("EventVersion", response.json())
        sources = self.client.get("/api/v1/sources").json()
        validate("SourcePage", sources)
        self.assertNotIn("adapter", sources["items"][0])
        proof = self.client.get("/api/v1/evidence/" + event["evidence"][0]["id"])
        self.assertEqual(proof.status_code, 200, proof.text)
        validate("Evidence", proof.json())
        parsed = self.client.get("/api/v1/snapshots/" + snapshot["snapshot_id"] + "/parsed")
        self.assertEqual(parsed.status_code, 200, parsed.text)
        validate("ParsedDocument", parsed.json())

    def test_feed_personal_state_and_stale_revision_rejection(self):
        notice = self.approved_notice()
        client, user, headers = self.auth()
        profile = client.get("/api/v1/me/profile").json()
        profile.update(education_level="undergraduate", college_id="computer")
        self.assertEqual(client.put("/api/v1/me/profile", json=profile, headers=headers).status_code, 200)
        feed = client.get("/api/v1/me/feed").json()
        validate("ActionCardPage", feed)
        self.assertEqual(feed["items"][0]["classification"], "required")
        patch_body = {
            "expected_state_revision": 0,
            "acknowledged_event_version": 1,
            "state": "done",
            "read": True,
            "saved": True,
        }
        response = client.patch(
            "/api/v1/me/events/" + notice["event"]["id"] + "/state", json=patch_body, headers=headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        validate("UserEventState", response.json())
        repeated = client.patch(
            "/api/v1/me/events/" + notice["event"]["id"] + "/state", json=patch_body, headers=headers
        )
        self.assertEqual(repeated.status_code, 409)
        validate("Error", repeated.json())

    def test_reschedule_current_history_search_and_citation_versions(self):
        original = self.approved_notice()
        replacement = prepare_notice(
            self.core, date="2030-01-05", marker="唯新材料", source=original["source"], url=original["url"]
        )
        updated = self.approved_notice(replacement, admin=original["admin"])
        self.assertEqual(updated["event"]["id"], original["event"]["id"])
        self.assertEqual(updated["event"]["version"], 2)
        self.assertEqual(updated["event"]["deadlines"][0]["date"], "2030-01-05")
        current = self.client.get("/api/v1/search", params={"q": "唯旧材料"})
        self.assertEqual(current.status_code, 200, current.text)
        validate("SearchResponse", current.json())
        self.assertFalse(any("唯旧材料" in h["excerpt"] for h in current.json()["hits"]))
        self.assertTrue(all(h["event_version"] == 2 and not h["historical"] for h in current.json()["hits"]))
        historical = self.client.get("/api/v1/search", params={"q": "唯旧材料", "include_historical": True})
        self.assertEqual(historical.status_code, 200, historical.text)
        self.assertTrue(
            any(
                "唯旧材料" in h["excerpt"] and h["historical"] and h["event_version"] == 1
                for h in historical.json()["hits"]
            )
        )
        client, _, headers = self.auth()
        answer = client.post(
            "/api/v1/answers",
            json={
                "question": "专业确认什么时候截止",
                "education_level": "undergraduate",
                "college_id": None,
                "include_historical": False,
            },
            headers=headers,
        )
        self.assertEqual(answer.status_code, 200, answer.text)
        validate("Answer", answer.json())
        self.assertEqual(answer.json()["status"], "grounded")
        self.assertIn("2030-01-05", answer.json()["text"])
        self.assertTrue(all(c["event_version"] == 2 for c in answer.json()["citations"]))

    def test_search_hits_carry_ended_consistent_with_feed_folding(self):
        admin = self.auth("admin@example.com")
        ended = self.approved_notice(
            prepare_notice(self.core, date="2026-09-01", marker="过期材料", title="合成集成测试过往事项"),
            admin=admin,
        )
        open_ = self.approved_notice(
            prepare_notice(self.core, date="2030-06-01", marker="未截止材料", title="合成集成测试进行中事项"),
            admin=admin,
        )
        response = self.client.get("/api/v1/search", params={"q": "确认表"})
        self.assertEqual(response.status_code, 200, response.text)
        validate("SearchResponse", response.json())
        by_id = {hit["event_id"]: hit for hit in response.json()["hits"]}
        self.assertEqual(by_id[ended["event"]["id"]]["ended"], True)
        self.assertEqual(by_id[open_["event"]["id"]]["ended"], False)

    def test_duplicate_candidate_replay_and_semantic_repost_keep_done(self):
        notice = self.approved_notice()
        client, user, headers = self.auth()
        response = client.patch(
            "/api/v1/me/events/" + notice["event"]["id"] + "/state",
            json={"expected_state_revision": 0, "acknowledged_event_version": 1, "state": "done", "saved": True},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        replay = self.core.events.reconcile_candidate(
            notice["accepted"]["candidate_id"], notice["accepted"]["revision"]
        )
        self.assertEqual(replay["version"], 1)
        # Another source repeats identical requirements with different evidence IDs.
        repost = self.approved_notice(prepare_notice(self.core), admin=notice["admin"])
        self.assertEqual(repost["event"]["id"], notice["event"]["id"])
        self.assertNotIn("deadlines", repost["event"]["changed_fields"])
        self.assertNotIn("audience", repost["event"]["changed_fields"])
        state = client.get("/api/v1/me/events/" + notice["event"]["id"] + "/state").json()["state"]
        self.assertEqual(state["state"], "done")
        self.assertFalse(state["needs_attention"])

    def test_student_cannot_publish_and_withdrawal_revokes_all_public_reads(self):
        notice = self.approved_notice()
        client, _, headers = self.auth()
        body = {"expected_event_version": 1, "publication_state": "withdrawn", "reason": "合成测试撤回"}
        path = "/api/v1/admin/events/" + notice["event"]["id"] + "/publication"
        self.assertEqual(client.patch(path, json=body, headers=headers).status_code, 403)
        admin, _, adminheaders = notice["admin"]
        self.assertEqual(admin.patch(path, json=body, headers=adminheaders).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/events/" + notice["event"]["id"]).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/events/" + notice["event"]["id"] + "/versions").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/evidence/" + notice["event"]["evidence"][0]["id"]).status_code, 404)
        self.assertEqual(
            self.client.get("/api/v1/snapshots/" + notice["snapshot"]["snapshot_id"] + "/parsed").status_code, 404
        )
        result = self.client.get("/api/v1/search", params={"q": "专业确认", "include_historical": True}).json()
        self.assertEqual(result["hits"], [])

    def test_withdrawal_during_search_assembly_never_returns_old_excerpt(self):
        notice = self.approved_notice()
        original = self.core.pipeline.register_evidence
        triggered = False

        def revoke_in_assembly(snapshot_id, segment_id, quote):
            nonlocal triggered
            proof = original(snapshot_id, segment_id, quote)
            if not triggered:
                triggered = True
                admin, _, headers = notice["admin"]
                response = admin.patch(
                    "/api/v1/admin/events/" + notice["event"]["id"] + "/publication",
                    json={"expected_event_version": 1, "publication_state": "withdrawn", "reason": "搜索组装竞态测试"},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 200)
            return proof

        with patch.object(self.core.pipeline, "register_evidence", side_effect=revoke_in_assembly):
            response = self.client.get("/api/v1/search", params={"q": "专业确认"})
        self.assertTrue(triggered)
        if response.status_code == 200:
            validate("SearchResponse", response.json())
            self.assertEqual(response.json()["hits"], [])
        else:
            self.assertEqual(response.status_code, 409)
            validate("Error", response.json())

    def test_two_user_private_isolation_and_schema_owner_injection(self):
        a, auser, aheaders = self.auth("a@example.com")
        b, buser, bheaders = self.auth("b@example.com")
        item, draft, headers = self.private(a, aheaders, excerpt="合成仅本人的私密摘录")
        self.assertEqual(b.get("/api/v1/me/items").json()["items"], [])
        path = "/api/v1/me/items/" + item["id"]
        self.assertEqual(
            b.patch(path, json={"expected_revision": 1, "state": "done"}, headers=bheaders).status_code, 404
        )
        self.assertEqual(b.delete(path, headers=bheaders).status_code, 404)
        malicious = a.post(
            "/api/v1/me/items",
            json={**draft, "owner_user_id": buser["id"]},
            headers={**aheaders, "Idempotency-Key": uid()},
        )
        self.assertEqual(malicious.status_code, 422)
        self.assertEqual(a.get("/api/v1/me/items").json()["items"][0]["owner_user_id"], auser["id"])
        public = self.client.get("/api/v1/search", params={"q": "合成仅本人的私密摘录"}).json()
        self.assertEqual(public["hits"], [])

    def test_delete_purges_digest_and_cached_create_returns_gone(self):
        client, user, headers = self.auth()
        excerpt = "合成删除后必须清除的私密摘录"
        item, draft, createheaders = self.private(client, headers, excerpt=excerpt, key="create-private-once")
        self.core.delivery.build_digest({"user_id": user["id"]})
        digest_response = client.get("/api/v1/me/digests")
        validate("DigestPage", digest_response.json())
        self.assertEqual(len(digest_response.json()["items"][0]["personal_items"]), 1)
        self.assertEqual(client.delete("/api/v1/me/items/" + item["id"], headers=headers).status_code, 204)
        replay = client.post("/api/v1/me/items", json=draft, headers=createheaders)
        self.assertEqual(replay.status_code, 410, replay.text)
        validate("Error", replay.json())
        self.assertEqual(client.get("/api/v1/me/items").json()["items"], [])
        self.assertEqual(client.get("/api/v1/me/digests").json()["items"][0]["personal_items"], [])
        for collection in ("personal_items", "digests", "idempotency", "calendar_entries"):
            self.assertNotIn(excerpt, json.dumps(self.core.store.list(collection), ensure_ascii=False), collection)

    def test_ics_date_only_stable_uid_cancellation_and_token_revocation(self):
        client, user, headers = self.auth()
        item, draft, _ = self.private(client, headers)
        token = self.token(client, headers)
        uri = "/api/v1/calendar/" + token + ".ics"
        initial = self.client.get(uri)
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertIn("DTSTART;VALUE=DATE:20300102", initial.text)
        self.assertNotIn("2359", initial.text)
        uidline = next(line for line in initial.text.splitlines() if line.startswith("UID:"))
        self.assertNotIn(user["id"], uidline)
        sequence = next(line for line in initial.text.splitlines() if line.startswith("SEQUENCE:"))
        replay = self.client.get(uri)
        self.assertIn(uidline, replay.text)
        self.assertIn(sequence, replay.text)
        revised = {**draft, "deadline_date": "2030-01-04"}
        changed = client.patch(
            "/api/v1/me/items/" + item["id"], json={"expected_revision": 1, "draft": revised}, headers=headers
        )
        self.assertEqual(changed.status_code, 200)
        updated = self.client.get(uri)
        self.assertIn(uidline, updated.text)
        self.assertIn("DTSTART;VALUE=DATE:20300104", updated.text)
        self.assertNotIn(sequence + "\r\n", updated.text)
        cleared = {**revised, "deadline_date": None}
        self.assertEqual(
            client.patch(
                "/api/v1/me/items/" + item["id"], json={"expected_revision": 2, "draft": cleared}, headers=headers
            ).status_code,
            200,
        )
        cancelled = self.client.get(uri)
        self.assertIn(uidline, cancelled.text)
        self.assertIn("STATUS:CANCELLED", cancelled.text)
        self.assertIn("DTSTART;VALUE=DATE:20300104", cancelled.text)
        self.assertEqual(client.delete("/api/v1/me/calendar-token", headers=headers).status_code, 204)
        self.assertEqual(self.client.get(uri).status_code, 404)

    def test_sending_replan_and_duplicate_dispatch_send_once(self):
        client, user, headers = self.auth()
        preferences = {**copy.deepcopy(DEFAULTS), "channels": ["email"], "quiet_start": None, "quiet_end": None}
        self.assertEqual(client.put("/api/v1/me/preferences", json=preferences, headers=headers).status_code, 200)
        item, _, _ = self.private(client, headers)
        self.core.delivery.plan_reminders(
            {"user_id": user["id"], "subject": {"kind": "personal_item", "id": item["id"], "revision": 1}}
        )
        row = next(d for d in self.core.store.list("deliveries") if d["kind"] == "deadline")
        entered, release = threading.Event(), threading.Event()
        errors = []

        class PausedMail:
            def __init__(self):
                self.calls = 0

            def send_delivery(self, *args):
                self.calls += 1
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("Mock SMTP release timed out")
                return {"status": "sent", "provider_id": "local-mock"}

        mail = PausedMail()
        self.core.mail = mail
        payload = {"user_id": user["id"], "delivery_id": row["id"]}

        def run():
            try:
                self.core.delivery.deliver_message(payload)
            except Exception as exc:
                errors.append(repr(exc))

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "sending")
            self.core.delivery.plan_reminders({"user_id": user["id"], "subject": None})
            self.core.delivery.deliver_message(payload)
            self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "sending")
            self.assertEqual(mail.calls, 1)
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "sent")
        self.core.delivery.deliver_message(payload)
        self.assertEqual(mail.calls, 1)

    def test_completed_private_item_cancels_scheduled_reminders(self):
        client, user, headers = self.auth()
        item, _, _ = self.private(client, headers)
        payload = {"user_id": user["id"], "subject": {"kind": "personal_item", "id": item["id"], "revision": 1}}
        self.core.delivery.plan_reminders(payload)
        rows = self.core.store.list("deliveries")
        self.assertTrue(rows)
        self.assertEqual(
            client.patch(
                "/api/v1/me/items/" + item["id"], json={"expected_revision": 1, "state": "done"}, headers=headers
            ).status_code,
            200,
        )
        self.core.delivery.plan_reminders({"user_id": user["id"], "subject": None})
        for row in rows:
            self.core.delivery.deliver_message({"user_id": user["id"], "delivery_id": row["id"]})
            self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "cancelled")

    def test_all_59_operations_return_contract_success_shapes(self):
        """One HTTP lifecycle checks every declared success response against the contract.

        DNS validation is isolated for the synthetic source creation. Parsing,
        evidence, ownership, jobs and route handlers use their real services.
        """
        contract = json.loads((SOURCE / "campus-contracts/openapi.v1.json").read_text(encoding="utf-8-sig"))
        visited = set()

        def call(client, method, template, path=None, status=200, **kwargs):
            response = client.request(method, "/api/v1" + (path or template), **kwargs)
            self.assertEqual(response.status_code, status, f"{method} {template}: {response.text}")
            operation = contract["paths"][template][method.lower()]
            self.assertIn(str(status), operation["responses"])
            content = operation["responses"][str(status)].get("content", {})
            if "application/json" in content:
                reference = content["application/json"]["schema"]["$ref"]
                validate(reference.rsplit("/", 1)[-1], response.json())
            elif "text/calendar" in content:
                self.assertIn("text/calendar", response.headers["content-type"])
                self.assertIn("BEGIN:VCALENDAR", response.text)
            elif "application/octet-stream" in content:
                self.assertIn("application/octet-stream", response.headers["content-type"])
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertTrue(response.content)
            elif status == 204:
                self.assertEqual(response.content, b"")
            visited.add((method.lower(), template))
            return response

        def login(email):
            client = TestClient(self.app)
            self.clients.append(client)
            challenge = call(client, "POST", "/auth/challenges", status=201, json={"email": email})
            session = call(
                client,
                "POST",
                "/auth/sessions",
                json={"challenge_id": challenge.json()["challenge_id"], "code": challenge.headers["X-Dev-Login-Code"]},
            ).json()
            return client, session["user"], {"X-CSRF-Token": session["csrf_token"]}

        admin, adminuser, ah = login("admin@example.com")
        student, user, sh = login("student@example.com")
        call(student, "GET", "/me")
        call(student, "GET", "/me/push-status")
        call(self.client, "GET", "/catalog")
        profile = call(student, "GET", "/me/profile").json()
        profile.update(education_level="undergraduate", college_id="computer")
        call(student, "PUT", "/me/profile", json=profile, headers=sh)

        with patch("server.campus.sources.resolve_public", return_value=None):
            source = call(
                admin,
                "POST",
                "/admin/sources",
                status=201,
                headers={**ah, "Idempotency-Key": "contract-source"},
                json={
                    "name": "合成契约源（非学校真实通知）",
                    "kind": "website",
                    "origin_url": "https://example.com/contract",
                    "adapter": "website",
                    "source_identity": None,
                },
            ).json()
        sid = source["id"]
        call(admin, "GET", "/admin/sources")
        source = call(
            admin,
            "PATCH",
            "/admin/sources/{source_id}",
            path=f"/admin/sources/{sid}",
            json={"name": "合成契约测试来源"},
            headers=ah,
        ).json()
        source = call(
            admin,
            "POST",
            "/admin/sources/{source_id}/verification",
            path=f"/admin/sources/{sid}/verification",
            json={"verification": "verified_organization", "basis": "本地合成测试来源"},
            headers=ah,
        ).json()
        call(self.client, "GET", "/sources")
        call(student, "GET", "/me/subscriptions")
        call(student, "PUT", "/me/subscriptions", json={"source_ids": [sid], "event_types": []}, headers=sh)
        job = call(
            admin,
            "POST",
            "/admin/sources/{source_id}/fetch",
            path=f"/admin/sources/{sid}/fetch",
            status=202,
            headers={**ah, "Idempotency-Key": "contract-fetch"},
        ).json()
        # Fail locally without any network; then exercise the real operator retry.
        claimed = self.core.store.claim_job("contract-worker", kinds=["fetch_source"])
        self.assertEqual(claimed["job"]["id"], job["id"])
        self.core.store.fail_job(job["id"], "synthetic_fetch_failure", retry_delay=None)
        call(admin, "GET", "/admin/jobs")
        call(admin, "GET", "/admin/jobs/{job_id}", path=f"/admin/jobs/{job['id']}")
        call(
            admin,
            "POST",
            "/admin/jobs/{job_id}/retry",
            status=202,
            path=f"/admin/jobs/{job['id']}/retry",
            headers={**ah, "Idempotency-Key": "contract-retry"},
        )
        service_token = "synthetic-service-token-" * 3
        with patch.dict(os.environ, {"CAMPUS_CHANGE_TOKEN": service_token}):
            call(
                self.client,
                "POST",
                "/internal/changes",
                status=202,
                json={
                    "source_id": sid,
                    "source_url": "https://example.com/contract/new",
                    "observed_at": now_iso(),
                    "provider_change_id": "contract-change",
                },
                headers={"Authorization": "Bearer " + service_token, "Idempotency-Key": "contract-hint"},
            )

        request = call(
            student,
            "POST",
            "/me/source-requests",
            status=201,
            headers={**sh, "Idempotency-Key": "contract-request"},
            json={
                "name": "合成契约源",
                "kind": "website",
                "url": "https://example.com/contract",
                "auto_subscribe": True,
            },
        ).json()
        call(student, "GET", "/me/source-requests")
        call(admin, "GET", "/admin/source-requests")
        source.update(health="healthy", last_success_at=now_iso())
        self.core.store.put("sources", sid, source)
        call(
            admin,
            "POST",
            "/admin/source-requests/{request_id}/resolve",
            path=f"/admin/source-requests/{request['id']}/resolve",
            headers={**ah, "Idempotency-Key": "contract-resolve"},
            json={"decision": "connect", "source_id": sid, "note": "合成来源成功采集并核对身份，测试不连接公网"},
        )

        notice = prepare_notice(self.core, source=source)
        candidate = notice["candidate"]
        call(admin, "GET", "/admin/reviews")
        accepted = call(
            admin,
            "POST",
            "/admin/reviews/{candidate_id}/decision",
            path=f"/admin/reviews/{candidate['candidate_id']}/decision",
            headers=ah,
            json={
                "expected_candidate_revision": candidate["revision"],
                "decision": "accept",
                "reason": "本地契约审核",
                "replacement_candidate": None,
            },
        ).json()
        event = self.core.events.reconcile_candidate(accepted["candidate_id"], accepted["revision"])
        eid = event["id"]
        call(self.client, "GET", "/events")
        call(self.client, "GET", "/events/{event_id}", path=f"/events/{eid}")
        call(self.client, "GET", "/events/{event_id}/versions", path=f"/events/{eid}/versions")
        call(self.client, "GET", "/events/{event_id}/changes", path=f"/events/{eid}/changes")
        call(self.client, "GET", "/events/{event_id}/quality", path=f"/events/{eid}/quality")
        call(student, "GET", "/me/feed")
        call(student, "GET", "/me/events/{event_id}/state", path=f"/me/events/{eid}/state")
        call(
            student,
            "PATCH",
            "/me/events/{event_id}/state",
            path=f"/me/events/{eid}/state",
            headers=sh,
            json={"expected_state_revision": 0, "acknowledged_event_version": 1, "saved": True, "read": True},
        )
        call(self.client, "GET", "/search", params={"q": "专业确认"})
        call(
            student,
            "POST",
            "/answers",
            headers=sh,
            json={
                "question": "专业确认什么时候截止",
                "education_level": "undergraduate",
                "college_id": "computer",
                "include_historical": False,
            },
        )
        snapshot_id = notice["snapshot"]["snapshot_id"]
        public_read = call(self.client, "GET", "/snapshots/{snapshot_id}", path=f"/snapshots/{snapshot_id}").json()
        call(self.client, "GET", "/snapshots/{snapshot_id}/file", path=public_read["signed_url"].split("/api/v1", 1)[1])
        call(self.client, "GET", "/snapshots/{snapshot_id}/parsed", path=f"/snapshots/{snapshot_id}/parsed")
        proof = event["evidence"][0]
        call(self.client, "GET", "/evidence/{evidence_id}", path=f"/evidence/{proof['id']}")
        admin_read = call(admin, "GET", "/admin/snapshots/{snapshot_id}", path=f"/admin/snapshots/{snapshot_id}").json()
        call(admin, "GET", "/admin/snapshots/{snapshot_id}/file", path=admin_read["signed_url"].split("/api/v1", 1)[1])
        call(admin, "GET", "/admin/snapshots/{snapshot_id}/parsed", path=f"/admin/snapshots/{snapshot_id}/parsed")
        call(admin, "GET", "/admin/events")
        child, _ = self.core.pipeline.sources.persist(
            source,
            FetchResult(
                "https://example.com/system/_content/download.jsp?wbfileid=contract",
                b'<html><input name="codeValue"><img src="createimage.jsp"></html>',
                "text/html",
            ),
            "合成受限附件",
            now_iso(),
            snapshot_id,
        )
        replacement = call(
            admin,
            "POST",
            "/admin/snapshots/{snapshot_id}/replacement",
            status=201,
            path=f"/admin/snapshots/{child['snapshot_id']}/replacement",
            headers={**ah, "Idempotency-Key": "contract-attachment-replacement"},
            data={"basis": "仅合成文件授权验收，不代表真实授权"},
            files={"file": ("contract.txt", "材料：合成授权补充确认表。".encode(), "text/plain")},
        ).json()
        self.assertEqual(self.core.store.get("candidates", replacement["candidate_id"])["review_status"], "pending")

        draft = {
            "title": "合成契约个人待办",
            "action_steps": ["核对确认"],
            "materials": [],
            "entry_urls": [],
            "deadline_at": None,
            "deadline_date": "2030-01-02",
            "timezone": "Asia/Shanghai",
            "evidence_excerpt": None,
            "excerpt_sync_consent": False,
        }
        item = call(
            student, "POST", "/me/items", status=201, json=draft, headers={**sh, "Idempotency-Key": "contract-personal"}
        ).json()
        call(student, "GET", "/me/items")
        call(
            student,
            "PATCH",
            "/me/items/{item_id}",
            path=f"/me/items/{item['id']}",
            json={"expected_revision": 1, "state": "done"},
            headers=sh,
        )
        prefs = call(student, "GET", "/me/preferences").json()
        call(student, "PUT", "/me/preferences", json=prefs, headers=sh)
        self.core.delivery.plan_reminders({"user_id": user["id"], "subject": None})
        self.core.delivery.build_digest({"user_id": user["id"]})
        call(student, "GET", "/me/digests")
        call(student, "GET", "/me/deliveries")
        token = call(
            student, "POST", "/me/calendar-token", headers={**sh, "Idempotency-Key": "contract-calendar"}
        ).json()["token"]
        call(self.client, "GET", "/calendar/{token}.ics", path=f"/calendar/{token}.ics")
        call(student, "DELETE", "/me/calendar-token", status=204, headers=sh)
        call(student, "DELETE", "/me/items/{item_id}", path=f"/me/items/{item['id']}", status=204, headers=sh)
        call(
            admin,
            "PATCH",
            "/admin/events/{event_id}/publication",
            path=f"/admin/events/{eid}/publication",
            headers=ah,
            json={
                "expected_event_version": event["version"],
                "publication_state": "withdrawn",
                "reason": "合成契约通知撤回",
            },
        )
        call(student, "DELETE", "/auth/session", status=204, headers=sh)
        call(self.client, "GET", "/health/live")
        call(self.client, "GET", "/health/ready")
        expected = {
            (method, path)
            for path, methods in contract["paths"].items()
            for method in methods
            if method in {"get", "post", "put", "patch", "delete"}
        }
        self.assertEqual(len(expected), 59)
        self.assertEqual(visited, expected)

    def test_admin_snapshot_capability_and_withdrawn_inventory_require_admin(self):
        notice = self.approved_notice()
        admin, adminuser, ah = notice["admin"]
        student, _, _ = self.auth()
        snapshot_id = notice["snapshot"]["snapshot_id"]
        details = admin.get(f"/api/v1/admin/snapshots/{snapshot_id}")
        self.assertEqual(details.status_code, 200, details.text)
        validate("SnapshotRead", details.json())
        file_url = details.json()["signed_url"]
        for path in (
            "/api/v1/admin/events",
            f"/api/v1/admin/snapshots/{snapshot_id}",
            f"/api/v1/admin/snapshots/{snapshot_id}/parsed",
            file_url,
        ):
            self.assertEqual(self.client.get(path).status_code, 401, path)
            self.assertEqual(student.get(path).status_code, 403, path)
        # A second admin cannot redeem the original admin's signed file capability.
        other, _, _ = self.auth("other@example.com")
        otheruser = next(u for u in self.core.store.list("users") if u["email"] == "other@example.com")
        otheruser["role"] = "admin"
        self.core.store.put("users", otheruser["id"], otheruser)
        self.assertEqual(other.get(file_url).status_code, 403)
        response = admin.patch(
            f"/api/v1/admin/events/{notice['event']['id']}/publication",
            headers=ah,
            json={"expected_event_version": 1, "publication_state": "withdrawn", "reason": "审核库存测试撤回"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        inventory = admin.get("/api/v1/admin/events")
        validate("EventVersionPage", inventory.json())
        self.assertEqual(inventory.json()["items"][0]["publication_state"], "withdrawn")
        self.assertEqual(self.client.get("/api/v1/events").json()["items"], [])
        self.assertEqual(admin.get(file_url).status_code, 200)
        self.assertEqual(admin.delete("/api/v1/auth/session", headers=ah).status_code, 204)
        self.assertEqual(admin.get(file_url).status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)

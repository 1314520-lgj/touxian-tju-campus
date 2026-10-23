"""Real SQLite/HTTP contract tests. SMTP is mocked; no external mail is sent."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from server.campus.db import Store
from server.campus.platform import (
    APIError,
    Core,
    MailGateway,
    Settings,
    error_handlers,
    make_router,
)
from server.campus.schema import uid, validate


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            data_dir=Path(self.temp.name),
            secret="platform-tests-secret-" * 3,
            dev_auth=True,
            admin_emails={"admin@example.com"},
        )
        self.core = Core(self.settings)
        self.app = FastAPI()
        self.app.state.core = self.core
        error_handlers(self.app)
        self.app.include_router(make_router(self.core), prefix="/api/v1")
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.core.store.close()
        self.temp.cleanup()

    def login(self, email="student@example.com", client=None):
        client = client or self.client
        response = client.post("/api/v1/auth/challenges", json={"email": email})
        self.assertEqual(response.status_code, 201, response.text)
        validate("LoginChallenge", response.json())
        code = response.headers["X-Dev-Login-Code"]
        challenge_id = response.json()["challenge_id"]
        verify = client.post("/api/v1/auth/sessions", json={"challenge_id": challenge_id, "code": code})
        self.assertEqual(verify.status_code, 200, verify.text)
        validate("SessionResponse", verify.json())
        return verify.json(), {"challenge_id": challenge_id, "code": code}

    def test_random_otp_session_cookie_and_replay(self):
        session, login_request = self.login()
        challenge = self.core.store.get("challenges", login_request["challenge_id"])
        self.assertNotIn(login_request["code"], json.dumps(challenge))
        self.assertEqual(challenge["attempts"], 1)
        self.assertTrue(challenge["used"])
        self.assertEqual(self.client.get("/api/v1/me").json(), session)
        cookie = self.client.cookies.get("touxian_session")
        self.assertGreater(len(cookie), 32)
        self.assertIsNone(self.core.store.get("sessions", cookie))
        self.assertIsNotNone(self.core.store.get("sessions", self.core.digest(cookie)))
        replay = self.client.post("/api/v1/auth/sessions", json=login_request)
        self.assertEqual(replay.status_code, 401)
        validate("Error", replay.json())
        UUID(replay.headers["X-Request-ID"])

    def test_concurrent_code_verify_only_one_session(self):
        response = self.client.post("/api/v1/auth/challenges", json={"email": "parallel@example.com"})
        payload = {"challenge_id": response.json()["challenge_id"], "code": response.headers["X-Dev-Login-Code"]}
        barrier = threading.Barrier(2)

        def verify(_):
            with TestClient(self.app) as client:
                barrier.wait(timeout=10)
                return client.post("/api/v1/auth/sessions", json=payload).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(verify, [1, 2]))
        self.assertCountEqual(results, [200, 401])
        self.assertEqual(len(self.core.store.list("sessions")), 1)

    def test_csrf_and_origin(self):
        session, _ = self.login()
        profile = self.client.get("/api/v1/me/profile").json()
        for headers in (
            {},
            {"X-CSRF-Token": "incorrect"},
            {"X-CSRF-Token": session["csrf_token"], "Origin": "https://evil.example"},
        ):
            response = self.client.put("/api/v1/me/profile", json=profile, headers=headers)
            self.assertEqual(response.status_code, 403)
        response = self.client.put(
            "/api/v1/me/profile",
            json=profile,
            headers={"X-CSRF-Token": session["csrf_token"], "Origin": "http://localhost:5173"},
        )
        self.assertEqual(response.status_code, 200)

    def test_two_users_profile_isolation_etags_and_extra_owner_rejection(self):
        a, _ = self.login()
        bclient = TestClient(self.app)
        try:
            b, _ = self.login("another@example.com", bclient)
            self.assertNotEqual(a["user"]["id"], b["user"]["id"])
            response = self.client.get("/api/v1/me/profile")
            etag = response.headers["ETag"]
            profile = response.json()
            profile.update(college_id="computer", education_level="undergraduate", major="计算机科学")
            headers = {"X-CSRF-Token": a["csrf_token"], "If-Match": etag}
            self.assertEqual(self.client.put("/api/v1/me/profile", json=profile, headers=headers).status_code, 200)
            self.assertEqual(self.client.put("/api/v1/me/profile", json=profile, headers=headers).status_code, 409)
            self.assertIsNone(bclient.get("/api/v1/me/profile").json()["college_id"])
            injected = {**profile, "user_id": b["user"]["id"]}
            self.assertEqual(
                self.client.put(
                    "/api/v1/me/profile", json=injected, headers={"X-CSRF-Token": a["csrf_token"]}
                ).status_code,
                422,
            )
            events = self.core.store.pending_outbox()
            self.assertEqual(events[-1]["topic"], "profile.changed")
            self.assertEqual(events[-1]["user_id"], a["user"]["id"])
        finally:
            bclient.close()

    def test_admin_role_and_revocation(self):
        student, _ = self.login()
        self.assertEqual(self.client.get("/api/v1/admin/jobs").status_code, 403)
        adminclient = TestClient(self.app)
        try:
            admin, _ = self.login("admin@example.com", adminclient)
            self.assertEqual(admin["user"]["role"], "admin")
            self.assertEqual(adminclient.get("/api/v1/admin/jobs").status_code, 200)
            self.settings.admin_emails.clear()
            self.assertEqual(adminclient.get("/api/v1/admin/jobs").status_code, 403)
        finally:
            adminclient.close()

    def test_code_failures_are_persistent_and_bounded(self):
        challenge = self.client.post("/api/v1/auth/challenges", json={"email": "wrong@example.com"})
        payload = {"challenge_id": challenge.json()["challenge_id"], "code": challenge.headers["X-Dev-Login-Code"]}
        badcode = "000000" if payload["code"] != "000000" else "111111"
        for _ in range(5):
            self.assertEqual(
                self.client.post("/api/v1/auth/sessions", json={**payload, "code": badcode}).status_code, 401
            )
        self.assertEqual(self.core.store.get("challenges", payload["challenge_id"])["attempts"], 5)
        self.assertEqual(self.client.post("/api/v1/auth/sessions", json=payload).status_code, 401)

    def test_expired_code_session_and_logout(self):
        session, _ = self.login()
        cookie = self.client.cookies.get("touxian_session")
        key = self.core.digest(cookie)
        record = self.core.store.get("sessions", key)
        record["expires_at"] = "2000-01-01T00:00:00Z"
        self.core.store.put("sessions", key, record)
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)
        another = TestClient(self.app)
        try:
            data, _ = self.login("logout@example.com", another)
            self.assertEqual(
                another.delete("/api/v1/auth/session", headers={"X-CSRF-Token": data["csrf_token"]}).status_code, 204
            )
            self.assertEqual(another.get("/api/v1/me").status_code, 401)
        finally:
            another.close()
        response = self.client.post("/api/v1/auth/challenges", json={"email": "expired@example.com"})
        key = response.json()["challenge_id"]
        record = self.core.store.get("challenges", key)
        record["expires_at"] = "2000-01-01T00:00:00Z"
        self.core.store.put("challenges", key, record)
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/sessions", json={"challenge_id": key, "code": response.headers["X-Dev-Login-Code"]}
            ).status_code,
            401,
        )

    def test_rate_limit_no_otp_leak_and_unconfigured_delivery(self):
        response = self.client.post("/api/v1/auth/challenges", json={"email": "rate@example.com"})
        self.assertEqual(response.status_code, 201)
        limited = self.client.post("/api/v1/auth/challenges", json={"email": "rate@example.com"})
        self.assertEqual(limited.status_code, 429)
        self.assertNotIn("X-Dev-Login-Code", limited.headers)
        self.settings.dev_auth = False
        unavailable = self.client.post("/api/v1/auth/challenges", json={"email": "no-smtp@example.com"})
        self.assertEqual(unavailable.status_code, 503)
        self.assertNotIn("X-Dev-Login-Code", unavailable.headers)
        self.assertEqual(unavailable.json()["details"]["delivery_status"], "failed")

    def test_catalog_subscription_validation_and_no_demo_seed(self):
        catalog = self.client.get("/api/v1/catalog").json()
        validate("InstitutionCatalog", catalog)
        self.assertEqual(catalog["school_name"], "天津大学")
        self.assertEqual(len(catalog["colleges"]), 36)
        self.assertEqual(self.core.store.list("sources"), [])
        self.assertEqual(self.core.store.list("users"), [])
        self.assertFalse(self.client.get("/api/v1/health/ready").json()["demo_data"])
        session, _ = self.login()
        headers = {"X-CSRF-Token": session["csrf_token"]}
        invalid = self.client.put(
            "/api/v1/me/subscriptions", json={"source_ids": [uid()], "event_types": []}, headers=headers
        )
        self.assertEqual(invalid.status_code, 422)
        source_id = uid()
        self.core.store.put("sources", source_id, {"id": source_id})
        valid = self.client.put(
            "/api/v1/me/subscriptions",
            json={"source_ids": [source_id, source_id], "event_types": ["scholarship"]},
            headers=headers,
        )
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.json()["source_ids"], [source_id])

    def test_payload_limits_bad_json_and_formats(self):
        self.assertEqual(self.client.post("/api/v1/auth/challenges", content="bad").status_code, 400)
        self.assertEqual(self.client.post("/api/v1/auth/challenges", json={"email": "bad"}).status_code, 422)
        oversized = self.client.post("/api/v1/auth/challenges", content=" " * 65537)
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(oversized.json()["details"], {})

    def test_job_admin_retry_idempotency_and_pagination(self):
        session, _ = self.login("admin@example.com")
        job = self.core.store.enqueue("build_digest", {"user_id": session["user"]["id"]}, "admin-test")
        self.core.store.claim_job("worker")
        self.core.store.fail_job(job["id"], "TEST", retry_delay=None)
        headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "retry-once"}
        first = self.client.post(f"/api/v1/admin/jobs/{job['id']}/retry", headers=headers)
        second = self.client.post(f"/api/v1/admin/jobs/{job['id']}/retry", headers=headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(self.client.get("/api/v1/admin/jobs?limit=101").status_code, 422)
        page = self.client.get("/api/v1/admin/jobs?limit=1").json()
        validate("JobPage", page)
        self.assertEqual(len(page["items"]), 1)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.url = "sqlite:///" + (Path(self.temp.name) / "data.sqlite3").as_posix()
        self.store = Store(self.url)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_durable_json_copy_and_internal_revision(self):
        source = {"name": "test", "nested": {"value": 1}}
        self.store.put("sources", "one", source, expected_revision=0)
        source["nested"]["value"] = 2
        self.assertEqual(self.store.get("sources", "one")["nested"]["value"], 1)
        self.assertNotIn("revision", self.store.get("sources", "one"))
        self.assertEqual(self.store.revision("sources", "one"), 1)
        other = Store(self.url)
        try:
            self.assertEqual(other.get("sources", "one")["name"], "test")
            value = other.get("sources", "one")
            value["nested"]["value"] = 9
            self.assertEqual(other.get("sources", "one")["nested"]["value"], 1)
        finally:
            other.close()

    def test_business_outbox_queue_atomic_rollback(self):
        user_id = uid()
        with self.assertRaisesRegex(ValueError, "abort"):
            with self.store.transaction():
                self.store.put("profiles", user_id, {"value": 1})
                self.store.emit("profile.changed", user_id, 1, user_id=user_id)
                self.store.enqueue("build_digest", {"user_id": user_id}, "atomic")
                raise ValueError("abort")
        self.assertIsNone(self.store.get("profiles", user_id))
        self.assertEqual(self.store.pending_outbox(), [])
        self.assertEqual(self.store.jobs(), [])
        with self.store.transaction():
            self.store.put("profiles", user_id, {"value": 1})
            self.store.emit("profile.changed", user_id, 1, user_id=user_id)
            self.store.enqueue("build_digest", {"user_id": user_id}, "atomic")
        self.assertEqual(len(self.store.pending_outbox()), 1)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_nested_failure_cannot_silently_commit(self):
        with self.assertRaises(APIError):
            with self.store.transaction():
                self.store.put("profiles", "one", {"value": 1})
                try:
                    self.store.put("profiles", "one", {"value": 2}, expected_revision=0)
                except APIError:
                    pass
        self.assertIsNone(self.store.get("profiles", "one"))

    def test_competing_revision_updates_one_winner(self):
        self.store.put("profiles", "one", {"value": 0}, expected_revision=0)
        barrier = threading.Barrier(2)

        def update_record(value):
            store = Store(self.url)
            try:
                revision = store.revision("profiles", "one")
                barrier.wait(timeout=10)
                try:
                    store.put("profiles", "one", {"value": value}, expected_revision=revision)
                    return "ok"
                except APIError as exc:
                    return exc.status
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update_record, [1, 2]))
        self.assertCountEqual(results, ["ok", 409])
        self.assertEqual(self.store.revision("profiles", "one"), 2)

    def test_queue_idempotency_and_contract_validation(self):
        user_id = uid()
        first = self.store.enqueue("build_digest", {"user_id": user_id}, "once")
        second = self.store.enqueue("build_digest", {"user_id": user_id}, "once")
        self.assertEqual(first["id"], second["id"])
        with self.assertRaises(APIError) as raised:
            self.store.enqueue("build_digest", {"user_id": uid()}, "once")
        self.assertEqual(raised.exception.status, 409)
        with self.assertRaises(APIError):
            self.store.enqueue("fetch_source", {}, "missing-id")
        self.assertEqual(len(self.store.jobs()), 1)
        claimed = self.store.claim_job("test")
        validate("JobPayload", claimed["payload"])
        self.assertEqual(claimed["payload"]["job_id"], first["id"])

    def test_two_workers_do_not_claim_same_unexpired_job(self):
        self.store.enqueue("build_digest", {"user_id": uid()}, "workers")
        barrier = threading.Barrier(2)

        def claim(worker):
            store = Store(self.url)
            try:
                barrier.wait(timeout=10)
                result = store.claim_job(worker)
                return result["job"]["id"] if result else None
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(claim, ["one", "two"]))
        self.assertEqual(sum(value is not None for value in result), 1)

    def test_expired_lease_replay_fences_old_worker(self):
        job = self.store.enqueue("build_digest", {"user_id": uid()}, "lease")
        self.store.claim_job("old")
        with self.store.engine.begin() as connection:
            connection.execute(
                update(self.store.job_table)
                .where(self.store.job_table.c.id == job["id"])
                .values(lease_until="2000-01-01T00:00:00.000000Z")
            )
        other = Store(self.url)
        try:
            claim = other.claim_job("new")
            self.assertEqual(claim["job"]["attempts"], 2)
            with self.assertRaises(APIError) as failure:
                self.store.complete_job(job["id"])
            self.assertEqual(failure.exception.code, "JOB_LEASE_LOST")
            other.complete_job(job["id"])
            self.assertEqual(self.store.get_job(job["id"])["state"], "succeeded")
        finally:
            other.close()

    def test_bounded_automatic_and_manual_retry(self):
        job = self.store.enqueue("build_digest", {"user_id": uid()}, "bounded")
        for attempt in range(6):
            claimed = self.store.claim_job("test")
            self.assertEqual(claimed["job"]["attempts"], attempt + 1)
            self.store.fail_job(job["id"], "FAIL", retry_delay=0)
        self.assertEqual(self.store.get_job(job["id"])["state"], "failed")
        self.assertIsNone(self.store.claim_job("test"))
        for _ in range(3):
            self.store.retry_job(job["id"])
            self.store.claim_job("test")
            self.store.fail_job(job["id"], "FAIL", retry_delay=None)
        with self.assertRaises(APIError) as failure:
            self.store.retry_job(job["id"])
        self.assertEqual(failure.exception.code, "JOB_REPLAY_EXHAUSTED")

    def test_dispatch_mark_and_enqueue_are_atomic(self):
        owner = uid()
        event = self.store.emit("profile.changed", owner, 1, user_id=owner)
        with self.assertRaises(ValueError):
            with self.store.transaction():
                self.store.enqueue("plan_reminders", {"user_id": owner}, "dispatch:" + event["id"])
                self.store.mark_outbox_dispatched(event["id"])
                raise ValueError("crash before commit")
        self.assertEqual(self.store.jobs(), [])
        self.assertEqual(len(self.store.pending_outbox()), 1)
        with self.store.transaction():
            self.store.enqueue("plan_reminders", {"user_id": owner}, "dispatch:" + event["id"])
            self.store.mark_outbox_dispatched(event["id"])
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(self.store.pending_outbox(), [])

    def test_time_order_kinds_and_delete_cas(self):
        self.store.enqueue("build_digest", {"user_id": uid()}, "future", run_at=datetime.now(UTC) + timedelta(hours=1))
        ready = self.store.enqueue("fetch_source", {"source_id": uid()}, "ready")
        self.assertIsNone(self.store.claim_job("test", kinds=["build_digest"]))
        self.assertEqual(self.store.claim_job("test", kinds=["fetch_source"])["job"]["id"], ready["id"])
        self.store.complete_job(ready["id"])
        self.store.put("profiles", "one", {"value": 1})
        with self.assertRaises(APIError):
            self.store.delete("profiles", "one", expected_revision=0)
        self.store.delete("profiles", "one", expected_revision=1)
        self.assertEqual(self.store.revision("profiles", "one"), 0)


class ConcurrentStartupTests(unittest.TestCase):
    """Two cores may open one fresh WAL database at once; the loser must wait.

    Regression: the schema bootstrap's `BEGIN IMMEDIATE` could be answered with
    SQLITE_READONLY rather than SQLITE_BUSY while the other connection switched
    the journal mode. `busy_timeout` does not cover the readonly form, so the
    second core failed outright instead of converging on the state the winner
    had already committed. Measured before the fix: 6/200 and 7/120 attempts
    raised `attempt to write a readonly database`; after the fix, 300/300
    converged.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.url = "sqlite:///" + (Path(self.temp.name) / "data.sqlite3").as_posix()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _contention():
        return OperationalError("BEGIN IMMEDIATE", {}, sqlite3.OperationalError("attempt to write a readonly database"))

    def test_bootstrap_retries_a_transient_readonly_race(self):
        real, attempts = Store.transaction, []

        def flaky(store):
            attempts.append(store)
            if len(attempts) == 1:
                raise self._contention()
            return real(store)

        with patch.object(Store, "transaction", flaky):
            store = Store(self.url)
        try:
            self.assertEqual(len(attempts), 2, "the bootstrap must retry the losing attempt")
            store.put("sources", "one", {"name": "kept"})
            self.assertEqual(store.get("sources", "one")["name"], "kept")
        finally:
            store.close()

    def test_bootstrap_does_not_retry_a_real_failure(self):
        attempts = []

        def broken(store):
            attempts.append(store)
            raise OperationalError("BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database disk image is malformed"))

        with patch.object(Store, "transaction", broken):
            with self.assertRaises(OperationalError):
                Store(self.url)
        self.assertEqual(len(attempts), 1, "a corrupt database must fail loudly, not be retried")

    def test_two_cores_open_a_fresh_database_together(self):
        barrier = threading.Barrier(2)

        def start(_):
            settings = Settings(data_dir=Path(self.temp.name), secret="concurrent-startup-secret-" * 3)
            barrier.wait(timeout=10)
            core = Core(settings)
            try:
                return core.settings.secret
            finally:
                core.store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            secrets = list(pool.map(start, [1, 2]))
        self.assertEqual(secrets[0], secrets[1])


class ConfigurationAndMailTests(unittest.TestCase):
    def test_production_rejects_unsafe_config(self):
        valid = dict(
            env="production",
            secret="s" * 48,
            database_url="postgresql://user:pass@db/campus",
            allowed_origins=["https://campus.example"],
        )
        with self.assertRaises(ValueError):
            Settings(**valid, dev_auth=True)
        with self.assertRaises(ValueError):
            Settings(**{**valid, "database_url": "sqlite:///:memory:"})
        with self.assertRaises(ValueError):
            Settings(**{**valid, "secret": ""})
        with self.assertRaises(ValueError):
            Settings(**{**valid, "allowed_origins": ["*"]})
        Settings(**valid)

    def test_parallel_development_cores_share_atomic_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            barrier = threading.Barrier(2)

            def start(_):
                settings = Settings(data_dir=Path(directory))
                barrier.wait(timeout=10)
                core = Core(settings)
                try:
                    return core.settings.secret
                finally:
                    core.store.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                values = list(pool.map(start, [1, 2]))
            self.assertEqual(values[0], values[1])
            self.assertGreaterEqual(len(values[0]), 32)

    def test_development_never_sends_real_email(self):
        settings = Settings(smtp_host="smtp.example", smtp_from="sender@example.com")
        with patch("server.campus.platform.smtplib.SMTP") as smtp:
            receipt = MailGateway(settings).send_delivery("person@example.com", "subject", "text", "one")
            self.assertEqual(receipt["status"], "failed")
            smtp.assert_not_called()

    def test_smtp_data_timeout_is_unknown_not_sent(self):
        settings = Settings(
            env="production",
            secret="s" * 48,
            database_url="postgresql://user:pass@db/campus",
            allowed_origins=["https://campus.example"],
            smtp_host="smtp.example",
            smtp_from="sender@example.com",
        )
        client = MagicMock()
        client.send_message.side_effect = TimeoutError("SMTP data response timed out")
        manager = MagicMock()
        manager.__enter__.return_value = client
        with patch("server.campus.platform.smtplib.SMTP", return_value=manager):
            receipt = MailGateway(settings).send_delivery("person@example.com", "subject", "text", "one")
        self.assertEqual(receipt, {"status": "unknown", "provider_id": None})
        client.starttls.assert_called_once()

    def test_smtp_acceptance_receipt_and_preconnect_failure(self):
        settings = Settings(
            env="production",
            secret="s" * 48,
            database_url="postgresql://user:pass@db/campus",
            allowed_origins=["https://campus.example"],
            smtp_host="smtp.example",
            smtp_from="sender@example.com",
        )
        client = MagicMock()
        client.send_message.return_value = {}
        manager = MagicMock()
        manager.__enter__.return_value = client
        with patch("server.campus.platform.smtplib.SMTP", return_value=manager):
            self.assertEqual(
                MailGateway(settings).send_delivery("person@example.com", "subject", "text", "one")["status"], "sent"
            )
        with patch("server.campus.platform.smtplib.SMTP", side_effect=ConnectionError("offline")):
            self.assertEqual(
                MailGateway(settings).send_delivery("person@example.com", "subject", "text", "one")["status"], "failed"
            )


if __name__ == "__main__":
    unittest.main()

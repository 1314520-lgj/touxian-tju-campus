"""Network fixtures prove bounded discovery, restart recovery and attachment state."""

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from server.campus.content import Pipeline
from server.campus.ingestion import SourceBacklog
from server.campus.platform import Core, Settings
from server.campus.schema import uid, validate
from server.campus.sources import FetchError, FetchResult, content_kind
from server.campus.tests.test_pipeline import MappingFetcher


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name), secret="x" * 48, dev_auth=True)
        self.core = Core(self.settings)
        self.core.pipeline = Pipeline(self.core)
        self.source = dict(
            id=uid(),
            name="Synthetic backlog",
            kind="rss",
            adapter="rss",
            origin_url="https://public.test/list",
            source_identity=None,
            enabled=True,
            verification="unverified",
            verification_basis=None,
            health="never_checked",
            last_error_code=None,
            last_checked_at=None,
            last_success_at=None,
        )
        self.core.store.put("sources", self.source["id"], self.source)
        self.fetcher = MappingFetcher({})
        self.core.pipeline.sources.fetcher = self.fetcher

    def tearDown(self):
        self.core.store.close()
        self.tmp.cleanup()

    def listing(self, count):
        entries = "".join(
            f"<item><title>Item {i}</title><link>https://public.test/{i}</link></item>" for i in range(count)
        )
        self.fetcher.mapping[self.source["origin_url"]] = FetchResult(
            self.source["origin_url"], ("<rss><channel>" + entries + "</channel></rss>").encode(), "application/xml"
        )
        for i in range(count):
            url = f"https://public.test/{i}"
            self.fetcher.mapping[url] = FetchResult(
                url, f"<h1>Item {i}</h1><article>Fixture {i}</article>".encode(), "text/html"
            )

    def fetch(self):
        return self.core.pipeline.sources.fetch_source({"source_id": self.source["id"], "job_id": uid()})

    def state(self):
        return self.core.store.get("source_ingestion", self.source["id"])

    def summary(self):
        return SourceBacklog(self.core.store).summary(self.core.store.get("sources", self.source["id"]))

    def test_more_than_eight_drains_across_process_restart_and_rotates_refresh(self):
        self.listing(19)
        self.fetch()
        self.assertEqual(self.summary()["pending_count"], 11)
        self.core.store.close()
        self.core = Core(self.settings)
        self.core.pipeline = Pipeline(self.core)
        self.core.pipeline.sources.fetcher = self.fetcher
        self.fetch()
        self.fetch()
        self.assertEqual(len(self.core.store.list("snapshots")), 19)
        self.assertEqual(self.summary()["pending_count"], 0)
        self.assertEqual(self.summary()["completed_count"], 19)
        calls = len(self.fetcher.calls)
        self.fetch()
        self.fetch()
        refreshed = [url for url, _ in self.fetcher.calls[calls:] if url != self.source["origin_url"]]
        self.assertEqual(len(refreshed), 16)
        self.assertEqual(len(set(refreshed)), 16)

    def test_failed_first_item_does_not_starve_later_and_retries_with_backoff(self):
        self.listing(12)
        self.fetcher.mapping["https://public.test/0"] = FetchError("upstream_http_503")
        self.fetch()
        self.fetch()
        self.assertEqual(len(self.core.store.list("snapshots")), 11)
        self.assertEqual(self.summary()["failed_count"], 1)
        self.assertEqual(self.core.store.get("sources", self.source["id"])["health"], "degraded")
        failed = next(v for v in self.state()["items"].values() if v["status"] == "failed")
        self.assertGreater(failed["next_retry_at"], failed["last_attempt_at"])
        self.assertEqual(sum(url == "https://public.test/0" for url, _ in self.fetcher.calls), 1)
        state = self.state()
        next(v for v in state["items"].values() if v["status"] == "failed")["next_retry_at"] = (
            "2020-01-01T00:00:00+00:00"
        )
        self.core.store.put("source_ingestion", self.source["id"], state)
        self.fetcher.mapping["https://public.test/0"] = FetchResult("https://public.test/0", b"recovered", "text/plain")
        self.fetch()
        self.assertEqual(self.summary()["failed_count"], 0)
        self.assertEqual(self.core.store.get("sources", self.source["id"])["health"], "healthy")

    def test_discovery_is_bounded_and_reports_truncation(self):
        self.listing(115)
        self.fetch()
        summary = self.summary()
        self.assertTrue(summary["window_truncated"])
        self.assertEqual(summary["known_count"], 100)
        validate("AdminSource", {**self.core.store.get("sources", self.source["id"]), "ingestion": summary})

    def test_real_worker_drains_continuation_jobs_without_new_payload_fields(self):
        from server.campus.worker import Worker

        self.listing(19)
        self.core.handlers.update(self.core.pipeline.handlers)
        self.core.store.enqueue("fetch_source", {"source_id": self.source["id"]}, "synthetic-first-fetch")
        worker = Worker(self.core)
        totals = []
        for _ in range(20):
            result = worker.run_once(max_jobs=100, sweep=False)
            self.assertEqual(result["failed"], 0)
            totals.append(result)
            if not result["processed"] and not result["dispatched"]:
                break
        self.assertEqual(self.summary()["completed_count"], 19)
        self.assertEqual(self.summary()["pending_count"], 0)
        self.assertEqual(len(self.core.store.list("snapshots")), 19)
        self.assertGreater(sum(v["processed"] for v in totals), 19)

    def test_admin_summary_is_contract_valid_and_not_exposed_to_students(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from server.campus.platform import error_handlers
        from server.campus.platform import make_router as platform_router
        from server.campus.sources import make_router

        self.listing(10)
        self.fetch()
        self.core.settings.admin_emails.add("fixture-admin@example.org")
        app = FastAPI()
        app.state.core = self.core
        error_handlers(app)
        app.include_router(platform_router(self.core), prefix="/api/v1")
        app.include_router(make_router(self.core), prefix="/api/v1")
        client = TestClient(app)
        public = client.get("/api/v1/sources").json()["items"][0]
        self.assertNotIn("ingestion", public)
        self.assertEqual(client.get("/api/v1/admin/sources").status_code, 401)
        challenge = client.post("/api/v1/auth/challenges", json={"email": "fixture-admin@example.org"})
        login = client.post(
            "/api/v1/auth/sessions",
            json={"challenge_id": challenge.json()["challenge_id"], "code": challenge.headers["X-Dev-Login-Code"]},
        )
        self.assertEqual(login.status_code, 200)
        response = client.get("/api/v1/admin/sources")
        self.assertEqual(response.status_code, 200)
        validate("AdminSourcePage", response.json())
        self.assertEqual(response.json()["items"][0]["ingestion"]["pending_count"], 2)

    def test_concurrent_fetch_is_rejected_without_corrupting_health(self):
        self.listing(1)
        backlog = SourceBacklog(self.core.store)
        _, state = backlog.acquire(self.source["id"])
        try:
            with self.assertRaisesRegex(FetchError, "source_busy"):
                self.fetch()
            self.assertEqual(self.core.store.get("sources", self.source["id"])["health"], "never_checked")
            self.assertFalse(self.fetcher.calls)
        finally:
            backlog.release(self.source["id"], state["lease_token"])
        self.fetch()
        self.assertEqual(len(self.core.store.list("snapshots")), 1)

    def test_source_identity_change_invalidates_backlog(self):
        self.listing(10)
        self.fetch()
        source = self.core.store.get("sources", self.source["id"])
        source["source_identity"] = "new-identity"
        self.core.store.put("sources", source["id"], source)
        self.assertIsNone(self.summary())
        _, state = SourceBacklog(self.core.store).acquire(source["id"])
        self.assertFalse(state["items"])
        SourceBacklog(self.core.store).release(source["id"], state["lease_token"])

    def test_temporary_listing_outage_does_not_block_known_pending_urls(self):
        self.listing(12)
        self.fetch()
        self.fetcher.mapping[self.source["origin_url"]] = FetchError("upstream_http_503")
        self.fetch()
        self.assertEqual(self.summary()["completed_count"], 12)
        self.assertEqual(self.summary()["pending_count"], 0)
        self.assertEqual(len(self.core.store.list("snapshots")), 12)
        source = self.core.store.get("sources", self.source["id"])
        self.assertEqual(source["health"], "degraded")
        self.assertEqual(source["last_error_code"], "upstream_http_503")
        self.assertEqual(self.state()["listing_failures"], 1)
        self.assertIsNotNone(self.state()["listing_retry_at"])

    def test_body_reversion_reactivates_old_immutable_snapshot_and_emits_update(self):
        self.listing(1)
        self.fetch()
        first = self.core.store.list("snapshots")[0]
        original = self.fetcher.mapping["https://public.test/0"]
        self.fetcher.mapping["https://public.test/0"] = FetchResult(
            "https://public.test/0", b"<h1>Changed B</h1><article>Changed</article>", "text/html"
        )
        self.fetch()
        second = self.core.store.get("documents", first["document_id"])["latest_snapshot_id"]
        self.assertNotEqual(second, first["snapshot_id"])
        self.fetcher.mapping["https://public.test/0"] = original
        self.fetch()
        document = self.core.store.get("documents", first["document_id"])
        self.assertEqual(document["latest_snapshot_id"], first["snapshot_id"])
        self.assertEqual(document["snapshot_observations"][first["snapshot_id"]], 2)
        self.assertEqual(self.core.store.get("snapshots", first["snapshot_id"]), first)
        self.assertEqual(len(self.core.store.list("snapshots")), 2)
        self.assertEqual(
            len([e for e in self.core.store.pending_outbox() if e["snapshot_id"] == first["snapshot_id"]]), 2
        )

    def test_reviewed_reactivation_publishes_and_delayed_old_candidate_cannot_undo_it(self):
        from server.campus.events import Events

        self.core.events = Events(self.core)
        self.source.update(verification="verified_organization", verification_basis="Synthetic test only")
        self.core.store.put("sources", self.source["id"], self.source)
        self.listing(1)

        def body(day):
            return FetchResult(
                "https://public.test/0",
                f"<h1>合成专业确认</h1><article><p>全体本科生请于2026年10月{day}日前提交申请表。</p></article>".encode(),
                "text/html",
            )

        def candidate():
            document = next(d for d in self.core.store.list("documents") if d["source_url"] == "https://public.test/0")
            return self.core.pipeline.extract_candidates({"snapshot_id": document["latest_snapshot_id"]})

        def approve(value):
            return self.core.pipeline.review(
                value["candidate_id"],
                dict(
                    expected_candidate_revision=value["revision"],
                    decision="accept",
                    reason="Synthetic review",
                    replacement_candidate=None,
                ),
                {"id": uid()},
            )

        def publish(value):
            return self.core.events.reconcile_candidate(value["candidate_id"], value["revision"])

        self.fetcher.mapping["https://public.test/0"] = body(20)
        self.fetch()
        first = approve(candidate())
        event = publish(first)
        self.fetcher.mapping["https://public.test/0"] = body(25)
        self.fetch()
        second = approve(candidate())
        changed = publish(second)
        self.assertEqual(changed["id"], event["id"])
        self.assertEqual(changed["deadlines"][0]["date"], "2026-10-25")
        delayed = approve(second)  # Accepted before reactivation, queue execution intentionally delayed.
        self.fetcher.mapping["https://public.test/0"] = body(20)
        self.fetch()
        reactivated = candidate()
        self.assertEqual(reactivated["review_status"], "pending")
        self.assertIn("document_reactivation_requires_review", reactivated["warnings"])
        restored = publish(approve(reactivated))
        self.assertEqual(restored["deadlines"][0]["date"], "2026-10-20")
        self.assertEqual(restored["version"], changed["version"] + 1)
        self.assertEqual(publish(delayed), restored)
        self.assertEqual(self.core.store.get("events", event["id"]), restored)

    def test_edit_during_network_fetch_cannot_commit_stale_snapshots(self):
        self.listing(1)
        original = self.fetcher.fetch

        def edited(url, headers=None):
            if url.endswith("/0"):
                source = self.core.store.get("sources", self.source["id"])
                source["origin_url"] = "https://public.test/new"
                self.core.store.put("sources", source["id"], source)
            return original(url, headers)

        self.fetcher.fetch = edited
        with self.assertRaisesRegex(FetchError, "source_changed_during_fetch"):
            self.fetch()
        self.assertFalse(self.core.store.list("snapshots"))
        self.assertEqual(self.core.store.get("sources", self.source["id"])["origin_url"], "https://public.test/new")

    def test_attachment_failure_recovers_same_parent_and_emits_metadata_update(self):
        self.listing(1)
        self.fetcher.mapping["https://public.test/0"] = FetchResult(
            "https://public.test/0", b'<h1>Fixture</h1><article><a href="/a.doc">attachment</a></article>', "text/html"
        )
        self.fetcher.mapping["https://public.test/a.doc"] = FetchError("upstream_http_503")
        self.fetch()
        parent = self.core.store.list("snapshots")[0]
        sid = parent["snapshot_id"]
        did = parent["document_id"]
        doc = self.core.store.get("documents", did)
        self.assertEqual(doc["snapshot_attachments"][sid], [])
        self.assertIn("attachment_fetch_upstream_http_503", doc["snapshot_warnings"][sid])
        state = self.state()
        next(iter(state["items"].values()))["next_retry_at"] = "2020-01-01T00:00:00+00:00"
        self.core.store.put("source_ingestion", self.source["id"], state)
        self.fetcher.mapping["https://public.test/a.doc"] = FetchResult(
            "https://public.test/a.doc", bytes.fromhex("D0CF11E0A1B11AE1") + b"fixture", "application/msword"
        )
        self.fetch()
        doc = self.core.store.get("documents", did)
        self.assertEqual(doc["snapshot_warnings"][sid], [])
        self.assertEqual(len(doc["snapshot_attachments"][sid]), 1)
        events = [v for v in self.core.store.pending_outbox() if v["snapshot_id"] == sid]
        self.assertEqual(len(events), 2)

    def test_attachment_cap_is_visible_and_download_mime_is_recognized(self):
        self.listing(1)
        html = "<h1>Fixture</h1><article>" + "".join(f'<a href="/a{i}.pdf">A{i}</a>' for i in range(6)) + "</article>"
        self.fetcher.mapping["https://public.test/0"] = FetchResult("https://public.test/0", html.encode(), "text/html")
        for i in range(6):
            url = f"https://public.test/a{i}.pdf"
            self.fetcher.mapping[url] = FetchResult(url, b"%PDF-fixture", "application/pdf")
        self.fetch()
        parent = next(v for v in self.core.store.list("snapshots") if v["parent_snapshot_id"] is None)
        doc = self.core.store.get("documents", parent["document_id"])
        self.assertIn("attachment_limit_reached", doc["snapshot_warnings"][parent["snapshot_id"]])
        self.assertEqual(len(doc["snapshot_attachments"][parent["snapshot_id"]]), 4)
        for kind, name in [("docx", "word/document.xml"), ("xlsx", "xl/workbook.xml")]:
            body = io.BytesIO()
            with zipfile.ZipFile(body, "w") as archive:
                archive.writestr(name, "<fixture/>")
            self.assertEqual(
                content_kind(
                    FetchResult("https://public.test/download.jsp?id=1", body.getvalue(), "application/octet-stream")
                ),
                kind,
            )

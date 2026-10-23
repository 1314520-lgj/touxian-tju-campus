"""Module tests use real SQLite Store/contracts. Network doubles are labeled fixtures."""

import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.campus.content import Pipeline, date_values
from server.campus.content import make_router as content_router
from server.campus.platform import APIError, Core, Settings, error_handlers
from server.campus.platform import make_router as platform_router
from server.campus.schema import uid, validate
from server.campus.sources import FetchError, FetchResult, SafeFetcher, canonical_url, feed_items, resolve_public
from server.campus.sources import make_router as source_router

PUBLIC_IP = "93.184.216.34"


def dns(host, port, **kwargs):
    ip = {"private.test": "127.0.0.1", "metadata.test": "169.254.169.254"}.get(host, PUBLIC_IP)
    return [(2, 1, 6, "", (ip, port))]


class Response:
    def __init__(self, status=200, body=b"ok", headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = headers or {"Content-Type": "text/html"}

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read(self, size):
        return self.body.read(size)


class Connector:
    responses = []
    calls = []

    def __init__(self, host, ip, port, timeout):
        self.host = host
        self.ip = ip
        self.port = port

    def request(self, method, path, headers):
        self.calls.append((self.host, self.ip, path, headers))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        pass


class MappingFetcher:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []
        self.trusted_origins = ()

    def fetch(self, url, headers=None):
        self.calls.append((url, headers))
        value = self.mapping.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise FetchError("fixture_url_missing")
        return value


class SecurityTests(unittest.TestCase):
    def setUp(self):
        Connector.calls = []
        Connector.responses = []

    def test_private_metadata_and_mixed_dns_rejected(self):
        for host in ("private.test", "metadata.test"):
            with self.assertRaisesRegex(FetchError, "unsafe_address"):
                resolve_public("http://" + host, dns)

        def mixed(*a, **k):
            return [(2, 1, 6, "", (PUBLIC_IP, 80)), (2, 1, 6, "", ("10.0.0.1", 80))]

        with self.assertRaisesRegex(FetchError, "unsafe_address"):
            resolve_public("http://public.test", mixed)

    def test_redirect_to_private_never_connects(self):
        Connector.responses = [Response(302, headers={"Location": "http://private.test/"})]
        with self.assertRaisesRegex(FetchError, "unsafe_address"):
            SafeFetcher(dns, Connector).fetch("http://public.test/")
        self.assertEqual(len(Connector.calls), 1)

    def test_connection_pinned_and_redirect_credentials_not_forwarded(self):
        Connector.responses = [Response(302, headers={"Location": "https://other.test/notice"}), Response()]
        result = SafeFetcher(dns, Connector).fetch("https://public.test/feed", {"Authorization": "Bearer SYNTHETIC"})
        self.assertEqual(result.body, b"ok")
        self.assertEqual(Connector.calls[0][1], PUBLIC_IP)
        self.assertIn("Authorization", Connector.calls[0][3])
        self.assertNotIn("Authorization", Connector.calls[1][3])
        self.assertEqual(Connector.calls[1][3]["Host"], "other.test")

    def test_limits_compression_scheme_and_userinfo(self):
        for response, code in [
            (Response(body=b"12345"), "body_too_large"),
            (Response(headers={"Content-Encoding": "gzip"}), "compressed_response_denied"),
        ]:
            Connector.responses = [response]
            with self.assertRaisesRegex(FetchError, code):
                SafeFetcher(dns, Connector, max_bytes=4).fetch("https://public.test")
        for url in ("file:///tmp/test", "http://u:p@public.test", "http://public.test:22"):
            with self.assertRaises(FetchError):
                canonical_url(url)

    def test_gateway_allowlist_is_exact_and_metadata_still_denied(self):
        with self.assertRaises(FetchError):
            resolve_public("http://private.test", dns, ("http://other.test",))
        self.assertEqual(resolve_public("http://private.test", dns, ("http://private.test",))[1], "127.0.0.1")
        with self.assertRaises(FetchError):
            resolve_public("http://metadata.test", dns, ("http://metadata.test",))

    def test_gateway_trust_never_allows_metadata_or_mapped_metadata(self):
        for address in ("100.100.100.200", "fd00:ec2::254", "::ffff:169.254.169.254"):

            def resolver(*args, **kwargs):
                return [(2, 1, 6, "", (address, 80))]

            with self.assertRaisesRegex(FetchError, "unsafe_address"):
                resolve_public("http://metadata.test", resolver, ("http://metadata.test",))

    def test_xml_entities_rejected_and_atom_supported(self):
        result = FetchResult("https://public.test/rss", b'<!DOCTYPE x [<!ENTITY a "x">]><rss/>', "application/xml")
        with self.assertRaisesRegex(FetchError, "unsafe_xml"):
            feed_items(result, "rss", {})
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Fixture</title><link href="https://public.test/a"/><updated>2026-10-01T08:00:00Z</updated></entry></feed>'
        self.assertEqual(
            feed_items(FetchResult("https://public.test", atom, "application/xml"), "werss", {})[0]["url"],
            "https://public.test/a",
        )


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.core = Core(
            Settings(data_dir=Path(self.tmp.name), secret="s" * 48, dev_auth=True, admin_emails={"admin@test.example"})
        )
        self.core.pipeline = Pipeline(self.core)
        self.core.pipeline.sources.seed()
        self.source = self.core.store.list("sources")[0]
        self.allowed = set()
        self.core.events = SimpleNamespace(
            can_read_snapshot=lambda sid: sid in self.allowed, get_public=lambda eid: {"id": eid}
        )

    def tearDown(self):
        self.core.store.close()
        self.tmp.cleanup()

    def notice(
        self,
        text="全体本科生请于2026年10月20日前提交申请表。",
        title="合成报名通知",
        published="2026-10-08T12:00:00+08:00",
    ):
        html = f'<html><h1>{title}</h1><div class="v_news_content"><p>{text}</p></div></html>'.encode()
        with self.core.store.transaction():
            snapshot, _ = self.core.pipeline.sources.persist(
                self.source, FetchResult("https://public.test/info/1/1.htm", html, "text/html"), title, published
            )
        self.core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
        return snapshot

    def test_config_only_or_unrecognized_entries_never_become_official_seeds(self):
        config_path = Path(self.tmp.name) / "feeds.json"
        identity = uid()
        entries = [
            {"id": identity, "config": {"list_selector": "article a"}},
            {
                "id": uid(),
                "name": "Unverified fixture",
                "kind": "website",
                "adapter": "website",
                "source_identity": "custom",
                "origin_url": "https://example.com",
                "verification_basis": "not a builtin",
            },
        ]
        config_path.write_text(json.dumps({"sources": entries}), encoding="utf-8")
        self.core.pipeline.sources.config_path = config_path
        before = len(self.core.store.list("sources"))
        self.core.pipeline.sources.seed()
        self.assertEqual(len(self.core.store.list("sources")), before)
        self.assertIsNone(self.core.store.get("sources", identity))
        self.assertEqual(self.core.pipeline.sources.config({"id": identity})["list_selector"], "article a")

    def test_date_precision_never_fabricates_end_of_day(self):
        eid = uid()
        values = date_values("请于2026年10月20日前提交申请表", "2026-10-08T12:00:00+08:00", eid)
        self.assertEqual(values[0]["precision"], "date")
        self.assertEqual(values[0]["date"], "2026-10-20")
        self.assertIsNone(values[0]["at"])
        validate("Deadline", values[0])
        minute = date_values("请于2026年10月20日下午3点前提交", "2026-10-08T12:00:00+08:00", eid)[0]
        self.assertEqual(minute["at"], "2026-10-20T15:00:00+08:00")
        unknown = date_values("请于周五前提交材料", None, eid)[0]
        self.assertEqual(unknown["precision"], "unknown")
        self.assertIsNone(unknown["date"])

    def test_snapshot_dedup_immutable_and_parent(self):
        snapshot = self.notice()
        body = (self.core.data_dir / snapshot["object_key"]).read_bytes()
        with self.core.store.transaction():
            same, new = self.core.pipeline.sources.persist(
                self.source, FetchResult(snapshot["source_url"], body, "text/html"), "other", None
            )
            child, child_new = self.core.pipeline.sources.persist(
                self.source,
                FetchResult("https://public.test/a.pdf", b"%PDF-fixture", "application/pdf"),
                "附件",
                None,
                snapshot["snapshot_id"],
            )
        self.assertFalse(new)
        self.assertEqual(same, snapshot)
        self.assertTrue(child_new)
        self.assertEqual(child["parent_snapshot_id"], snapshot["snapshot_id"])
        with self.assertRaises(OSError):
            (self.core.data_dir / snapshot["object_key"]).open("xb")

    def test_evidence_quote_dedup_and_fabrication_rejected(self):
        snapshot = self.notice()
        sid = snapshot["snapshot_id"]
        segment = self.core.pipeline.parsed(sid)["segments"][1]
        a = self.core.pipeline.register_evidence(sid, segment["id"], "提交申请表")
        b = self.core.pipeline.register_evidence(sid, segment["id"], "提交申请表")
        self.assertEqual(a, b)
        with self.assertRaises(APIError):
            self.core.pipeline.register_evidence(sid, segment["id"], "不存在的材料")

    def test_clear_candidate_requires_review_and_keeps_evidence(self):
        snapshot = self.notice()
        c = self.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
        validate("ExtractionCandidate", c)
        self.assertEqual(c["review_status"], "pending")
        self.assertTrue(c["action_required"])
        self.assertEqual(c["audience"]["education_levels"], ["undergraduate"])
        self.assertEqual(c["deadlines"][0]["precision"], "date")
        self.assertIsNone(self.core.pipeline.get_accepted_candidate(c["candidate_id"], 1))
        self.assertFalse(any(e["topic"] == "candidate.accepted" for e in self.core.store.pending_outbox()))

    def test_unknown_time_requires_review_and_revision_conflicts(self):
        snapshot = self.notice("全体本科生请于周五前提交申请表。", published=None)
        candidate = self.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
        self.assertTrue(candidate["review_required"])
        decision = {
            "expected_candidate_revision": 1,
            "decision": "accept",
            "reason": "保留待确认日期",
            "replacement_candidate": None,
        }
        accepted = self.core.pipeline.review(candidate["candidate_id"], decision, {"id": uid()})
        self.assertEqual(accepted["revision"], 2)
        self.assertIsNone(self.core.pipeline.get_accepted_candidate(candidate["candidate_id"], 1))
        self.assertEqual(self.core.pipeline.get_accepted_candidate(candidate["candidate_id"], 2), accepted)
        with self.assertRaises(APIError) as caught:
            self.core.pipeline.review(candidate["candidate_id"], decision, {"id": uid()})
        self.assertEqual(caught.exception.status, 409)

    def test_manual_fabricated_date_or_action_cannot_be_accepted(self):
        snapshot = self.notice()
        c = self.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
        for field in ("deadlines", "action_steps"):
            replacement = copy.deepcopy(c)
            if field == "deadlines":
                replacement[field][0]["date"] = "2026-12-31"
            else:
                replacement[field] = ["缴费999元"]
            with self.assertRaises(APIError):
                self.core.pipeline.review(
                    c["candidate_id"],
                    {
                        "expected_candidate_revision": 1,
                        "decision": "accept",
                        "reason": "fixture",
                        "replacement_candidate": replacement,
                    },
                    {"id": uid()},
                )

    def test_cancel_requires_evidence_and_explicit_target(self):
        snapshot = self.notice("原活动取消，不再举办。", title="合成活动取消通知")
        c = self.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
        self.assertEqual(c["review_status"], "pending")
        with self.assertRaises(APIError):
            self.core.pipeline.review(
                c["candidate_id"],
                {
                    "expected_candidate_revision": 1,
                    "decision": "accept",
                    "reason": "fixture",
                    "replacement_candidate": None,
                },
                {"id": uid()},
            )
        replacement = copy.deepcopy(c)
        replacement["target_event_id"] = uid()
        accepted = self.core.pipeline.review(
            c["candidate_id"],
            {
                "expected_candidate_revision": 1,
                "decision": "accept",
                "reason": "fixture确认原事项",
                "replacement_candidate": replacement,
            },
            {"id": uid()},
        )
        self.assertEqual(accepted["proposed_status"], "cancelled")

    def test_scan_pdf_no_fake_ocr_success(self):
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(100, 100)
        buf = io.BytesIO()
        writer.write(buf)
        with self.core.store.transaction():
            snapshot, _ = self.core.pipeline.sources.persist(
                self.source,
                FetchResult("https://public.test/scan.pdf", buf.getvalue(), "application/pdf"),
                "fixture",
                None,
            )
        parsed = self.core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
        self.assertEqual(parsed["state"], "failed")
        self.assertTrue(any("ocr" in w for w in parsed["warnings"]))

    def test_fetch_health_actual_partial_and_failure(self):
        source = {**self.source, "origin_url": "https://public.test/list", "adapter": "rss"}
        self.core.store.put("sources", source["id"], source)
        rss = b"<rss><channel><item><title>Fixture</title><link>https://public.test/one</link></item><item><title>Bad</title><link>https://public.test/missing</link></item></channel></rss>"
        fetcher = MappingFetcher(
            {
                "https://public.test/list": FetchResult("https://public.test/list", rss, "application/xml"),
                "https://public.test/one": FetchResult(
                    "https://public.test/one", b"<h1>Fixture</h1><article>Some text</article>", "text/html"
                ),
                "https://public.test/missing": FetchError("auth_expired"),
            }
        )
        self.core.pipeline.sources.fetcher = fetcher
        result = self.core.pipeline.sources.fetch_source({"source_id": source["id"], "job_id": uid()})
        self.assertEqual(len(result["snapshot_ids"]), 1)
        self.assertEqual(self.core.store.get("sources", source["id"])["health"], "degraded")
        fetcher.mapping["https://public.test/list"] = FetchError("auth_expired")
        with self.assertRaises(FetchError):
            self.core.pipeline.sources.fetch_source({"source_id": source["id"]})
        self.assertEqual(self.core.store.get("sources", source["id"])["health"], "auth_expired")

    def app(self):
        app = FastAPI()
        app.state.core = self.core
        error_handlers(app)
        for factory in (platform_router, source_router, content_router):
            app.include_router(factory(self.core), prefix="/api/v1")
        return TestClient(app)

    def login(self, client, email="admin@test.example"):
        challenge = client.post("/api/v1/auth/challenges", json={"email": email})
        self.assertEqual(challenge.status_code, 201, challenge.text)
        login = client.post(
            "/api/v1/auth/sessions",
            json={"challenge_id": challenge.json()["challenge_id"], "code": challenge.headers["X-Dev-Login-Code"]},
        )
        self.assertEqual(login.status_code, 200, login.text)
        return {"X-CSRF-Token": login.json()["csrf_token"], "Idempotency-Key": "fixture-create"}

    def test_api_auth_csrf_source_idempotency_and_redaction(self):
        client = self.app()
        self.assertEqual(client.get("/api/v1/admin/sources").status_code, 401)
        headers = self.login(client)
        body = {
            "name": "Fixture",
            "kind": "rss",
            "origin_url": "https://public.test/rss",
            "adapter": "rss",
            "source_identity": None,
        }
        self.assertEqual(client.post("/api/v1/admin/sources", json=body).status_code, 403)
        with patch("server.campus.sources.resolve_public", return_value=("public.test", PUBLIC_IP, 443)):
            created = client.post("/api/v1/admin/sources", json=body, headers=headers)
            self.assertEqual(created.status_code, 201, created.text)
            self.assertEqual(client.post("/api/v1/admin/sources", json=body, headers=headers).json(), created.json())
            changed = {**body, "name": "other"}
            self.assertEqual(client.post("/api/v1/admin/sources", json=changed, headers=headers).status_code, 409)
        source_id = created.json()["id"]
        with patch.dict(
            os.environ, {"CAMPUS_SOURCE_" + source_id.replace("-", "").upper() + "_TOKEN": "SYNTHETIC_SECRET"}
        ):
            self.assertNotIn("SYNTHETIC_SECRET", client.get("/api/v1/admin/sources").text)
            self.assertNotIn("verification_basis", client.get("/api/v1/sources").text)
        student = self.app()
        student_headers = self.login(student, "student@test.example")
        self.assertEqual(
            student.post(f"/api/v1/admin/sources/{source_id}/fetch", headers=student_headers).status_code, 403
        )

    def test_snapshot_signature_rechecks_withdrawal_and_parsed_evidence(self):
        snapshot = self.notice()
        sid = snapshot["snapshot_id"]
        self.core.pipeline.extract_candidates({"snapshot_id": sid})
        client = self.app()
        self.assertEqual(client.get(f"/api/v1/snapshots/{sid}").status_code, 404)
        self.allowed.add(sid)
        result = client.get(f"/api/v1/snapshots/{sid}")
        self.assertEqual(result.status_code, 200, result.text)
        url = result.json()["signed_url"]
        self.assertEqual(client.get(url).status_code, 200)
        self.assertEqual(client.get(f"/api/v1/snapshots/{sid}/parsed").status_code, 200)
        self.allowed.clear()
        self.assertEqual(client.get(url).status_code, 404)
        self.assertEqual(client.get(f"/api/v1/snapshots/{sid}/parsed").status_code, 404)

    def test_admin_unpublished_snapshot_actor_binding_logout_and_role_loss(self):
        snapshot = self.notice()
        sid = snapshot["snapshot_id"]
        client = self.app()
        headers = self.login(client)
        result = client.get(f"/api/v1/admin/snapshots/{sid}")
        self.assertEqual(result.status_code, 200, result.text)
        url = result.json()["signed_url"]
        self.assertEqual(client.get(url).status_code, 200)
        self.assertEqual(client.get(f"/api/v1/admin/snapshots/{sid}/parsed").status_code, 200)
        self.assertEqual(client.get(f"/api/v1/snapshots/{sid}").status_code, 404)
        other = self.app()
        self.login(other, "other@test.example")
        operator = next(u for u in self.core.store.list("users") if u["email"] == "other@test.example")
        operator["role"] = "operator"
        self.core.store.put("users", operator["id"], operator)
        self.assertEqual(other.get(url).status_code, 403)
        self.core.settings.admin_emails.clear()
        self.assertEqual(client.get(url).status_code, 403)
        self.core.settings.admin_emails.add("admin@test.example")
        client.delete("/api/v1/auth/session", headers=headers)
        self.assertEqual(client.get(url).status_code, 401)

    def test_changed_attachment_updates_candidate_revision_and_history(self):
        snapshot = self.notice()
        sid = snapshot["snapshot_id"]
        with self.core.store.transaction():
            child, _ = self.core.pipeline.sources.persist(
                self.source,
                FetchResult(
                    "https://public.test/attachment.txt",
                    "全体本科生请于2026年10月20日前提交材料。".encode(),
                    "text/plain",
                ),
                "附件",
                snapshot["published_at"],
                sid,
            )
        self.core.pipeline.parse_document({"snapshot_id": child["snapshot_id"]})
        first = self.core.pipeline.extract_candidates({"snapshot_id": sid})
        with self.core.store.transaction():
            changed, _ = self.core.pipeline.sources.persist(
                self.source,
                FetchResult(
                    "https://public.test/attachment.txt",
                    "全体本科生请于2026年10月22日前提交材料。".encode(),
                    "text/plain",
                ),
                "附件",
                snapshot["published_at"],
                sid,
            )
        second = self.core.pipeline.extract_candidates({"snapshot_id": changed["snapshot_id"]})
        self.assertEqual(first["candidate_id"], second["candidate_id"])
        self.assertEqual(second["revision"], 2)
        self.assertNotIn(child["snapshot_id"], second["snapshot_ids"])
        self.assertIn(changed["snapshot_id"], second["snapshot_ids"])
        self.assertEqual(self.core.store.get("candidate_versions", f"{first['candidate_id']}:1"), first)
        self.assertIsNone(self.core.pipeline.get_accepted_candidate(first["candidate_id"], 1))

    def test_url_date_never_becomes_deadline(self):
        values = date_values("请于稍后提交，入口 https://public.test/2026/10/20/form", "2026-10-08T00:00:00Z", uid())
        self.assertFalse(any(i["date"] for i in values))

    def test_audience_manual_injection_rejected(self):
        snapshot = self.notice()
        c = self.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
        replacement = copy.deepcopy(c)
        replacement["audience"]["college_ids"] = ["mechanical"]
        with self.assertRaises(APIError):
            self.core.pipeline.review(
                c["candidate_id"],
                {
                    "expected_candidate_revision": 1,
                    "decision": "accept",
                    "reason": "fixture",
                    "replacement_candidate": replacement,
                },
                {"id": uid()},
            )

    def test_change_webhook_cannot_fetch_arbitrary_url(self):
        client = self.app()
        body = {
            "source_id": self.source["id"],
            "source_url": "http://127.0.0.1/secret",
            "observed_at": "2026-10-08T00:00:00Z",
            "provider_change_id": "fixture",
        }
        self.assertEqual(client.post("/api/v1/internal/changes", json=body).status_code, 401)
        with patch.dict(os.environ, {"CAMPUS_CHANGE_TOKEN": "x" * 48}):
            headers = {"Authorization": "Bearer " + "x" * 48, "Idempotency-Key": "fixture"}
            self.assertEqual(client.post("/api/v1/internal/changes", json=body, headers=headers).status_code, 422)
            body["source_url"] = self.source["origin_url"]
            r = client.post("/api/v1/internal/changes", json=body, headers=headers)
            self.assertEqual(r.status_code, 202, r.text)
            job = self.core.store.get_job(r.json()["id"])
            self.assertEqual(job["kind"], "fetch_source")


if __name__ == "__main__":
    unittest.main()

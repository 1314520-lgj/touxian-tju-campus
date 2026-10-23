"""Synthetic attachment restrictions, public quality and operator replacement gates."""

from __future__ import annotations

import asyncio
import copy
import io
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.concurrency import run_in_threadpool

from server.campus.authorized_upload import MAX_XML_DEPTH, MAX_XML_NODES, validate_file
from server.campus.content import html_body_segments
from server.campus.platform import APIError, Settings
from server.campus.schema import uid, validate
from server.campus.sources import FetchResult, attachment_access_restricted
from server.campus.tests.test_integration import synthetic_source
from server.campus.tests.test_pipeline import MappingFetcher
from server.campus_app import create_app

CAPTCHA = b"""<!DOCTYPE html><html><head><title>Attachment download</title></head><body>
<input id="codeValue" type="text"><input type="button" value="download">
<script>document.write('<img src="/system/resource/js/filedownload/createimage.jsp?randnum=1">');</script>
</body></html>"""
ATTACHMENT_URL = "https://example.com/system/_content/download.jsp?urltype=news.DownloadAttachUrl&wbfileid=fixture"
REPLACEMENT_TEXT = "材料：确认表、授权附件中的合成补充证明。".encode()


def office_file(kind):
    """Minimal real OPC document fixtures; no optional document libraries needed."""
    office = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package = "http://schemas.openxmlformats.org/package/2006/"
    part = "word/document.xml" if kind == "docx" else "xl/workbook.xml"
    mime = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
        if kind == "docx"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    )
    files = {
        "[Content_Types].xml": f'<Types xmlns="{package}content-types"><Override PartName="/{part}" ContentType="{mime}"/></Types>',
        "_rels/.rels": f'<Relationships xmlns="{package}relationships"><Relationship Id="rId1" Type="{office}/officeDocument" Target="{part}"/></Relationships>',
    }
    if kind == "docx":
        files[part] = (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>材料：合成授权证明。</w:t></w:r></w:p></w:body></w:document>'
        )
    else:
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        files[part] = (
            f'<workbook xmlns="{ns}" xmlns:r="{office}"><sheets><sheet name="Materials" sheetId="1" r:id="rId1"/></sheets></workbook>'
        )
        files["xl/_rels/workbook.xml.rels"] = (
            f'<Relationships xmlns="{package}relationships"><Relationship Id="rId1" Type="{office}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
        )
        files["xl/worksheets/sheet1.xml"] = (
            f'<worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>材料：合成授权证明。</t></is></c></row></sheetData></worksheet>'
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value.encode("utf-8"))
    return buffer.getvalue()


def office_with_parts(kind, replacements):
    with zipfile.ZipFile(io.BytesIO(office_file(kind))) as archive:
        parts = {entry.filename: archive.read(entry) for entry in archive.infolist()}
    parts.update(replacements)
    buffer = io.BytesIO()
    # Keep repeated XML uncompressed so tests reach the XML boundary, not ratio checks.
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return buffer.getvalue()


class AuthorizedReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ, {"CAMPUS_MEILI_URL": "", "CAMPUS_DEMO": "false", "CAMPUS_BLOB_STORE": "local"}
        )
        self.environment.start()
        self.app = create_app(
            Settings(
                data_dir=Path(self.temp.name),
                secret="replacement-test-secret-" * 3,
                dev_auth=True,
                admin_emails={"admin@example.com"},
            ),
            start_worker=False,
        )
        self.core = self.app.state.core
        self.client = TestClient(self.app)
        self.clients = [self.client]
        self.no_network = patch(
            "server.campus.sources.SafeFetcher.fetch", side_effect=AssertionError("Synthetic tests must never fetch")
        )
        self.no_network.start()
        self.source = synthetic_source(self.core)

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.core.store.close()
        self.no_network.stop()
        self.environment.stop()
        self.temp.cleanup()

    def fixture(self, publish=True):
        body = (
            """<html><h1>合成授权附件通知</h1><article><p>全体本科生请于2030年1月3日前提交确认表。</p>
<p>请按附件准备材料。</p></article></html>"""
        ).encode()
        with self.core.store.transaction():
            parent, _ = self.core.pipeline.sources.persist(
                self.source,
                FetchResult("https://example.com/integration/replacement", body, "text/html; charset=utf-8"),
                "合成授权附件通知",
                "2030-01-01T00:00:00Z",
            )
            child, _ = self.core.pipeline.sources.persist(
                self.source,
                FetchResult(ATTACHMENT_URL, CAPTCHA, "text/html"),
                "合成附件",
                parent["published_at"],
                parent["snapshot_id"],
            )
            document = self.core.store.get("documents", parent["document_id"])
            document.setdefault("snapshot_attachments", {})[parent["snapshot_id"]] = [child["snapshot_id"]]
            document.setdefault("snapshot_warnings", {})[parent["snapshot_id"]] = ["attachment_access_restricted"]
            self.core.store.put("documents", document["id"], document)
        candidate = self.core.pipeline.extract_candidates({"snapshot_id": parent["snapshot_id"]})
        event = self.approve(candidate) if publish else None
        return {"parent": parent, "child": child, "candidate": candidate, "event": event}

    def approve(self, candidate):
        accepted = self.core.pipeline.review(
            candidate["candidate_id"],
            {
                "expected_candidate_revision": candidate["revision"],
                "decision": "accept",
                "reason": "本地合成授权文件人工核对",
                "replacement_candidate": None,
            },
            {"id": uid()},
        )
        return self.core.events.reconcile_candidate(candidate["candidate_id"], accepted["revision"])

    def auth(self, email="admin@example.com", role=None):
        client = TestClient(self.app)
        self.clients.append(client)
        challenge = client.post("/api/v1/auth/challenges", json={"email": email})
        self.assertEqual(challenge.status_code, 201, challenge.text)
        session = client.post(
            "/api/v1/auth/sessions",
            json={"challenge_id": challenge.json()["challenge_id"], "code": challenge.headers["X-Dev-Login-Code"]},
        )
        self.assertEqual(session.status_code, 200, session.text)
        user = session.json()["user"]
        if role:
            user["role"] = role
            self.core.store.put("users", user["id"], user)
        return client, user, {"X-CSRF-Token": session.json()["csrf_token"], "Idempotency-Key": uid()}

    def upload(
        self,
        fixture,
        client=None,
        headers=None,
        body=REPLACEMENT_TEXT,
        basis="来源维护者授权提供的合成文件",
        filename="authorized.txt",
        mime="text/plain",
    ):
        if client is None:
            client, _, headers = self.auth()
        return client.post(
            f"/api/v1/admin/snapshots/{fixture['child']['snapshot_id']}/replacement",
            files={"file": (filename, body, mime)},
            data={"basis": basis},
            headers=headers,
        )

    def quality(self, event_id, status=200):
        response = self.client.get(f"/api/v1/events/{event_id}/quality")
        self.assertEqual(response.status_code, status, response.text)
        if status == 200:
            validate("EventDocumentQuality", response.json())
        return response.json()

    def test_captcha_attachment_has_explicit_failed_quality(self):
        f = self.fixture()
        parsed = self.core.pipeline.parsed(f["child"]["snapshot_id"])
        self.assertEqual(parsed["state"], "failed")
        self.assertIn("attachment_access_restricted", parsed["warnings"])
        q = self.quality(f["event"]["id"])
        self.assertFalse(q["complete"])
        child = next(d for d in q["documents"] if d["is_attachment"])
        self.assertEqual(child["state"], "failed")
        self.assertIn("attachment_access_restricted", child["warnings"])

    def test_ordinary_notice_mentioning_a_verification_code_is_not_restricted(self):
        text = "<h1>办理说明</h1><article><p>全体本科生报名后输入验证码完成身份确认。</p></article>".encode()
        segments, state, warnings = html_body_segments(text, "https://example.com/info/notice.htm")
        self.assertEqual(state, "ok")
        self.assertTrue(segments)
        self.assertNotIn("attachment_access_restricted", warnings)
        # Even on download.jsp the words alone cannot replace the two structural controls.
        self.assertFalse(attachment_access_restricted(text, ATTACHMENT_URL))

    def test_http_200_restriction_degrades_source_and_completes_the_notice(self):
        """The notice is captured; only its captcha-gated attachment is missing.

        Round 19 (t21): the item used to be marked failed so the whole article was
        refetched, but a download captcha is recognized by its own controls
        (`codevalue` + `createimage.jsp`) and nothing about it improves by fetching
        the article again — meanwhile a captured notice made the five 教务处 columns
        read "completed 0, all failed" for ever. The warning stays on the snapshot,
        the source still reports degraded, and a transient attachment failure
        (`attachment_fetch_*`) still schedules a retry.
        """
        source = {**self.source, "adapter": "rss", "origin_url": "https://example.com/integration/feed"}
        self.core.store.put("sources", source["id"], source)
        listing = b"<rss><channel><item><title>Fixture</title><link>https://example.com/integration/body</link></item></channel></rss>"
        body = (
            f'<h1>Fixture</h1><article><p>Public body</p><a href="{ATTACHMENT_URL}">Attachment</a></article>'.encode()
        )
        self.core.pipeline.sources.fetcher = MappingFetcher(
            {
                source["origin_url"]: FetchResult(source["origin_url"], listing, "application/xml"),
                "https://example.com/integration/body": FetchResult(
                    "https://example.com/integration/body", body, "text/html"
                ),
                ATTACHMENT_URL: FetchResult(ATTACHMENT_URL, CAPTCHA, "text/html"),
            }
        )
        result = self.core.pipeline.sources.fetch_source({"source_id": source["id"], "job_id": uid()})
        self.assertIn("attachment_access_restricted", result["errors"])
        self.assertEqual(self.core.store.get("sources", source["id"])["health"], "degraded")
        state = self.core.store.get("source_ingestion", source["id"])
        item = next(iter(state["items"].values()))
        self.assertEqual(item["status"], "completed", item)
        self.assertIsNone(item["error_code"])
        snapshot = next(s for s in self.core.store.list("snapshots") if s["source_url"] == "https://example.com/integration/body")
        document = self.core.store.get("documents", snapshot["document_id"])
        self.assertIn(
            "attachment_access_restricted", document["snapshot_warnings"][snapshot["snapshot_id"]]
        )

    def test_unknown_and_withdrawn_events_return_404(self):
        self.quality(uid(), 404)
        f = self.fixture()
        self.core.events.publication(
            f["event"]["id"],
            {
                "expected_event_version": f["event"]["version"],
                "publication_state": "withdrawn",
                "reason": "本地合成撤回",
            },
            uid(),
        )
        self.quality(f["event"]["id"], 404)

    def test_unparsed_is_explicit_and_quality_does_not_parse_or_publish(self):
        f = self.fixture()
        self.core.store.delete("parsed", f["parent"]["snapshot_id"])
        q = self.quality(f["event"]["id"])
        root = next(d for d in q["documents"] if not d["is_attachment"])
        self.assertEqual(root["state"], "unparsed")
        self.assertFalse(q["complete"])
        self.assertIsNone(self.core.pipeline.parsed(f["parent"]["snapshot_id"]))
        self.assertEqual(self.core.events.get_public(f["event"]["id"]), f["event"])

    def test_quality_ignores_new_unapproved_attachment_and_redacts_url_credentials(self):
        f = self.fixture()
        replacement, _ = self.core.pipeline.sources.persist(
            self.source,
            FetchResult(ATTACHMENT_URL, REPLACEMENT_TEXT, "text/plain"),
            "unapproved",
            f["parent"]["published_at"],
            f["parent"]["snapshot_id"],
            content_kind_override="text",
        )
        stored = self.core.store.get("snapshots", f["child"]["snapshot_id"])
        stored["source_url"] += "&token=SYNTHETIC_SECRET&file=fixture"
        self.core.store.put("snapshots", stored["snapshot_id"], stored)
        q = self.quality(f["event"]["id"])
        self.assertNotIn(replacement["snapshot_id"], str(q))
        self.assertNotIn("SYNTHETIC_SECRET", str(q))
        self.assertNotIn("token=", str(q))
        self.assertIn("file=fixture", str(q))

    def test_authorization_csrf_origin_and_idempotency_are_required(self):
        f = self.fixture()
        self.assertEqual(self.upload(f, self.client, {}).status_code, 401)
        student, _, student_headers = self.auth("student@example.com")
        self.assertEqual(self.upload(f, student, student_headers).status_code, 403)
        admin, _, headers = self.auth()
        self.assertEqual(self.upload(f, admin, {"Idempotency-Key": uid()}).status_code, 403)
        self.assertEqual(self.upload(f, admin, {"X-CSRF-Token": headers["X-CSRF-Token"]}).status_code, 422)
        self.assertEqual(self.upload(f, admin, {**headers, "Origin": "https://untrusted.example"}).status_code, 403)

    def test_operator_can_supply_file_and_role_loss_blocks_it(self):
        f = self.fixture()
        operator, user, headers = self.auth("operator@example.com", "operator")
        response = self.upload(f, operator, headers)
        self.assertEqual(response.status_code, 201, response.text)
        user["role"] = "student"
        self.core.store.put("users", user["id"], user)
        self.assertEqual(self.upload(f, operator, headers).status_code, 403)

    def test_same_key_replays_once_and_changed_file_or_basis_conflicts(self):
        f = self.fixture()
        admin, _, headers = self.auth()
        before = len(self.core.store.list("snapshots"))
        first = self.upload(f, admin, headers)
        self.assertEqual(first.status_code, 201, first.text)
        second = self.upload(f, admin, headers)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(first.json(), second.json())
        validate("AttachmentReplacementResponse", first.json())
        self.assertEqual(len(self.core.store.list("snapshots")), before + 1)
        self.assertEqual(len(self.core.store.list("snapshot_replacements")), 1)
        self.assertEqual(self.upload(f, admin, headers, basis="不同授权依据").status_code, 409)
        self.assertEqual(self.upload(f, admin, headers, body=b"changed valid text").status_code, 409)

    def test_empty_basis_oversized_and_fake_files_are_rejected_without_writes(self):
        f = self.fixture()
        admin, _, headers = self.auth()
        before = len(self.core.store.list("snapshots"))
        for kwargs in (
            {"basis": " "},
            {"basis": "x" * 2001},
            {"body": b""},
            {"filename": "fake.pdf", "mime": "application/pdf", "body": b"%PDF-not-a-real-file"},
            {"filename": "fake.docx", "mime": "application/octet-stream", "body": b"PK\x03\x04fake"},
            {"body": CAPTCHA},
            {"filename": "fake.txt", "mime": "application/pdf"},
        ):
            response = self.upload(f, admin, {**headers, "Idempotency-Key": uid()}, **kwargs)
            self.assertEqual(response.status_code, 422, response.text)
        oversized = self.upload(f, admin, {**headers, "Idempotency-Key": uid()}, body=b"x" * (8 * 1024 * 1024 + 1))
        self.assertEqual(oversized.status_code, 413, oversized.text)
        self.assertEqual(len(self.core.store.list("snapshots")), before)
        self.assertEqual(self.core.store.list("snapshot_replacements"), [])

    def test_private_unverified_and_credential_sources_are_rejected(self):
        f = self.fixture()
        admin, _, headers = self.auth()
        for origin in ("http://127.0.0.1/private", "https://example.com/private?token=SYNTHETIC_SECRET"):
            source = {**self.source, "origin_url": origin}
            self.core.store.put("sources", source["id"], source)
            self.assertEqual(self.upload(f, admin, {**headers, "Idempotency-Key": uid()}).status_code, 422)
        self.core.store.put("sources", self.source["id"], {**self.source, "verification": "unverified"})
        self.assertEqual(self.upload(f, admin, {**headers, "Idempotency-Key": uid()}).status_code, 403)

    def test_unknown_snapshot_and_root_snapshot_cannot_be_replaced(self):
        f = self.fixture()
        admin, _, headers = self.auth()
        unknown = copy.deepcopy(f)
        unknown["child"]["snapshot_id"] = uid()
        self.assertEqual(self.upload(unknown, admin, headers).status_code, 404)
        root = copy.deepcopy(f)
        root["child"] = f["parent"]
        self.assertEqual(self.upload(root, admin, {**headers, "Idempotency-Key": uid()}).status_code, 422)

    def test_replacement_is_immutable_audited_pending_and_only_new_approval_changes_quality(self):
        f = self.fixture()
        admin, user, headers = self.auth()
        old = copy.deepcopy(f["event"])
        old_bytes = (self.core.data_dir / f["child"]["object_key"]).read_bytes()
        response = self.upload(f, admin, headers, basis="合成授权依据仅供管理员审计")
        self.assertEqual(response.status_code, 201, response.text)
        result = response.json()
        new = self.core.store.get("snapshots", result["snapshot_id"])
        candidate = self.core.store.get("candidates", result["candidate_id"])
        self.assertEqual(new["content_kind"], "text")
        self.assertEqual(new["source_url"], f["child"]["source_url"])
        self.assertEqual(new["capture"]["method"], "operator_upload")
        self.assertFalse(new["capture"]["original_verified"])
        self.assertNotIn("authorization_basis", new["capture"])
        self.assertNotIn(user["id"], str(new["capture"]))
        self.assertEqual(candidate["review_status"], "pending")
        self.assertTrue(candidate["review_required"])
        self.assertIn("operator_upload_requires_review", candidate["warnings"])
        self.assertIsNone(self.core.pipeline.get_accepted_candidate(candidate["candidate_id"], candidate["revision"]))
        with self.assertRaises(APIError):
            self.core.events.reconcile_candidate(candidate["candidate_id"], candidate["revision"])
        self.assertEqual(self.core.events.get_public(old["id"]), old)
        self.assertFalse(self.quality(old["id"])["complete"])
        self.assertEqual(self.client.get(f"/api/v1/snapshots/{new['snapshot_id']}").status_code, 404)
        audit = self.core.store.list("snapshot_replacements")[0]
        self.assertEqual(audit["actor_id"], user["id"])
        self.assertEqual(audit["authorization_basis"], "合成授权依据仅供管理员审计")
        accepted = admin.post(
            f"/api/v1/admin/reviews/{candidate['candidate_id']}/decision",
            json={
                "expected_candidate_revision": candidate["revision"],
                "decision": "accept",
                "reason": "合成授权文件逐字段核对",
                "replacement_candidate": None,
            },
            headers=headers,
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        event = self.core.events.reconcile_candidate(candidate["candidate_id"], accepted.json()["revision"])
        self.assertGreater(event["version"], old["version"])
        self.assertEqual(self.core.store.get("event_versions", f"{old['id']}:{old['version']}"), old)
        quality = self.quality(event["id"])
        self.assertTrue(quality["complete"], quality)
        self.assertIn(new["snapshot_id"], str(quality))
        self.assertNotIn(f["child"]["snapshot_id"], str(quality))
        self.assertNotIn("合成授权依据仅供管理员审计", str(quality))
        self.assertEqual((self.core.data_dir / f["child"]["object_key"]).read_bytes(), old_bytes)
        self.assertEqual(self.client.get(f"/api/v1/snapshots/{new['snapshot_id']}").status_code, 200)
        self.core.events.publication(
            event["id"],
            {"expected_event_version": event["version"], "publication_state": "withdrawn", "reason": "合成撤回"},
            user["id"],
        )
        self.quality(event["id"], 404)
        self.assertEqual(self.client.get(f"/api/v1/snapshots/{new['snapshot_id']}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/snapshots/{f['child']['snapshot_id']}").status_code, 404)

    def test_upload_and_review_do_not_republish_a_withdrawn_event(self):
        f = self.fixture()
        self.core.events.publication(
            f["event"]["id"],
            {"expected_event_version": f["event"]["version"], "publication_state": "withdrawn", "reason": "合成撤回"},
            uid(),
        )
        response = self.upload(f)
        self.assertEqual(response.status_code, 201, response.text)
        candidate = self.core.store.get("candidates", response.json()["candidate_id"])
        event = self.approve(candidate)
        self.assertEqual(event["publication_state"], "withdrawn")
        self.quality(event["id"], 404)

    def test_validation_and_transaction_leave_event_loop_responsive(self):
        f = self.fixture()
        calls = []
        parsing_threads = []
        parse = self.core.pipeline.parse_document

        async def responsive_pool(function, *args, **kwargs):
            loop = asyncio.get_running_loop()
            loop_thread = threading.get_ident()
            heartbeat = threading.Event()

            def work():
                worker = threading.get_ident()
                calls.append((function.__name__, loop_thread, worker))
                # This callback must run while synchronous work is waiting.
                loop.call_soon_threadsafe(heartbeat.set)
                self.assertTrue(heartbeat.wait(2), "upload blocked the event loop")
                return function(*args, **kwargs)

            return await run_in_threadpool(work)

        def record_parse(payload):
            parsing_threads.append(threading.get_ident())
            return parse(payload)

        with (
            patch("server.campus.authorized_upload.run_in_threadpool", side_effect=responsive_pool),
            patch.object(self.core.pipeline, "parse_document", side_effect=record_parse),
        ):
            response = self.upload(f)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual([name for name, _, _ in calls], ["validate_file", "idempotent"])
        self.assertTrue(all(loop_thread != worker for _, loop_thread, worker in calls))
        self.assertTrue(parsing_threads)
        self.assertEqual(set(parsing_threads), {calls[-1][2]})


class AuthorizedFileValidationTests(unittest.TestCase):
    def test_real_pdf_docx_xlsx_and_utf8_text_are_accepted(self):
        from pypdf import PdfWriter

        buffer = io.BytesIO()
        pdf = PdfWriter()
        pdf.add_blank_page(100, 100)
        pdf.write(buffer)
        self.assertEqual(validate_file(buffer.getvalue(), "valid.pdf", "application/pdf")[0], "pdf")
        self.assertEqual(validate_file(office_file("docx"), "valid.docx", "application/octet-stream")[0], "docx")
        self.assertEqual(validate_file(office_file("xlsx"), "valid.xlsx", "application/octet-stream")[0], "xlsx")
        self.assertEqual(validate_file(REPLACEMENT_TEXT, "valid.txt", "text/plain")[0], "text")

    def test_office_container_spoof_and_expansion_are_rejected(self):
        for content in (b"<w:document/>", b"x" * 500000):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("word/document.xml", content)
            with self.assertRaises(APIError):
                validate_file(buffer.getvalue(), "fake.docx", "application/octet-stream")

    def test_deep_xml_and_excessive_nodes_are_rejected(self):
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        for body in (
            "<w:p>"
            + ("<w:smartTag>" * (MAX_XML_DEPTH + 1))
            + "<w:r><w:t>x</w:t></w:r>"
            + ("</w:smartTag>" * (MAX_XML_DEPTH + 1))
            + "</w:p>",
            "<w:p><w:r>" + ("<w:br/>" * MAX_XML_NODES) + "</w:r></w:p>",
        ):
            xml = f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'.encode()
            packed = office_with_parts("docx", {"word/document.xml": xml})
            with self.assertRaises(APIError) as error:
                validate_file(packed, "bounded.docx", "application/octet-stream")
            self.assertEqual(error.exception.code, "invalid_attachment_file")

    def test_node_limit_applies_to_the_entire_office_package(self):
        nodes = "<item/>" * (MAX_XML_NODES // 2 + 1)
        packed = office_with_parts(
            "docx",
            {
                "customXml/item1.xml": ("<root>" + nodes + "</root>").encode(),
                "customXml/item2.xml": ("<root>" + nodes + "</root>").encode(),
            },
        )
        with self.assertRaises(APIError):
            validate_file(packed, "bounded.docx", "application/octet-stream")

    def test_nested_text_box_text_is_bounded_without_rejecting_small_text_boxes(self):
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        for size, accepted in ((5, True), (260_000, False)):
            xml = (
                f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:drawing><w:txbxContent>'
                "<w:p><w:r><w:t>" + ("x" * size) + "</w:t></w:r></w:p>"
                "</w:txbxContent></w:drawing></w:r></w:p></w:body></w:document>"
            ).encode()
            packed = office_with_parts("docx", {"word/document.xml": xml})
            if accepted:
                self.assertEqual(validate_file(packed, "textbox.docx", "application/octet-stream")[0], "docx")
            else:
                with self.assertRaises(APIError):
                    validate_file(packed, "textbox.docx", "application/octet-stream")

    def test_utf16_dtd_is_rejected(self):
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xml = (
            f'<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE document [<!ENTITY example "x">]>'
            f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:t>&example;</w:t></w:r></w:p>'
            "</w:body></w:document>"
        ).encode("utf-16")
        packed = office_with_parts("docx", {"word/document.xml": xml})
        with self.assertRaises(APIError):
            validate_file(packed, "dtd.docx", "application/octet-stream")

    def test_duplicate_office_parts_are_rejected(self):
        buffer = io.BytesIO(office_file("xlsx"))
        with self.assertWarns(UserWarning), zipfile.ZipFile(buffer, "a") as archive:
            archive.writestr("xl/worksheets/sheet1.xml", b"<worksheet/>")
        with self.assertRaises(APIError):
            validate_file(buffer.getvalue(), "duplicate.xlsx", "application/octet-stream")


if __name__ == "__main__":
    unittest.main()

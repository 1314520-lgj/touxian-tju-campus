"""Cloud storage protocol and entrypoints; no credentials or real cloud resources."""

import hashlib
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from server.campus.blob_store import MAX_BLOB_BYTES, BlobError, GCSBlobStore, LocalBlobStore, blob_store, public_origin
from server.campus.cloud_run import cloud_settings, create_cloud_app, drain, main
from server.campus.platform import Core, Settings
from server.campus.schema import uid
from server.campus.sources import FetchError, FetchResult
from server.campus.tests.test_integration import prepare_notice
from server.campus.worker import Worker
from server.campus_app import create_app


class SDKError(Exception):
    pass


class Missing(SDKError):
    pass


class Exists(SDKError):
    pass


EXCEPTIONS = types.ModuleType("google.api_core.exceptions")
EXCEPTIONS.GoogleAPICallError = SDKError
EXCEPTIONS.NotFound = Missing
EXCEPTIONS.PreconditionFailed = Exists


class CloudBlobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.body = "合成学校资料".encode()
        self.digest = hashlib.sha256(self.body).hexdigest()
        self.key = f"snapshots/{uid()}/{self.digest}.bin"
        self.local = LocalBlobStore(self.tmp.name)
        self.blob = Mock(size=len(self.body), generation=37)
        self.blob.download_as_bytes.return_value = self.body
        self.bucket = Mock()
        self.bucket.blob.return_value = self.blob
        self.client = Mock()
        self.client.bucket.return_value = self.bucket
        self.cloud = GCSBlobStore("private-campus-test", self.client)
        self.sdk = patch.dict(sys.modules, {"google.api_core.exceptions": EXCEPTIONS})
        self.sdk.start()

    def tearDown(self):
        self.sdk.stop()
        self.tmp.cleanup()

    def test_local_immutable_paths_and_hash(self):
        self.local.put(self.key, self.body, self.digest)
        self.local.put(self.key, self.body, self.digest)
        self.assertEqual(self.local.read(self.key, self.digest), self.body)
        self.local.path(self.key).write_bytes(b"corrupt")
        with self.assertRaisesRegex(BlobError, "snapshot_storage_corrupt"):
            self.local.put(self.key, self.body, self.digest)

    def test_local_path_traversal_and_read_size_bound(self):
        for key in ["../secret", "/etc/passwd", self.key + "/../x"]:
            with self.assertRaises(BlobError):
                self.local.read(key, self.digest)
        target = self.local.path(self.key)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x" * (MAX_BLOB_BYTES + 1))
        with self.assertRaisesRegex(BlobError, "snapshot_too_large"):
            self.local.read(self.key, self.digest)

    def test_gcs_create_never_overwrites_and_existing_is_verified(self):
        self.cloud.put(self.key, self.body, self.digest)
        self.blob.upload_from_string.assert_called_once_with(
            self.body, content_type="application/octet-stream", if_generation_match=0, checksum="auto", timeout=30
        )
        self.blob.upload_from_string.side_effect = Exists()
        self.cloud.put(self.key, self.body, self.digest)
        self.blob.download_as_bytes.assert_called_once_with(
            start=0, end=MAX_BLOB_BYTES, if_generation_match=37, raw_download=True, checksum="auto", timeout=30
        )
        self.blob.download_as_bytes.return_value = b"wrong"
        with self.assertRaisesRegex(BlobError, "snapshot_storage_corrupt"):
            self.cloud.put(self.key, self.body, self.digest)

    def test_gcs_read_rejects_size_before_download_and_pins_generation(self):
        self.blob.size = MAX_BLOB_BYTES + 1
        with self.assertRaisesRegex(BlobError, "snapshot_too_large"):
            self.cloud.read(self.key, self.digest)
        self.blob.download_as_bytes.assert_not_called()
        self.blob.size = 1
        self.blob.download_as_bytes.return_value = b"x" * (MAX_BLOB_BYTES + 1)
        with self.assertRaisesRegex(BlobError, "snapshot_too_large"):
            self.cloud.read(self.key, self.digest)

    def test_gcs_missing_hash_and_response_no_public_redirect(self):
        self.blob.reload.side_effect = Missing()
        with self.assertRaisesRegex(BlobError, "snapshot_file_missing"):
            self.cloud.read(self.key, self.digest)
        self.blob.reload.side_effect = None
        snapshot = {"object_key": self.key, "content_hash": self.digest, "snapshot_id": uid()}
        response = self.cloud.response(snapshot, {"Cache-Control": "no-store"})
        self.assertEqual(response.body, self.body)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertNotIn("location", response.headers)

    def test_gcs_network_error_is_sanitized(self):
        self.blob.reload.side_effect = SDKError("private secret and provider detail")
        with self.assertRaisesRegex(BlobError, "^snapshot_storage_unavailable$"):
            self.cloud.read(self.key, self.digest)
        self.blob.upload_from_string.side_effect = SDKError("private secret and provider detail")
        with self.assertRaisesRegex(BlobError, "^snapshot_storage_unavailable$"):
            self.cloud.put(self.key, self.body, self.digest)

    def test_local_default_does_not_load_cloud_sdk(self):
        with patch.dict(os.environ, {"CAMPUS_BLOB_STORE": "local"}):
            core = SimpleNamespace(data_dir=Path(self.tmp.name))
            self.assertIsInstance(blob_store(core), LocalBlobStore)
            self.assertIs(blob_store(core), blob_store(core))


class CloudBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "CAMPUS_BLOB_STORE": "local",
                "CAMPUS_MEILI_URL": "",
                "CAMPUS_PUBLIC_ORIGIN": "https://campus-123.asia-east2.run.app",
                "CAMPUS_DEMO": "false",
            },
        )
        self.env.start()
        self.app = create_app(
            Settings(
                data_dir=Path(self.tmp.name), secret="cloud-test" * 6, dev_auth=True, admin_emails={"admin@example.com"}
            ),
            start_worker=False,
        )
        self.core = self.app.state.core
        self.clients = []
        self.fixture = prepare_notice(self.core)

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.core.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def auth(self, email):
        client = TestClient(self.app)
        self.clients.append(client)
        challenge = client.post("/api/v1/auth/challenges", json={"email": email})
        response = client.post(
            "/api/v1/auth/sessions",
            json={"challenge_id": challenge.json()["challenge_id"], "code": challenge.headers["X-Dev-Login-Code"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return client, {"X-CSRF-Token": response.json()["csrf_token"]}

    def test_admin_links_use_canonical_https_and_wrong_actor_is_denied_before_io(self):
        admin, headers = self.auth("admin@example.com")
        student, _ = self.auth("student@example.com")
        sid = self.fixture["snapshot"]["snapshot_id"]
        response = admin.get(f"/api/v1/admin/snapshots/{sid}")
        self.assertEqual(response.status_code, 200, response.text)
        url = response.json()["signed_url"]
        self.assertTrue(url.startswith(os.environ["CAMPUS_PUBLIC_ORIGIN"]))
        file_path = url.split(".run.app", 1)[1]
        blobs = Mock()
        blobs.response.side_effect = lambda snapshot, headers: LocalBlobStore(self.tmp.name).response(snapshot, headers)
        self.core._snapshot_blobs = blobs
        self.assertIn(student.get(file_path).status_code, {403, 404})
        blobs.response.assert_not_called()
        self.assertEqual(admin.get(file_path).status_code, 200)
        self.assertEqual(blobs.response.call_count, 1)
        self.assertEqual(admin.get(file_path.replace("read_token=", "read_token=invalid")).status_code, 403)
        self.assertEqual(blobs.response.call_count, 1)

    def test_public_permission_and_withdrawal_gate_storage(self):
        admin, headers = self.auth("admin@example.com")
        student, _ = self.auth("student@example.com")
        sid = self.fixture["snapshot"]["snapshot_id"]
        candidate = self.fixture["candidate"]
        blobs = Mock()
        blobs.response.side_effect = lambda snapshot, headers: LocalBlobStore(self.tmp.name).response(snapshot, headers)
        self.core._snapshot_blobs = blobs
        self.assertEqual(student.get(f"/api/v1/snapshots/{sid}").status_code, 404)
        self.assertEqual(student.get(f"/api/v1/snapshots/{sid}/file?read_token=anything").status_code, 404)
        blobs.response.assert_not_called()
        accepted = admin.post(
            f"/api/v1/admin/reviews/{candidate['candidate_id']}/decision",
            json={
                "expected_candidate_revision": candidate["revision"],
                "decision": "accept",
                "reason": "合成审核",
                "replacement_candidate": None,
            },
            headers=headers,
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        event = self.core.events.reconcile_candidate(candidate["candidate_id"], accepted.json()["revision"])
        signed = student.get(f"/api/v1/snapshots/{sid}").json()["signed_url"]
        self.assertTrue(signed.startswith(os.environ["CAMPUS_PUBLIC_ORIGIN"]))
        path = signed.split(".run.app", 1)[1]
        self.assertEqual(student.get(path).status_code, 200)
        event["publication_state"] = "withdrawn"
        self.core.store.put("events", event["id"], event)
        self.assertEqual(student.get(path).status_code, 404)
        self.assertEqual(blobs.response.call_count, 1)

    def test_storage_failure_updates_source_health_and_preserves_fetch_run(self):
        source = self.fixture["source"]
        sources = self.core.pipeline.sources
        listing = FetchResult(source["origin_url"], b'<a href="/new/123">new notice</a>', "text/html")
        article = FetchResult("https://example.com/new/123", b"<h1>new</h1><article>new</article>", "text/html")
        blobs = Mock()
        blobs.put.side_effect = BlobError("snapshot_storage_unavailable")
        self.core._snapshot_blobs = blobs
        with (
            patch.object(sources, "config", return_value={"article_url_pattern": r"/new/[0-9]+"}),
            patch.object(sources.fetcher, "fetch", side_effect=[listing, article]),
        ):
            with self.assertRaisesRegex(FetchError, "snapshot_storage_unavailable"):
                sources.fetch_source({"source_id": source["id"]})
        current = self.core.store.get("sources", source["id"])
        self.assertEqual(current["health"], "failed")
        self.assertEqual(current["last_error_code"], "snapshot_storage_unavailable")
        run = self.core.store.list("fetch_runs")[-1]
        self.assertEqual(run["snapshot_ids"], [])
        self.assertEqual(run["error_codes"], ["snapshot_storage_unavailable"])

    def test_multiple_runtimes_share_immutable_snapshots_and_parse(self):
        other = create_app(Settings(data_dir=Path(self.tmp.name), secret="cloud-test" * 6), start_worker=False)
        try:
            sid = self.fixture["snapshot"]["snapshot_id"]
            self.assertEqual(other.state.core.pipeline.parse_document({"snapshot_id": sid})["snapshot_id"], sid)
            snapshot = other.state.core.store.get("snapshots", sid)
            self.assertEqual(
                blob_store(other.state.core).read(snapshot["object_key"], snapshot["content_hash"]),
                blob_store(self.core).read(snapshot["object_key"], snapshot["content_hash"]),
            )
        finally:
            other.state.core.store.close()


class CloudWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.core = Core(Settings(data_dir=Path(self.tmp.name), secret="s" * 48))

    def tearDown(self):
        self.core.store.close()
        self.tmp.cleanup()

    def test_drain_follows_chained_outbox_in_real_sqlite(self):
        sid, source, candidate, event = uid(), uid(), uid(), uid()
        calls = []

        def parse(payload):
            calls.append("parse")
            self.core.store.emit("content.parsed", sid, 1, snapshot_id=sid, source_id=source)

        def extract(payload):
            calls.append("extract")
            self.core.store.emit("candidate.accepted", candidate, 1, candidate_id=candidate, candidate_revision=1)

        def reconcile(payload):
            calls.append("reconcile")
            self.core.store.emit(
                "public_event.changed", event, 1, subject={"kind": "public_event", "id": event, "revision": 1}
            )

        self.core.handlers = {
            "parse_document": parse,
            "extract_candidates": extract,
            "reconcile_event": reconcile,
            "index_event": lambda p: calls.append("index"),
            "plan_reminders": lambda p: calls.append("plan"),
        }
        self.core.store.emit("source.snapshot.created", sid, 1, snapshot_id=sid, source_id=source)
        result = drain(Worker(self.core), max_jobs=10)
        self.assertEqual(result["succeeded"], 5)
        self.assertEqual(set(calls), {"parse", "extract", "reconcile", "index", "plan"})
        self.assertEqual(self.core.store.pending_outbox(), [])
        second = Core(Settings(data_dir=Path(self.tmp.name), secret="s" * 48))
        try:
            second.handlers = self.core.handlers
            self.assertEqual(drain(Worker(second))["processed"], 0)
        finally:
            second.store.close()

    def test_drain_job_round_time_and_stop_budgets(self):
        self.core.handlers = {"parse_document": lambda p: None}
        for i in range(6):
            self.core.store.enqueue("parse_document", {"snapshot_id": uid()}, f"cloud-bound:{i}")
        worker = Worker(self.core)
        self.assertEqual(drain(worker, max_jobs=2)["processed"], 2)
        self.assertEqual(drain(worker, max_rounds=1)["processed"], 1)
        self.assertEqual(drain(worker, max_seconds=1, monotonic=Mock(side_effect=[0, 2]))["processed"], 0)
        stop = threading.Event()
        stop.set()
        self.assertEqual(drain(worker, stop=stop)["rounds"], 0)
        self.core.handlers["parse_document"] = lambda p: stop.set()
        stop.clear()
        self.assertEqual(drain(worker, stop=stop)["processed"], 1)
        with self.assertRaises(ValueError):
            drain(worker, max_seconds=0)


class CloudConfigurationTests(unittest.TestCase):
    def test_canonical_origin_rejects_credentials_paths_queries_and_http(self):
        for value in [
            "http://campus.run.app",
            "https://x.run.app/path",
            "https://user:pass@x.run.app",
            "https://x.run.app?q=x",
            "https://x.run.app#x",
            "https://x.run.app:8000",
            "https://x.run.app\\x",
        ]:
            with self.subTest(value=value), patch.dict(os.environ, {"CAMPUS_PUBLIC_ORIGIN": value}):
                with self.assertRaises(ValueError):
                    public_origin()
        with patch.dict(os.environ, {"CAMPUS_PUBLIC_ORIGIN": "https://x.run.app/"}):
            self.assertEqual(public_origin(), "https://x.run.app")

    def test_cloud_configuration_validates_before_database_and_service_disables_worker(self):
        env = {
            "CAMPUS_ENV": "production",
            "CAMPUS_DATABASE_URL": "postgresql+psycopg://u:p@/campus?host=/cloudsql/p:r:i",
            "CAMPUS_SECRET": "s" * 48,
            "CAMPUS_ALLOWED_ORIGINS": "https://x.run.app",
            "CAMPUS_PUBLIC_ORIGIN": "https://x.run.app",
            "CAMPUS_BLOB_STORE": "gcs",
            "CAMPUS_GCS_BUCKET": "private-test",
            "CAMPUS_DEV_AUTH": "false",
        }
        with patch.dict(os.environ, env):
            self.assertTrue(cloud_settings().production)
            with patch("server.campus_app.create_app") as create:
                create_cloud_app()
                self.assertFalse(create.call_args.kwargs["start_worker"])
            with patch.dict(os.environ, {"CAMPUS_ALLOWED_ORIGINS": "https://other.run.app"}):
                with self.assertRaises(ValueError):
                    cloud_settings()
            with patch.dict(os.environ, {"CAMPUS_ENV": "development"}):
                with self.assertRaises(ValueError):
                    cloud_settings()

    def test_cloud_server_binds_injected_port_without_trusting_forwarded_headers(self):
        with (
            patch("server.campus.cloud_run.cloud_settings"),
            patch("uvicorn.run") as run,
            patch.dict(os.environ, {"PORT": "8087"}),
        ):
            self.assertEqual(main(["serve"]), 0)
            self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")
            self.assertEqual(run.call_args.kwargs["port"], 8087)
            self.assertFalse(run.call_args.kwargs["proxy_headers"])


if __name__ == "__main__":
    unittest.main()

"""Regression proofs for invalid transport, feed failures and demo isolation."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from server.campus.demo import seed_demo
from server.campus.platform import Settings
from server.campus.schema import validate
from server.campus.sources import FetchError, FetchResult, feed_items
from server.campus_app import create_app


class BoundaryAuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(
            Settings(
                data_dir=Path(self.directory.name), secret="t" * 48, dev_auth=True, admin_emails={"admin@example.com"}
            ),
            start_worker=False,
        )
        self.core = self.app.state.core
        self.client = TestClient(self.app, raise_server_exceptions=False)
        challenge = self.client.post("/api/v1/auth/challenges", json={"email": "admin@example.com"})
        login = self.client.post(
            "/api/v1/auth/sessions",
            json={"challenge_id": challenge.json()["challenge_id"], "code": challenge.headers["X-Dev-Login-Code"]},
        )
        self.headers = {"X-CSRF-Token": login.json()["csrf_token"], "Content-Type": "application/json"}

    def tearDown(self):
        self.client.close()
        self.core.store.close()
        self.directory.cleanup()

    def test_invalid_json_and_encoding_are_client_errors_without_echoing_content(self):
        for body in (b"{PRIVATE-SYNTHETIC", b"\xff"):
            response = self.client.post("/api/v1/admin/sources", content=body, headers=self.headers)
            self.assertEqual(response.status_code, 400, response.text)
            validate("Error", response.json())
            self.assertEqual(response.headers["X-Request-ID"], response.json()["request_id"])
            self.assertNotIn("PRIVATE-SYNTHETIC", response.text)

    def test_stream_limit_error_preserves_error_contract_and_cors(self):
        response = self.client.post(
            "/api/v1/me/items", content=b"x" * 262145, headers={**self.headers, "Origin": "http://localhost:4173"}
        )
        self.assertEqual(response.status_code, 413, response.text)
        validate("Error", response.json())
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:4173")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_invalid_authorized_feed_shape_is_controlled(self):
        for body in (b"null", b"123", b'"wrong"', b'{"items":null}', b"{broken"):
            with self.assertRaisesRegex(FetchError, "invalid_feed"):
                feed_items(FetchResult("https://example.org/feed", body, "application/json"), "authorized_api", {})
        items = feed_items(
            FetchResult(
                "https://example.org/feed",
                b'{"items":[{"url":"https://example.org/one","published_at":123}]}',
                "application/json",
            ),
            "authorized_api",
            {},
        )
        self.assertIsNone(items[0]["published_at"])

    def test_invalid_feed_records_failed_health_and_fetch_run(self):
        self.core.pipeline.sources.seed()
        source = self.core.store.list("sources")[0]
        source["adapter"] = "authorized_api"
        source["health"] = "healthy"
        self.core.store.put("sources", source["id"], source)
        self.core.pipeline.sources.fetcher = SimpleNamespace(
            fetch=lambda *args: FetchResult(source["origin_url"], b"null", "application/json")
        )
        with self.assertRaisesRegex(FetchError, "invalid_feed"):
            self.core.pipeline.sources.fetch_source({"source_id": source["id"]})
        self.assertEqual(self.core.store.get("sources", source["id"])["health"], "failed")
        self.assertIn("invalid_feed", self.core.store.list("fetch_runs")[0]["error_codes"])

    def test_bad_article_pattern_is_controlled(self):
        result = FetchResult("https://example.org/list", b'<a href="/info/1/2.htm">test</a>', "text/html")
        with self.assertRaisesRegex(FetchError, "invalid_article_pattern"):
            feed_items(result, "website", {"article_url_pattern": "["})

    def test_demo_sources_are_editable_schema_and_do_not_fetch_real_sources(self):
        seed_demo(self.core)
        self.assertEqual(len(self.core.store.list("events")), 3)
        for source in self.core.store.list("sources"):
            validate("AdminSource", source)
            self.assertFalse(source["enabled"])

    def test_demo_refuses_external_database_before_writing(self):
        core = SimpleNamespace(
            settings=SimpleNamespace(production=False, database_url="postgresql://example.invalid/database")
        )
        with self.assertRaisesRegex(ValueError, "SQLite"):
            seed_demo(core)


if __name__ == "__main__":
    unittest.main()

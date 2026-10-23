"""Hosted single-port launcher contracts; no real deployment, no SMTP."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from serve_cloud import (
    DEMO_MODE,
    LIVE_MODE,
    OPERATOR_MODE,
    apply_cloud_env,
    build_application,
    cloud_env,
    same_origin,
    serve_address,
    should_register_sources,
    should_seed_demo,
)

ROOT = Path(__file__).resolve().parents[3]


class CloudEnvTests(unittest.TestCase):
    def test_demo_defaults_are_explicit(self):
        defaults = cloud_env({"CAMPUS_CLOUD_MODE": DEMO_MODE})
        self.assertEqual(defaults["CAMPUS_DEV_AUTH"], "true")
        self.assertEqual(defaults["CAMPUS_EMBEDDED_WORKER"], "false")
        self.assertEqual(defaults["CAMPUS_PUBLIC_API_DOCS"], "false")
        self.assertEqual(defaults["CAMPUS_DATA_DIR"], str(ROOT / "data"))
        self.assertIn("touxian.local", defaults["CAMPUS_ADMIN_EMAILS"])

    def test_live_defaults_keep_the_public_guard(self):
        defaults = cloud_env({"CAMPUS_CLOUD_MODE": LIVE_MODE})
        self.assertEqual(defaults["CAMPUS_DEV_AUTH"], "false")
        self.assertEqual(defaults["CAMPUS_EMBEDDED_WORKER"], "true")
        self.assertNotIn("CAMPUS_DEMO", defaults)
        self.assertNotIn("CAMPUS_ADMIN_EMAILS", defaults)

    def test_operator_defaults_run_the_worker_without_demo_data(self):
        defaults = cloud_env({"CAMPUS_CLOUD_MODE": OPERATOR_MODE})
        # A loopback operations instance has no SMTP either, so the development
        # login code is the only way in; the worker must run so relay feeds
        # actually collect, and nothing synthetic may ever be seeded.
        self.assertEqual(defaults["CAMPUS_DEV_AUTH"], "true")
        self.assertEqual(defaults["CAMPUS_EMBEDDED_WORKER"], "true")
        self.assertNotIn("CAMPUS_DEMO", defaults)
        self.assertIn("touxian.local", defaults["CAMPUS_ADMIN_EMAILS"])

    def test_operator_binds_loopback_unless_published(self):
        self.assertEqual(serve_address({"CAMPUS_CLOUD_MODE": OPERATOR_MODE, "PORT": "8000"}), ("127.0.0.1", 8000))
        self.assertEqual(serve_address({"CAMPUS_CLOUD_MODE": DEMO_MODE, "PORT": "8000"}), ("0.0.0.0", 8000))
        self.assertEqual(
            serve_address({"CAMPUS_CLOUD_MODE": OPERATOR_MODE, "CAMPUS_OPERATOR_HOST": "0.0.0.0"}), ("0.0.0.0", 3000)
        )

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            cloud_env({"CAMPUS_CLOUD_MODE": "staging"})

    def test_explicit_environment_wins_in_demo_mode(self):
        with patch.dict(os.environ, {"CAMPUS_CLOUD_MODE": DEMO_MODE, "CAMPUS_DEV_AUTH": "false"}):
            applied = apply_cloud_env()
            self.assertNotIn("CAMPUS_DEV_AUTH", applied)
            self.assertEqual(os.environ["CAMPUS_DEV_AUTH"], "false")

    def test_live_mode_overrides_an_inherited_development_code(self):
        with patch.dict(os.environ, {"CAMPUS_CLOUD_MODE": LIVE_MODE, "CAMPUS_DEV_AUTH": "true"}):
            apply_cloud_env()
            self.assertEqual(os.environ["CAMPUS_DEV_AUTH"], "false")

    def test_public_origin_joins_allowed_origins_without_duplicates(self):
        env = {"CAMPUS_CLOUD_MODE": LIVE_MODE, "CAMPUS_PUBLIC_ORIGIN": "https://touxian.example.run/"}
        defaults = cloud_env(env)
        self.assertIn("https://touxian.example.run", defaults["CAMPUS_ALLOWED_ORIGINS"])
        again = cloud_env({**env, "CAMPUS_ALLOWED_ORIGINS": "https://touxian.example.run"})
        self.assertEqual(again["CAMPUS_ALLOWED_ORIGINS"].count("https://touxian.example.run"), 1)

    def test_serve_address_follows_port(self):
        self.assertEqual(serve_address({}), ("0.0.0.0", 3000))
        self.assertEqual(serve_address({"PORT": "8080"}), ("0.0.0.0", 8080))


def fake_core(events, sources):
    store = SimpleNamespace(list=lambda kind: events if kind == "events" else sources)
    return SimpleNamespace(store=store, pipeline=SimpleNamespace(sources=SimpleNamespace(seed=lambda: None)))


class GuardTests(unittest.TestCase):
    def test_demo_seed_only_touches_an_empty_store(self):
        self.assertTrue(should_seed_demo(fake_core([], [])))
        self.assertFalse(should_seed_demo(fake_core([{"id": "kept"}], [])))

    def test_sources_register_once(self):
        self.assertTrue(should_register_sources(fake_core([], [])))
        self.assertFalse(should_register_sources(fake_core([], [{"id": "kept"}])))


class SameOriginTests(unittest.TestCase):
    def test_same_origin_matches_host_across_schemes_and_ports(self):
        self.assertTrue(same_origin("testserver", "http://testserver"))
        self.assertTrue(same_origin("testserver", "https://testserver"))
        self.assertTrue(same_origin("campus.example.run:8443", "https://campus.example.run:8443"))
        self.assertTrue(same_origin("testserver", "https://testserver/"))

    def test_foreign_origin_never_matches(self):
        self.assertFalse(same_origin("testserver", "https://evil.example"))
        self.assertFalse(same_origin("evil.example", "https://testserver"))
        self.assertFalse(same_origin("testserver", "https://testserver.evil.example"))


class SameOriginWriteTests(unittest.TestCase):
    def test_host_own_origin_writes_pass_and_foreign_origins_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "CAMPUS_CLOUD_MODE": DEMO_MODE,
                    "CAMPUS_DATA_DIR": tmp,
                    "CAMPUS_ENV": "development",
                    "CAMPUS_DEV_AUTH": "true",
                    "CAMPUS_EMBEDDED_WORKER": "false",
                },
            ):
                _, application = build_application()
                with TestClient(application) as client:
                    own = {"Origin": "http://testserver"}
                    challenge = client.post(
                        "/api/v1/auth/challenges",
                        json={"email": "same-origin@touxian.local"},
                        headers=own,
                    )
                    self.assertEqual(challenge.status_code, 201)
                    code = challenge.headers["X-Dev-Login-Code"]
                    session = client.post(
                        "/api/v1/auth/sessions",
                        json={"challenge_id": challenge.json()["challenge_id"], "code": code},
                        headers=own,
                    )
                    self.assertEqual(session.status_code, 200)

                    foreign = {"Origin": "https://evil.example"}
                    forged = client.post(
                        "/api/v1/auth/challenges",
                        json={"email": "foreign-origin@touxian.local"},
                        headers=foreign,
                    )
                    self.assertEqual(forged.status_code, 403)
                    self.assertEqual(forged.json()["code"], "ORIGIN_FORBIDDEN")


class DemoBootTests(unittest.TestCase):
    def test_demo_boot_serves_labeled_events_and_hides_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = patch.dict(
                os.environ,
                {
                    "CAMPUS_CLOUD_MODE": DEMO_MODE,
                    "CAMPUS_DATA_DIR": tmp,
                    "CAMPUS_ENV": "development",
                    "CAMPUS_DEV_AUTH": "true",
                    "CAMPUS_EMBEDDED_WORKER": "false",
                    "CAMPUS_PUBLIC_API_DOCS": "false",
                    "CAMPUS_SOURCE_REFRESH_SECONDS": "21600",
                },
            )
            with environment:
                _, application = build_application()
                with TestClient(application) as client:
                    ready = client.get("/api/v1/health/ready")
                    self.assertEqual(ready.status_code, 200)
                    events = client.get("/api/v1/events")
                    self.assertEqual(events.status_code, 200)
                    body = events.json()
                    self.assertTrue(body["items"])
                    self.assertTrue(all("【演示】" in item["title"] for item in body["items"]))
                    docs = client.get("/openapi.json")
                    self.assertEqual(docs.status_code, 404)

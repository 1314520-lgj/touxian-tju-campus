"""Preflight remains read-only and reports deployment failures without secrets."""

import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("cloud_preflight", ROOT / "deploy/cloudrun/preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class PreflightTests(unittest.TestCase):
    def test_local_checks_pass_without_database_or_network_access(self):
        with patch.object(preflight, "build_opener", side_effect=AssertionError("network")):
            self.assertTrue(all(value["ok"] for value in preflight.local_checks()))

    def test_database_diagnostics_do_not_expose_connection_secrets(self):
        with patch(
            "sqlalchemy.create_engine", side_effect=RuntimeError("secret_user:secret_password@unavailable.invalid")
        ):
            value = preflight.database_checks(
                "postgresql+psycopg://secret_user:secret_password@unavailable.invalid/campus"
            )
        serialized = json.dumps(value)
        self.assertFalse(value[0]["ok"])
        self.assertNotIn("secret_user", serialized)
        self.assertNotIn("secret_password", serialized)
        self.assertNotIn("unavailable.invalid", serialized)

    def test_database_rejects_sqlite_instead_of_initializing_schema(self):
        self.assertFalse(preflight.database_checks("sqlite:///:memory:")[0]["ok"])

    def test_service_requires_exact_https_cloud_run_origin(self):
        for origin in [
            "http://x.run.app",
            "https://x.run.app/path",
            "https://x.run.app?q=1",
            "https://user:pass@x.run.app",
            "https://x.run.app:bad",
            "https://x.run.app:8000",
            "https://x.run.app\\evil",
            "https://example.com",
        ]:
            with self.subTest(origin=origin):
                self.assertFalse(preflight.service_checks(origin)[0]["ok"])

    def test_service_checks_production_shape_with_bounded_read_and_token(self):
        responses = [{"status": "alive"}, {"status": "ready", "demo_data": False, "login_delivery": "smtp_configured"}]
        calls = []

        class Response:
            def __init__(self, body):
                self.body = json.dumps(body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, count):
                self.assert_count = count
                return self.body

        class Opener:
            def open(self, request, timeout):
                calls.append((request, timeout))
                return Response(responses[len(calls) - 1])

        with patch.object(preflight, "build_opener", return_value=Opener()):
            self.assertTrue(
                all(value["ok"] for value in preflight.service_checks("https://x.run.app", "private-token"))
            )
        self.assertEqual(calls[0][0].get_header("Authorization"), "Bearer private-token")
        self.assertEqual(calls[1][0].full_url, "https://x.run.app/api/v1/health/ready")

    def test_readiness_fails_if_synthetic_demo_or_missing_smtp(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, count):
                return b'{"status":"ready","demo_data":true,"login_delivery":"dev"}'

        with patch.object(preflight, "build_opener", return_value=SimpleNamespace(open=lambda *a, **kw: Response())):
            self.assertFalse(preflight.service_checks("https://x.run.app")[1]["ok"])


if __name__ == "__main__":
    unittest.main()

"""Isolated launcher regressions; never read or modify the packaged real data."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import urlopen

from fastapi.testclient import TestClient

from server.campus.bootstrap import (
    BootstrapError,
    materialize_frontend,
    prepare_runtime,
)

SOURCE_ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("campus_public_launcher", SOURCE_ROOT / "app.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def built_frontend(root, name="dist"):
    directory = root / name
    (directory / "assets").mkdir(parents=True)
    (directory / "campus.html").write_text('<html><script src="/assets/campus.js"></script></html>', encoding="utf-8")
    (directory / "assets" / "campus.js").write_text("console.log('fixture');", encoding="utf-8")
    return directory


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="campus-bootstrap-test-")
        self.root = Path(self.temp.name)
        self.data = self.root / "operator-data"
        built_frontend(self.root)
        clean = {key: value for key, value in os.environ.items() if not key.startswith("CAMPUS_")}
        clean.update(CAMPUS_ENV="test", CAMPUS_DATA_DIR=str(self.data), CAMPUS_EMBEDDED_WORKER="false")
        self.environment = patch.dict(os.environ, clean, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        # Windows releases a force-killed child's file handles asynchronously: a
        # CLI stopped in this class can still hold `campus.sqlite3` for a few
        # milliseconds after `process.wait()` returns, and rmtree then dies with
        # WinError 32 (observed once in the round-11 suite run, in this class's
        # loopback case). Retry briefly so the race does not fail an unrelated
        # run; a lock that really persists still raises, so nothing is masked.
        for attempt in range(20):
            try:
                self.temp.cleanup()
                return
            except (PermissionError, OSError):
                if attempt == 19:
                    raise
                time.sleep(0.1)

    def application(self):
        return launcher.create_application(root=self.root, start_worker=False)

    def test_committed_wal_survives_abrupt_process_exit_and_real_bootstrap(self):
        self.data.mkdir()
        database = self.data / "campus.sqlite3"
        child = """import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("PRAGMA wal_autocheckpoint=0")
connection.execute("CREATE TABLE recovered(value TEXT)")
connection.commit()
connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
connection.execute("INSERT INTO recovered VALUES ('committed-before-crash')")
connection.commit()
os._exit(0)
"""
        subprocess.run([sys.executable, "-X", "utf8", "-c", child, str(database)], check=True, timeout=15)
        self.assertGreater(Path(str(database) + "-wal").stat().st_size, 0)
        application = self.application()
        try:
            with application.state.core.store.engine.connect() as connection:
                rows = connection.exec_driver_sql("SELECT value FROM recovered").fetchall()
            self.assertEqual(rows, [("committed-before-crash",)])
        finally:
            application.state.core.store.close()

    def test_session_and_signing_key_survive_restart_without_development_auth(self):
        first = self.application()
        with (
            TestClient(first) as client,
            patch("server.campus.platform.secrets.randbelow", return_value=42),
            patch("server.campus.platform.MailGateway.send_login_code", return_value={"status": "sent"}),
        ):
            challenge = client.post("/api/v1/auth/challenges", json={"email": "student@example.com"})
            self.assertEqual(challenge.status_code, 201)
            self.assertNotIn("X-Dev-Login-Code", challenge.headers)
            session = client.post(
                "/api/v1/auth/sessions", json={"challenge_id": challenge.json()["challenge_id"], "code": "000042"}
            )
            self.assertEqual(session.status_code, 200)
            cookie = client.cookies.get("touxian_session")
            expected_user = session.json()["user"]
            signature = first.state.core.digest("snapshot-signature-fixture")
        secret = (self.data / ".server-secret").read_bytes()
        second = self.application()
        with TestClient(second) as client:
            client.cookies.set("touxian_session", cookie)
            self.assertEqual(client.get("/api/v1/me").json()["user"], expected_user)
            self.assertEqual(second.state.core.digest("snapshot-signature-fixture"), signature)
        self.assertEqual((self.data / ".server-secret").read_bytes(), secret)

    def test_explicit_data_directory_is_honored_without_seed_copy(self):
        settings = prepare_runtime(self.root)
        self.assertEqual(settings.data_dir, self.data.resolve())
        self.assertFalse((self.root / "data").exists())
        self.assertFalse((self.data / "campus.sqlite3").exists())

    def test_default_data_directory_is_the_project_directory(self):
        os.environ.pop("CAMPUS_DATA_DIR")
        self.assertEqual(prepare_runtime(self.root).data_dir, (self.root / "data").resolve())

    def test_readonly_data_directory_is_rejected_without_fallback(self):
        self.data.mkdir()
        self.data.chmod(stat.S_IREAD | stat.S_IEXEC)
        try:
            with self.assertRaisesRegex(BootstrapError, "not writable"):
                prepare_runtime(self.root)
            self.assertEqual(os.environ["CAMPUS_DATA_DIR"], str(self.data))
            self.assertFalse((self.root / "data").exists())
        finally:
            self.data.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)

    def test_denied_write_probe_is_rejected(self):
        with patch("server.campus.bootstrap.tempfile.TemporaryFile", side_effect=PermissionError("denied")):
            with self.assertRaises(BootstrapError):
                prepare_runtime(self.root)
        self.assertFalse((self.root / "data").exists())

    def test_external_production_storage_and_runtime_configuration_are_preserved(self):
        values = {
            "CAMPUS_ENV": "production",
            "CAMPUS_SECRET": "explicit-test-secret-" * 3,
            "CAMPUS_DATABASE_URL": "postgresql+psycopg://test.invalid/campus",
            "CAMPUS_ALLOWED_ORIGINS": "https://campus.example.com",
            "CAMPUS_BLOB_STORE": "gcs",
            "CAMPUS_GCS_BUCKET": "campus-test-bucket",
            "CAMPUS_MEILI_URL": "https://search.example.com",
            "CAMPUS_MEILI_KEY": "test-meili-key",
            "CAMPUS_SMTP_HOST": "smtp.example.com",
            "CAMPUS_SMTP_PORT": "465",
            "CAMPUS_SMTP_SSL": "true",
            "CAMPUS_SMTP_FROM": "notice@example.com",
            "CAMPUS_TRUSTED_GATEWAY_ORIGINS": "https://approved-feed.example.com",
        }
        os.environ.update(values)
        settings = prepare_runtime(self.root)
        self.assertTrue(settings.production)
        self.assertEqual(settings.database_url, values["CAMPUS_DATABASE_URL"])
        self.assertEqual(settings.smtp_host, values["CAMPUS_SMTP_HOST"])
        self.assertEqual(settings.smtp_port, 465)
        for key, expected in values.items():
            self.assertEqual(os.environ[key], expected)

    def test_production_guards_remain_enabled(self):
        os.environ.update(CAMPUS_ENV="production", CAMPUS_SECRET="short")
        with self.assertRaisesRegex(ValueError, "CAMPUS_SECRET"):
            prepare_runtime(self.root)
        self.assertFalse(self.data.exists())

    def test_no_default_private_dns_gateway_exception(self):
        prepare_runtime(self.root)
        self.assertNotIn("CAMPUS_TRUSTED_GATEWAY_ORIGINS", os.environ)

    def test_corrupt_database_prevents_application_creation(self):
        self.data.mkdir()
        database = self.data / "campus.sqlite3"
        body = b"unrecoverable sqlite fixture"
        database.write_bytes(body)
        with self.assertRaisesRegex(BootstrapError, "could not be recovered"):
            self.application()
        self.assertEqual(database.read_bytes(), body)
        self.assertFalse((self.data / ".server-secret").exists())

    def test_corrupt_database_with_explicit_sqlite_driver_is_checked(self):
        self.data.mkdir()
        database = self.data / "custom.sqlite3"
        database.write_bytes(b"unrecoverable sqlite fixture")
        os.environ["CAMPUS_DATABASE_URL"] = "sqlite+pysqlite:///" + database.as_posix()
        with self.assertRaises(BootstrapError):
            self.application()
        self.assertFalse((self.data / ".server-secret").exists())

    def test_readonly_database_is_rejected_before_application_writes(self):
        self.data.mkdir()
        database = self.data / "campus.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE fixture(value TEXT)")
        connection.close()
        database.chmod(stat.S_IREAD)
        try:
            with self.assertRaisesRegex(BootstrapError, "read-only"):
                self.application()
            self.assertFalse((self.data / ".server-secret").exists())
        finally:
            database.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_asgi_auto_lifespan_rejects_corrupt_database_before_http_listens(self):
        self.data.mkdir()
        database = self.data / "campus.sqlite3"
        database.write_bytes(b"unrecoverable sqlite fixture")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=SOURCE_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Application startup failed", result.stderr)
        self.assertNotIn("Uvicorn running", result.stderr)
        self.assertFalse((self.data / ".server-secret").exists())

    def test_import_is_lazy_and_does_not_delete_files_or_start_collection(self):
        self.data.mkdir()
        markers = [self.data / name for name in ("campus.sqlite3-wal", "campus.sqlite3-shm", ".server-secret")]
        for marker in markers:
            marker.write_bytes(b"unchanged")
        child = """import app, threading
assert app.app.application is None
assert not any(thread.name == "campus-worker" for thread in threading.enumerate())
"""
        subprocess.run([sys.executable, "-X", "utf8", "-c", child], cwd=SOURCE_ROOT, check=True, timeout=15)
        self.assertEqual([marker.read_bytes() for marker in markers], [b"unchanged"] * 3)
        self.assertEqual(set(self.data.iterdir()), set(markers))

    def test_existing_complete_dist_requires_no_writes(self):
        with patch("server.campus.bootstrap.tempfile.mkdtemp", side_effect=AssertionError("unexpected write")):
            self.assertEqual(materialize_frontend(self.root), self.root / "dist")

    def test_materialization_replaces_incomplete_dist_without_merging_old_files(self):
        built_frontend(self.root, "web")
        (self.root / "dist" / "assets" / "campus.js").unlink()
        (self.root / "dist" / "old-canary.txt").write_text("must not be served")
        directory = materialize_frontend(self.root)
        self.assertTrue((directory / "assets" / "campus.js").is_file())
        self.assertFalse((directory / "old-canary.txt").exists())
        self.assertEqual(list(self.root.glob(".campus-dist-*")), [])

    def test_missing_frontend_build_is_rejected(self):
        (self.root / "dist" / "assets" / "campus.js").unlink()
        with self.assertRaisesRegex(BootstrapError, "Missing complete frontend"):
            prepare_runtime(self.root)

    def test_lazy_asgi_entrypoint_serves_without_starting_collector(self):
        application = launcher.LazyApplication()
        self.assertIsNone(application.application)
        with patch("server.campus.worker.Worker.run") as worker, TestClient(application) as client:
            self.assertEqual(client.get("/healthz").json(), {"status": "ok"})
            self.assertEqual(client.get("/").status_code, 200)
            self.assertFalse(application.application.state.core.settings.dev_auth)
            worker.assert_not_called()

    def test_delayed_collector_is_cancelled_by_application_shutdown(self):
        os.environ["CAMPUS_WORKER_DELAY_SECONDS"] = "3600"
        application = launcher.create_application(root=self.root, start_worker=True)
        with patch("server.campus.worker.Worker.run") as worker:
            with TestClient(application) as client:
                self.assertEqual(client.get("/healthz").status_code, 200)
                worker.assert_not_called()
            worker.assert_not_called()
            self.assertFalse(
                any(
                    thread.name == "campus-worker" and thread.is_alive()
                    for thread in __import__("threading").enumerate()
                )
            )

    def test_cli_starts_on_loopback_with_isolated_data_and_worker_disabled(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = dict(os.environ, PORT=str(port))
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(SOURCE_ROOT / "app.py")],
            cwd=SOURCE_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                self.assertIsNone(process.poll(), "CLI exited before readiness")
                try:
                    with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5) as response:
                        self.assertEqual(response.status, 200)
                        break
                except (URLError, TimeoutError):
                    time.sleep(0.1)
            else:
                self.fail("CLI did not become ready on loopback")
            self.assertTrue((self.data / "campus.sqlite3").is_file())
            self.assertFalse((self.root / "data").exists())
        finally:
            if os.name == "nt" and process.poll() is None:
                # Windows virtualenv launchers may own a second interpreter.
                # Stop only this test's known PID tree, before its parent exits.
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    timeout=10,
                )
            elif process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


class ShippedFrontendParityTests(unittest.TestCase):
    """dist/ and web/ are two copies of one build, kept for two packagers.

    A hosting packager that drops dist/ (and keeps web/) falls back to web/, so a
    build that refreshed only dist/ would quietly serve a stale frontend there.
    The copies are therefore required to stay byte-identical.
    """

    def test_shipped_dist_and_web_are_byte_identical_when_both_are_present(self):
        dist, web = SOURCE_ROOT / "dist", SOURCE_ROOT / "web"
        if not (dist.is_dir() and web.is_dir()):
            self.skipTest("this checkout ships no frontend build")

        def digest(directory):
            return {
                str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(directory.rglob("*"))
                if path.is_file()
            }

        left, right = digest(dist), digest(web)
        self.assertEqual(sorted(left), sorted(right), "dist/ and web/ ship different files")
        self.assertEqual(left, right, "dist/ and web/ have drifted apart")


if __name__ == "__main__":
    unittest.main()

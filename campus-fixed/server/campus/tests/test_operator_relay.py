"""Offline guards for the operator relay (deploy/operator/relay.py).

These tests never touch the network or a running product: ``build`` runs
against a temporary staging directory, and the feed server is exercised on
an ephemeral loopback port. The relay's contract is narrow on purpose -- it
transports operator-fetched official pages into the product's authorized
feed pipeline without rewriting anything.
"""

import importlib.util
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

RELAY = Path(__file__).resolve().parents[3] / "deploy" / "operator" / "relay.py"

spec = importlib.util.spec_from_file_location("operator_relay", RELAY)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


def record(**overrides):
    base = {
        "url": "https://zs.tju.edu.cn/info/1116/4549.htm",
        "title": "天津大学2026年本科生招生章程",
        "published_at": "2026-05-29",
        "content_html": "<p>第一章 总则</p>",
        "fetched_at": "2026-10-13T19:50:00+08:00",
        "fetched_by": "operator server-side fetch",
    }
    base.update(overrides)
    return base


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.staging = Path(self.tmp.name) / "staging"
        self.out = Path(self.tmp.name) / "out"
        self.addCleanup(self.tmp.cleanup)

    def test_build_writes_feed_and_provenance(self):
        column = "zs-notices"
        (self.staging / column).mkdir(parents=True)
        (self.staging / column / "a.json").write_text(json.dumps(record(), ensure_ascii=False), encoding="utf-8")
        (self.staging / column / "b.json").write_text(
            json.dumps(
                record(url="https://zs.tju.edu.cn/info/1116/4829.htm", title="强基计划公示"), ensure_ascii=False
            ),
            encoding="utf-8",
        )
        provenance = relay.build(self.staging, self.out)
        feed = json.loads((self.out / "feed-zs-notices.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [i["url"] for i in feed["items"]],
            [
                "https://zs.tju.edu.cn/info/1116/4549.htm",
                "https://zs.tju.edu.cn/info/1116/4829.htm",
            ],
        )
        self.assertNotIn("fetched_at", feed["items"][0])
        items = provenance["columns"][column]["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["content_sha256"], relay.hashlib.sha256("<p>第一章 总则</p>".encode()).hexdigest())
        self.assertEqual(provenance["columns"][column]["skipped"], [])

    def test_build_rejects_unsafe_or_incomplete_records(self):
        column = "zs-notices"
        (self.staging / column).mkdir(parents=True)
        cases = {
            "http.json": record(url="http://zs.tju.edu.cn/info/1116/4549.htm"),
            "private.json": record(url="https://127.0.0.1/info/1116/4549.htm"),
            "notitle.json": {k: v for k, v in record().items() if k != "title"},
            "empty.json": record(content_html="   "),
            "long.json": record(title="标" * 301),
        }
        for name, body in cases.items():
            (self.staging / column / name).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        provenance = relay.build(self.staging, self.out)
        skipped = provenance["columns"][column]["skipped"]
        self.assertEqual(len(skipped), len(cases), skipped)
        self.assertEqual(provenance["columns"][column]["items"], [])
        self.assertEqual(json.loads((self.out / "feed-zs-notices.json").read_text(encoding="utf-8"))["items"], [])

    def test_build_dedupes_within_column(self):
        column = "oaa-competitions"
        (self.staging / column).mkdir(parents=True)
        (self.staging / column / "a.json").write_text(json.dumps(record(), ensure_ascii=False), encoding="utf-8")
        (self.staging / column / "b.json").write_text(json.dumps(record(), ensure_ascii=False), encoding="utf-8")
        provenance = relay.build(self.staging, self.out)
        self.assertEqual(len(provenance["columns"][column]["items"]), 1)
        self.assertEqual(provenance["columns"][column]["skipped"][0]["reason"].split()[0], "duplicate")

    def test_build_refuses_oversized_body(self):
        column = "zs-notices"
        (self.staging / column).mkdir(parents=True)
        (self.staging / column / "big.json").write_text(
            json.dumps(record(content_html="x" * (relay.MAX_EMBEDDED_BYTES + 1)), ensure_ascii=False), encoding="utf-8"
        )
        provenance = relay.build(self.staging, self.out)
        self.assertIn("larger than the feed limit", provenance["columns"][column]["skipped"][0]["reason"])


class FeedServerTests(unittest.TestCase):
    def test_serves_built_feed_on_loopback(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "feed-zs-notices.json").write_text('{"items": []}', encoding="utf-8")
            server = relay.FeedHandler
            import http.server

            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server)
            server.out_dir = out
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(httpd.shutdown)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/feed-zs-notices.json", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
                    self.assertEqual(json.loads(response.read()), {"items": []})
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/nope.json", timeout=5)
            finally:
                server.out_dir = relay.OUT_DIR


class SessionTests(unittest.TestCase):
    """The relay must spend at most one dev-auth challenge per session.

    The product allows six challenge codes per email per hour and never two
    within 30 seconds, so a stored cookie is reused whenever the API still
    accepts it -- otherwise back-to-back ``register``/``report``/``decide``
    runs would lock the operator out of their own instance.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session_file = Path(self.tmp.name) / "session.json"
        self._original = relay.SESSION_FILE
        relay.SESSION_FILE = self.session_file
        self.addCleanup(lambda: setattr(relay, "SESSION_FILE", self._original))

    def session(self):
        return relay.AdminSession("http://127.0.0.1:8000", "operator@touxian.local")

    def test_restore_adopts_a_still_valid_cookie(self):
        self.session_file.write_text(
            json.dumps(
                {"api": "http://127.0.0.1:8000", "email": "operator@touxian.local", "cookie": "campus_session=abc"}
            ),
            encoding="utf-8",
        )
        s = self.session()
        seen = {}

        def fake_request(method, path, body=None, headers=None):
            seen["cookie"] = s.cookies
            if path == "/api/v1/me":
                return {"csrf_token": "csrf-1"}, None
            raise AssertionError(path)

        s._request = fake_request
        self.assertTrue(s._restore())
        self.assertEqual(seen["cookie"], "campus_session=abc")
        self.assertEqual(s.csrf, "csrf-1")

    def test_restore_ignores_a_cookie_stored_for_another_instance(self):
        self.session_file.write_text(
            json.dumps({"api": "http://127.0.0.1:9000", "cookie": "campus_session=abc"}), encoding="utf-8"
        )
        s = self.session()
        s._request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the API"))
        self.assertFalse(s._restore())
        self.assertEqual(s.cookies, "")

    def test_restore_drops_a_cookie_the_api_rejects(self):
        self.session_file.write_text(
            json.dumps({"api": "http://127.0.0.1:8000", "cookie": "campus_session=stale"}), encoding="utf-8"
        )
        s = self.session()

        def fake_request(method, path, body=None, headers=None):
            raise urllib.error.HTTPError("http://127.0.0.1:8000/api/v1/me", 401, "Unauthorized", None, None)

        s._request = fake_request
        self.assertFalse(s._restore())
        self.assertEqual(s.cookies, "")

    def test_login_persists_the_cookie_and_a_later_run_reuses_it(self):
        s = self.session()
        calls = []

        def fake_request(method, path, body=None, headers=None):
            calls.append(path)
            if path == "/api/v1/auth/challenges":
                return {"challenge_id": "ch-1"}, "123456"
            if path == "/api/v1/auth/sessions":
                s.cookies = "campus_session=live"
                return {"user": {"email": s.email}}, None
            if path == "/api/v1/me":
                return {"csrf_token": "csrf-2"}, None
            raise AssertionError(path)

        s._request = fake_request
        me = s.login()
        self.assertEqual(me["csrf_token"], "csrf-2")
        self.assertEqual(calls, ["/api/v1/auth/challenges", "/api/v1/auth/sessions", "/api/v1/me"])
        stored = json.loads(self.session_file.read_text(encoding="utf-8"))
        self.assertEqual(stored["cookie"], "campus_session=live")
        self.assertEqual(stored["api"], "http://127.0.0.1:8000")

        later = self.session()
        reused = []

        def later_request(method, path, body=None, headers=None):
            reused.append(path)
            if path == "/api/v1/me":
                return {"csrf_token": "csrf-3"}, None
            raise AssertionError(path)

        later._request = later_request
        self.assertTrue(later._restore())
        self.assertEqual(reused, ["/api/v1/me"])
        self.assertEqual(later.csrf, "csrf-3")
        self.assertEqual(later.cookies, "campus_session=live")


if __name__ == "__main__":
    unittest.main()

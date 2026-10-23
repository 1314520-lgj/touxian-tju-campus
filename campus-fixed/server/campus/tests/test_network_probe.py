"""Probe safety/diagnostics with deliberate fixtures; never live-health evidence."""

import fnmatch
import importlib.util
import io
import json
import os
import socket
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from server.campus.sources import FetchError, FetchResult

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("campus_network_probe", ROOT / "deploy/cloudrun/probe_sources.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def answer(*addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]


def source():
    return {
        "id": "c4e00274-5b08-5d26-9a6d-19aed0d71571",
        "source_identity": "oaa",
        "origin_url": "https://oaa.tju.edu.cn/list.htm",
        "adapter": "website",
        "config": {"article_url_pattern": r"/info/\d+/\d+\.htm"},
    }


def clean_body(body, url):
    return ([{"id": "body:0", "text": "请报名参加活动。", "page": None}], "ok", [])


class Response:
    def __init__(self, status, body=b"", headers=None):
        self.status, self.body, self.headers = status, io.BytesIO(body), headers or {}

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, count):
        return self.body.read(count)


class Connections:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, host, ip, port, timeout):
        owner = self

        class Connection:
            def request(self, method, path, headers):
                self.host, self.path = host, path
                owner.calls.append((host, ip, path, headers))

            def getresponse(self):
                status, body, headers = owner.routes[(self.host, self.path)]
                return Response(status, body, headers)

            def close(self):
                pass

        return Connection()


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch(self, url):
        self.calls.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value


class NetworkProbeTests(unittest.TestCase):
    def test_runtime_image_copies_probe_without_changing_default_command(self):
        docker = (ROOT / "deploy/cloudrun/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY deploy/cloudrun/probe_sources.py ./deploy/cloudrun/probe_sources.py", docker)
        self.assertIn('CMD ["python", "-m", "server.campus.cloud_run", "serve"]', docker)
        for relative in ["deploy/cloudrun/.gcloudignore", "deploy/cloudrun/Dockerfile.dockerignore"]:
            patterns = (ROOT / relative).read_text(encoding="utf-8").splitlines()
            self.assertFalse(
                any(
                    fnmatch.fnmatch("deploy/cloudrun/probe_sources.py", value)
                    for value in patterns
                    if value and not value.startswith("#")
                )
            )

    def test_preview_never_resolves_or_constructs_core_or_creates_data(self):
        with (
            patch("socket.getaddrinfo", side_effect=AssertionError("network forbidden")),
            patch.dict(
                os.environ,
                {"CAMPUS_DATABASE_URL": "postgresql://secret@private/db", "CAMPUS_DATA_DIR": "/must-not-be-created"},
            ),
        ):
            report = probe.run(probe.load_catalog(), network=False)
        # The catalog grows as official columns are added; assert the shape of the
        # result, not a hard-coded count that turns every new source into a failure.
        self.assertEqual(set(report["counts"]), {"not_checked"})
        self.assertEqual(report["counts"]["not_checked"], len(probe.load_catalog()))
        self.assertNotIn("secret", json.dumps(report))

    def test_catalog_rejects_non_official_or_authenticated_origins(self):
        for url in [
            "https://school.example/list",
            "http://oaa.tju.edu.cn/list",
            "https://user:secret@oaa.tju.edu.cn/list",
            "https://oaa.tju.edu.cn/list?token=secret",
        ]:
            with self.subTest(url=url):
                item = source()
                item["origin_url"] = url
                with (
                    patch.object(Path, "read_text", return_value=json.dumps({"sources": [item]})),
                    self.assertRaises(ValueError),
                ):
                    probe.load_catalog()

    def test_dns_fake_ip_observed_and_rejected_before_connection(self):
        connector = Connections({})
        report = probe.run(
            [source()], network=True, resolver=lambda *a, **k: answer("198.18.7.9"), connector=connector, pause=0
        )
        stage = report["sources"][0]["stages"][0]
        self.assertEqual(stage["error"], "unsafe_address")
        self.assertEqual(stage["dns"][0]["fake_ip_count"], 1)
        self.assertEqual(connector.calls, [])
        self.assertNotIn("198.18.7.9", json.dumps(report))

    def test_mixed_public_private_dns_rejected_even_with_trust_environment(self):
        connector = Connections({})
        with patch.dict(os.environ, {"CAMPUS_TRUSTED_GATEWAY_ORIGINS": "https://oaa.tju.edu.cn"}):
            report = probe.run(
                [source()],
                network=True,
                resolver=lambda *a, **k: answer("8.8.8.8", "10.0.0.2"),
                connector=connector,
                pause=0,
            )
        self.assertEqual(report["sources"][0]["stages"][0]["error"], "unsafe_address")
        self.assertEqual(connector.calls, [])

    def test_redirect_rechecks_public_dns_and_does_not_log_location_secret(self):
        connector = Connections(
            {("oaa.tju.edu.cn", "/list.htm"): (302, b"", {"Location": "https://private.example/x?token=SECRET"})}
        )

        def resolver(host, *a, **k):
            return answer("8.8.8.8" if host == "oaa.tju.edu.cn" else "127.0.0.1")

        report = probe.run([source()], network=True, resolver=resolver, connector=connector, pause=0)
        stage = report["sources"][0]["stages"][0]
        self.assertEqual(stage["error"], "unsafe_address")
        self.assertEqual(len(stage["dns"]), 2)
        self.assertEqual(len(connector.calls), 1)
        self.assertNotIn("SECRET", json.dumps(report))

    def test_200_login_with_matching_notice_link_is_not_success(self):
        html = b'<form><input type="password"></form><a href="/info/1/2.htm">Notice</a>'
        connector = Connections({("oaa.tju.edu.cn", "/list.htm"): (200, html, {"Content-Type": "text/html"})})
        report = probe.run(
            [source()], network=True, resolver=lambda *a, **k: answer("8.8.8.8"), connector=connector, pause=0
        )
        self.assertEqual(report["sources"][0]["state"], "failed")
        self.assertEqual(report["sources"][0]["finding_codes"], ["suspicious_html"])
        self.assertEqual(len(connector.calls), 1)

    def test_article_body_and_attachment_sampling_are_bounded_and_text_redacted(self):
        listing_url = source()["origin_url"]
        article = "https://oaa.tju.edu.cn/info/1/2.htm"
        listing = '<a href="/info/1/2.htm?token=SECRET">Student personal title</a><a href="/info/1/3.htm">More</a>'
        article += "?token=SECRET"
        body = '<article>STUDENT_PRIVATE_CONTENT</article><a href="/one.pdf?key=SECRET">download</a><a href="/two.pdf">more</a>'
        fetcher = FakeFetcher(
            {
                listing_url: FetchResult(listing_url, listing.encode(), "text/html"),
                article: FetchResult(article, body.encode(), "text/html"),
                "https://oaa.tju.edu.cn/one.pdf?key=SECRET": FetchResult(
                    "https://oaa.tju.edu.cn/one.pdf?key=SECRET", b"%PDF-test", "application/pdf"
                ),
            }
        )
        report = probe.probe_source(source(), fetcher, probe.ObservedResolver(), pause=0, html_analyzer=clean_body)
        self.assertEqual(len(fetcher.calls), 3)
        self.assertEqual(report["listing_items"], 2)
        sample = report["article_samples"][0]
        self.assertEqual(sample["state"], "ok")
        self.assertTrue(sample["attachment_sample_truncated"])
        self.assertEqual(sample["attachments"][0]["state"], "not_run")
        self.assertEqual(report["state"], "review_required")
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertNotIn("STUDENT_PRIVATE_CONTENT", json.dumps(report))
        self.assertNotIn("Student personal title", json.dumps(report))

    def test_article_login_never_fetches_its_attachments(self):
        origin, article = source()["origin_url"], "https://oaa.tju.edu.cn/info/1/2.htm"
        fetcher = FakeFetcher(
            {
                origin: FetchResult(origin, b'<a href="/info/1/2.htm">Notice</a>', "text/html"),
                article: FetchResult(
                    article, b'<input type="password"><a href="/secret.pdf">download</a>', "text/html"
                ),
            }
        )
        report = probe.probe_source(source(), fetcher, probe.ObservedResolver(), pause=0, html_analyzer=clean_body)
        self.assertEqual(report["article_samples"][0]["state"], "failed")
        self.assertEqual(len(fetcher.calls), 2)

    def test_zero_article_sample_is_explicit_listing_only(self):
        origin = source()["origin_url"]
        fetcher = FakeFetcher({origin: FetchResult(origin, b'<a href="/info/1/2.htm">Notice</a>', "text/html")})
        report = probe.probe_source(source(), fetcher, probe.ObservedResolver(), articles=0, pause=0)
        self.assertEqual(report["state"], "listing_only")
        self.assertEqual(len(fetcher.calls), 1)

    def test_listing_window_marks_truncation(self):
        origin = source()["origin_url"]
        html = "".join(f'<a href="/info/1/{i}.htm">Notice</a>' for i in range(150)).encode()
        fetcher = FakeFetcher({origin: FetchResult(origin, html, "text/html")})
        report = probe.probe_source(source(), fetcher, probe.ObservedResolver(), articles=0, pause=0)
        self.assertEqual(report["listing_items"], 100)
        self.assertTrue(report["listing_window_truncated"])

    def test_network_headers_never_include_environment_source_token(self):
        connector = Connections({("oaa.tju.edu.cn", "/list.htm"): (200, b"empty", {"Content-Type": "text/html"})})
        with patch.dict(os.environ, {"CAMPUS_SOURCE_C4E002745B085D269A6D19AED0D71571_TOKEN": "PRIVATE_TOKEN"}):
            report = probe.run(
                [source()], network=True, resolver=lambda *a, **k: answer("8.8.8.8"), connector=connector, pause=0
            )
        self.assertNotIn("Authorization", connector.calls[0][3])
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(report))

    def test_large_response_is_rejected_by_product_byte_guard(self):
        connector = Connections(
            {
                ("oaa.tju.edu.cn", "/list.htm"): (
                    200,
                    b"",
                    {"Content-Type": "text/html", "Content-Length": str(9 * 1024 * 1024)},
                )
            }
        )
        report = probe.run(
            [source()], network=True, resolver=lambda *a, **k: answer("8.8.8.8"), connector=connector, pause=0
        )
        self.assertEqual(report["sources"][0]["stages"][0]["error"], "body_too_large")

    def test_raw_exception_or_error_message_never_enters_report(self):
        for failure in [RuntimeError("password=SECRET_TOKEN"), FetchError("https://private?key=SECRET_TOKEN")]:
            with self.subTest(failure=type(failure).__name__):
                report = probe.probe_source(
                    source(), FakeFetcher({source()["origin_url"]: failure}), probe.ObservedResolver(), pause=0
                )
                self.assertEqual(report["finding_codes"], ["unexpected_probe_error"])
                self.assertNotIn("SECRET_TOKEN", json.dumps(report))

    def test_cli_invalid_source_and_preview_exit_are_distinct(self):
        with redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(probe.main([]), 0)
        self.assertEqual(json.loads(captured.getvalue())["mode"], "preview")
        with self.assertRaises(SystemExit) as error, redirect_stdout(io.StringIO()):
            probe.main(["--source", "not-an-official-uuid"])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

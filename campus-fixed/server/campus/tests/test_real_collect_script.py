"""The real-data collection entry point (``scripts/campus_collect.py``).

The product already knows how to fetch, parse and extract; what was missing is
an operator-facing command that does all three against a *fresh* data directory
and says honestly what happened. These tests pin the four properties that make
that command safe to run daily:

* a fresh directory ends up with registered sources and reviewable candidates;
* a second run over unchanged pages re-ingests nothing (snapshot identity is
  content-addressed, so no duplicate documents or candidates appear);
* a failing source is reported with its error code instead of disappearing;
* the command never writes the bundled ``data/`` sample and never prints the
  machine's absolute paths into evidence that ships with the release.

No test touches the network: fetches are an explicit mapping fixture, and the
only real DNS call (the gateway diagnosis) is exercised through a resolver
double.
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.campus.content import Pipeline  # noqa: E402
from server.campus.platform import Core, Settings  # noqa: E402
from server.campus.sources import FetchError, FetchResult  # noqa: E402

SCRIPT = ROOT / "scripts" / "campus_collect.py"
spec = importlib.util.spec_from_file_location("campus_collect", SCRIPT)
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)

SOURCE_ID = "11111111-1111-5111-8111-111111111111"
LISTING_URL = "https://public.test/list"
ARTICLE_URL = "https://public.test/info/1/1.htm"
LISTING = b'<html><body><a href="/info/1/1.htm">2026 notice</a></body></html>'
ARTICLE = (
    "<html><head><title>2026 notice</title></head><body>"
    "<h1>2026 notice</h1><div class=\"v_news_content\">"
    "<p>全体本科生请于2026年10月20日前提交申请表。</p>"
    "</div></body></html>"
).encode()


def fixture_source(**overrides):
    source = {
        "id": SOURCE_ID,
        "name": "Fixture official column",
        "kind": "website",
        "origin_url": LISTING_URL,
        "adapter": "website",
        "source_identity": "fixture",
        "verification": "verified_official",
        "verification_basis": "fixture",
        "enabled": True,
        "health": "never_checked",
        "last_success_at": None,
        "last_checked_at": None,
        "last_error_code": None,
    }
    source.update(overrides)
    return source


class MappingFetcher:
    """Explicit offline fetch double; every miss is a labeled failure."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def fetch(self, url, headers=None):
        self.calls.append(url)
        value = self.mapping.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise FetchError("fixture_url_missing")
        return value


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = Core(
            Settings(data_dir=Path(self.tmp.name) / "operator", secret="s" * 48, dev_auth=True)
        )
        self.core.pipeline = Pipeline(self.core)
        self.core.store.put("sources", SOURCE_ID, fixture_source())

    def tearDown(self):
        self.core.store.close()

    def fetcher(self, article=ARTICLE):
        return MappingFetcher(
            {
                LISTING_URL: FetchResult(LISTING_URL, LISTING, "text/html"),
                ARTICLE_URL: FetchResult(ARTICLE_URL, article, "text/html"),
            }
        )

    def test_a_fresh_directory_gets_snapshots_parsed_and_pending_candidates(self):
        summary = collect.collect(self.core, fetcher=self.fetcher())
        row = summary["sources"][0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["new_snapshots"], 1)
        self.assertEqual(row["parse"]["ok"], 1)
        self.assertEqual(row["candidates_new"], 1)
        self.assertEqual(row["backlog"]["pending_count"], 0)
        self.assertEqual(summary["totals"]["new_snapshots"], 1)
        self.assertEqual(summary["totals"]["candidates_total"], 1)
        self.assertEqual(summary["totals"]["candidates_pending"], 1)
        self.assertEqual(summary["totals"]["ok"], 1)
        self.assertEqual(collect.exit_code(summary["totals"]), collect.EXIT_OK)
        snapshot = self.core.store.list("snapshots")[0]
        self.assertIsNotNone(self.core.pipeline.parsed(snapshot["snapshot_id"]))
        candidate = self.core.store.list("candidates")[0]
        self.assertEqual(candidate["review_status"], "pending")
        self.assertEqual(candidate["title"], "2026 notice")

    def test_second_run_over_unchanged_pages_ingests_nothing_new(self):
        first = collect.collect(self.core, fetcher=self.fetcher())
        second = collect.collect(self.core, fetcher=self.fetcher())
        self.assertEqual(first["totals"]["new_snapshots"], 1)
        self.assertEqual(second["totals"]["new_snapshots"], 0)
        self.assertEqual(second["totals"]["candidates_new"], 0)
        self.assertEqual(second["totals"]["candidates_total"], 1)
        self.assertEqual(second["totals"]["documents"], 1)
        self.assertEqual(len(self.core.store.list("snapshots")), 1)
        self.assertEqual(len(self.core.store.list("candidates")), 1)

    def test_a_failed_source_reports_its_error_code_instead_of_vanishing(self):
        fetcher = MappingFetcher({LISTING_URL: FetchError("unsafe_address")})
        summary = collect.collect(self.core, fetcher=fetcher)
        row = summary["sources"][0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["errors"], ["unsafe_address"])
        self.assertEqual(summary["totals"]["failed"], 1)
        self.assertEqual(collect.exit_code(summary["totals"]), collect.EXIT_FAILED)
        out = io.StringIO()
        collect.render_summary(summary, out)
        text = out.getvalue()
        self.assertIn("unsafe_address", text)
        self.assertIn("失败", text)

    def test_pending_count_reflects_a_human_decision(self):
        collect.collect(self.core, fetcher=self.fetcher())
        candidate = self.core.store.list("candidates")[0]
        self.core.pipeline.review(
            candidate["candidate_id"],
            {
                "expected_candidate_revision": candidate["revision"],
                "decision": "accept",
                "reason": "fixture decision",
                "replacement_candidate": None,
            },
            {"id": "22222222-2222-5222-8222-222222222222"},
        )
        summary = collect.collect(self.core, fetcher=self.fetcher())
        self.assertEqual(summary["totals"]["candidates_pending"], 0)
        self.assertEqual(summary["totals"]["candidates_total"], 1)
        self.assertEqual(summary["totals"]["candidates_new"], 0)

    def test_unknown_selector_is_a_usage_error_not_an_empty_run(self):
        with self.assertRaises(collect.UsageError) as caught:
            collect.collect(self.core, selectors=["does-not-exist"], fetcher=self.fetcher())
        self.assertIn("does-not-exist", str(caught.exception))

    def test_gateway_failures_are_reported_when_the_source_returns_degraded(self):
        listing = b'<html><body><a href="/info/1/1.htm">notice</a><a href="/info/1/2.htm">blocked</a></body></html>'
        fetcher = MappingFetcher(
            {
                LISTING_URL: FetchResult(LISTING_URL, listing, "text/html"),
                ARTICLE_URL: FetchResult(ARTICLE_URL, ARTICLE, "text/html"),
                "https://public.test/info/1/2.htm": FetchError("article_redirected_off_pattern"),
            }
        )
        summary = collect.collect(self.core, fetcher=fetcher)
        row = summary["sources"][0]
        self.assertEqual(row["status"], "degraded")
        self.assertIn("article_redirected_off_pattern", row["errors"])
        self.assertEqual(summary["totals"]["new_snapshots"], 1)
        out = io.StringIO()
        collect.render_summary(summary, out)
        self.assertIn("article_redirected_off_pattern", out.getvalue())


class RunTests(unittest.TestCase):
    """The command line half: data-directory guards and path-free evidence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        code = collect.run(argv, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_init_registers_sources_once_and_reuses_them_next_time(self):
        data_dir = str(Path(self.tmp.name) / "operator")
        report = Path(self.tmp.name) / "first.json"
        code, out, err = self.run_cli(
            ["init", "--no-collect", "--data-dir", data_dir, "--json-out", str(report)]
        )
        self.assertEqual(code, collect.EXIT_OK, err)
        first = json.loads(report.read_text(encoding="utf-8"))
        self.assertFalse(first["collected"])
        self.assertEqual(first["command"], "init")
        self.assertGreaterEqual(first["registration"]["new"], 19)
        self.assertEqual(first["registration"]["existing"], 0)

        second_report = Path(self.tmp.name) / "second.json"
        code, out, err = self.run_cli(
            ["init", "--no-collect", "--data-dir", data_dir, "--json-out", str(second_report)]
        )
        self.assertEqual(code, collect.EXIT_OK, err)
        second = json.loads(second_report.read_text(encoding="utf-8"))
        self.assertEqual(second["registration"]["new"], 0)
        self.assertEqual(second["registration"]["existing"], first["registration"]["new"])
        self.assertEqual(len(second["sources"]), 0)

    def test_the_bundled_sample_directory_is_refused(self):
        code, out, err = self.run_cli(["collect", "--data-dir", str(ROOT / "data")])
        self.assertEqual(code, collect.EXIT_USAGE)
        self.assertIn("data/", err)
        self.assertIn("随包样本", err)

    def test_a_subdirectory_of_the_sample_is_refused_too(self):
        code, out, err = self.run_cli(["collect", "--data-dir", str(ROOT / "data" / "snapshots")])
        self.assertEqual(code, collect.EXIT_USAGE)
        self.assertIn("随包样本", err)

    def test_output_and_json_never_name_an_absolute_machine_path(self):
        data_dir = str(Path(self.tmp.name) / "operator")
        report = Path(self.tmp.name) / "summary.json"
        code, out, err = self.run_cli(
            ["init", "--no-collect", "--data-dir", data_dir, "--json-out", str(report)]
        )
        self.assertEqual(code, collect.EXIT_OK, err)
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["data_dir"], "operator")
        for text in (out, report.read_text(encoding="utf-8")):
            self.assertNotIn(str(Path(self.tmp.name)), text)
            self.assertNotRegex(text, r"[A-Za-z]:\\")
            self.assertNotIn("AppData", text)

    def test_a_run_leaves_the_inherited_data_dir_environment_untouched(self):
        data_dir = str(Path(self.tmp.name) / "operator")
        with mock.patch.dict(os.environ, {"CAMPUS_DATA_DIR": "inherited-marker"}, clear=False):
            code, out, err = self.run_cli(["init", "--no-collect", "--data-dir", data_dir])
            self.assertEqual(code, collect.EXIT_OK, err)
            self.assertEqual(os.environ["CAMPUS_DATA_DIR"], "inherited-marker")


class GatewayTests(unittest.TestCase):
    def test_config_origins_cover_every_official_host_and_nothing_wild(self):
        origins = collect.gateway_origins_from_config()
        self.assertIn("https://oaa.tju.edu.cn", origins)
        self.assertIn("https://gs.tju.edu.cn", origins)
        self.assertIn("https://mp.weixin.qq.com", origins)
        for origin in origins:
            self.assertRegex(origin, r"^https://[A-Za-z0-9.-]+$")

    def test_fake_ip_resolution_is_flagged_with_an_actionable_hint(self):
        def resolver(host, port, **kwargs):
            ip = "198.18.0.200" if host == "oaa.tju.edu.cn" else "93.184.216.34"
            return [(2, 1, 6, "", (ip, port))]

        flagged = collect.diagnose_gateway(
            ["https://oaa.tju.edu.cn", "https://example.com"], resolver=resolver
        )
        self.assertEqual(flagged, ["https://oaa.tju.edu.cn"])
        hint = collect.gateway_hint(flagged)
        self.assertIn("CAMPUS_TRUSTED_GATEWAY_ORIGINS", hint)
        self.assertIn("--gateway-from-config", hint)
        self.assertIn("REAL-DATA-RUNBOOK", hint)

    def test_gateway_from_config_sets_only_the_documented_variable(self):
        data_dir = str(Path(tempfile.mkdtemp()) / "operator")
        self.addCleanup(lambda: __import__("shutil").rmtree(Path(data_dir).parent, ignore_errors=True))
        with mock.patch.dict(os.environ, {"CAMPUS_TRUSTED_GATEWAY_ORIGINS": ""}, clear=False):
            out, err = io.StringIO(), io.StringIO()
            code = collect.run(
                ["init", "--no-collect", "--data-dir", data_dir, "--gateway-from-config"],
                out=out,
                err=err,
            )
            self.assertEqual(code, collect.EXIT_OK, err.getvalue())
            value = os.environ.get("CAMPUS_TRUSTED_GATEWAY_ORIGINS", "")
            self.assertIn("https://oaa.tju.edu.cn", value)
            self.assertIn("https://mp.weixin.qq.com", value)
            self.assertIn("受信网关", out.getvalue())


if __name__ == "__main__":
    unittest.main()

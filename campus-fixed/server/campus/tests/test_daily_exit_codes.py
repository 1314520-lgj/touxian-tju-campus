"""Exit-code semantics of the daily update: skipped optional channels vs real failures.

An unattended run has to be able to say "nothing to report" even when the operator
relay is not switched on. ``feed_https_required`` is raised by the product before
anything is fetched -- it is evidence about *configuration* (the relay is not
running / its origin was never trusted), not about a site being down. Letting it
fail the run meant the command exited 1 every single day, so the exit code stopped
being an alarm: a bell that rings daily tells nobody anything.

The opposite mistake is worse than a noisy alarm, so these tests pin the boundary
rather than the convenience:

* a source that cannot be collected (unreachable host, upstream status, an address
  refused by the safety check) still exits 1;
* a notice body that fails to parse, and an extraction that raises, still exit 1;
* skipping is decided by error codes only. A source literally named "运营中继 · …"
  that fails for a real reason still exits 1, and an *enabled* authorized feed
  serving broken content still exits 1;
* a skipped source never masks a real failure in the same run.

The scripts are loaded, not re-implemented: ``scripts/daily_update.py`` decides the
exit code and ``scripts/campus_collect.py`` collects, exactly as in production.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.campus.sources import FetchError, FetchResult  # noqa: E402

SCRIPTS = ROOT / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collect = load("campus_collect")
daily_update = load("daily_update")

WEBSITE_ID = "11111111-1111-5111-8111-111111111111"
FEED_ID = "33333333-3333-5333-8333-333333333333"
USER_ID = "22222222-2222-5222-8222-222222222222"
LISTING_URL = "https://public.test/list"
ARTICLE_URL = "https://public.test/info/1/1.htm"
# The registered relay feeds look exactly like this: an authorized feed over
# http://127.0.0.1 (the operator relay), which the product refuses unless the
# origin is explicitly trusted.
RELAY_URL = "http://relay.test/feed.json"
ENABLED_FEED_URL = "https://relay.test/feed.json"
TODAY = date(2026, 10, 21)
LISTING = b'<html><body><a href="/info/1/1.htm">2026 notice</a></body></html>'


def source(source_id, name, origin_url, **overrides):
    row = {
        "id": source_id,
        "name": name,
        "kind": "website",
        "origin_url": origin_url,
        "adapter": "website",
        "source_identity": source_id[:8],
        "verification": "verified_official",
        "verification_basis": "fixture",
        "enabled": True,
        "health": "never_checked",
        "last_success_at": None,
        "last_checked_at": None,
        "last_error_code": None,
    }
    row.update(overrides)
    return row


def website_source(name="Fixture official column"):
    return source(WEBSITE_ID, name, LISTING_URL)


def relay_feed(name="运营中继 · 测试 feed", origin_url=RELAY_URL):
    """The real shape of the two registered relay sources (see data-operator)."""
    return source(
        FEED_ID,
        name,
        origin_url,
        kind="authorized_feed",
        adapter="authorized_api",
        source_identity="relay-fixture",
        feed_content_mode="authorized_fulltext",
    )


def article(title, sentence):
    return (
        f"<html><head><title>{title}</title></head><body><h1>{title}</h1>"
        f'<div class="v_news_content"><p>{sentence}</p></div></body></html>'
    ).encode()


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


class ExitCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "operator"

    # -- helpers ------------------------------------------------------------
    def build_core(self, data_dir, timeout=None):
        """A core with no catalog sources, so only this test's fixtures exist."""
        from server.campus import sources as sources_module

        patcher = mock.patch.object(sources_module, "builtin_sources", lambda: [])
        patcher.start()
        self.addCleanup(patcher.stop)
        return collect.build_core(data_dir, timeout=timeout)

    def core_factory(self, hook=None):
        def factory(data_dir, timeout=None):
            core = self.build_core(data_dir, timeout=timeout)
            if hook is not None:
                hook(core)
            return core

        return factory

    def prepare(self, *sources):
        """A data directory holding exactly the given fixture sources."""
        core = self.build_core(self.data_dir)
        for row in sources:
            core.store.put("sources", row["id"], row)
        core.store.put("users", USER_ID, {"id": USER_ID, "email": "operator@example.com", "role": "admin"})
        core.store.close()

    def run_cli(self, fetcher=None, hook=None, extra=()):
        """Run the command offline; return (exit code, stdout, stderr, summary)."""
        summary_path = Path(self.tmp.name) / "summary.json"
        out, err = io.StringIO(), io.StringIO()
        code = daily_update.run(
            ["--data-dir", str(self.data_dir), "--today", TODAY.isoformat(), "--apply",
             "--json-out", str(summary_path), *extra],
            out=out,
            err=err,
            fetcher=fetcher if fetcher is not None else MappingFetcher({}),
            core_factory=self.core_factory(hook),
        )
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        return code, out.getvalue(), err.getvalue(), summary

    def stage(self, summary):
        return summary["stages"]["collect"]

    def codes(self, rows):
        return sorted({code for row in rows for code in row.get("codes") or []})

    # -- skipped: an optional channel that is not switched on ---------------
    def test_an_unstarted_relay_feed_is_skipped_and_the_run_stays_clean(self):
        self.prepare(relay_feed())

        code, out, err, summary = self.run_cli()

        self.assertEqual(code, 0, err)
        collect_stage = self.stage(summary)
        self.assertEqual([row["name"] for row in collect_stage["failures"]], [])
        self.assertEqual(self.codes(collect_stage["skipped"]), ["feed_https_required"])
        self.assertIn("跳过来源", out)
        self.assertIn("feed_https_required", out)
        self.assertIn("失败 0", out)
        self.assertIn("退出码 0", out)
        self.assertIn("通道没开，不是故障", out, "the skip must say why in plain words")

    def test_the_relay_never_reached_the_network(self):
        """The skip is a configuration fact, not a swallowed outage."""
        self.prepare(relay_feed())
        fetcher = MappingFetcher({})

        code, _, err, summary = self.run_cli(fetcher=fetcher)

        self.assertEqual(code, 0, err)
        self.assertEqual(fetcher.calls, [], "an untrusted feed origin is refused before any request")
        self.assertEqual(self.codes(self.stage(summary)["skipped"]), ["feed_https_required"])

    def test_a_feed_whose_authorization_was_never_verified_is_skipped(self):
        """`feed_authorization_not_verified` is the same class: not switched on.

        The source is a feed over https (so the transport is fine) whose
        authorization basis was never recorded -- the operator has not set the
        channel up yet, which is a configuration fact, not an outage.
        """
        row = relay_feed(origin_url=ENABLED_FEED_URL)
        row["verification"] = "unverified"
        self.prepare(row)

        code, out, err, summary = self.run_cli()

        self.assertEqual(code, 0, err)
        self.assertEqual(self.codes(self.stage(summary)["skipped"]), ["feed_authorization_not_verified"])
        self.assertIn("跳过来源", out)

    # -- failures: the product could not do its job -------------------------
    def test_a_source_that_cannot_be_collected_still_fails_the_run(self):
        cases = {
            "dns_failed": "host cannot be resolved",
            "fetch_transport_failed": "connection dropped mid-fetch",
            "upstream_http_500": "official site returned a server error",
            "upstream_http_404": "listing page is gone",
            "unsafe_address": "address refused by the safety check (fake-IP / reserved)",
            "unsafe_url": "url refused by the safety check",
        }
        for code_name, why in cases.items():
            with self.subTest(code=code_name, why=why):
                self.setUp()
                self.prepare(website_source())
                code, out, err, summary = self.run_cli(
                    fetcher=MappingFetcher({LISTING_URL: FetchError(code_name)})
                )
                self.assertEqual(code, 1, f"{code_name} must still fail the run: {err}")
                collect_stage = self.stage(summary)
                self.assertEqual(self.codes(collect_stage["failures"]), [code_name])
                self.assertEqual(collect_stage["skipped"], [], "a real failure is never a skip")
                self.assertIn("失败来源", out)
                self.assertIn(code_name, out)

    def test_a_notice_body_parse_failure_still_fails_the_run(self):
        self.prepare(website_source())

        def break_parsing(core):
            def explode(payload):
                raise FetchError("fixture_parse_broken")

            core.pipeline.parse_document = explode

        code, out, err, summary = self.run_cli(
            fetcher=MappingFetcher(
                {
                    LISTING_URL: FetchResult(LISTING_URL, LISTING, "text/html"),
                    ARTICLE_URL: FetchResult(
                        ARTICLE_URL,
                        article("2026年秋季学期选课通知", "全体本科生请于2026年12月1日前完成选课。"),
                        "text/html",
                    ),
                }
            ),
            hook=break_parsing,
        )

        self.assertEqual(code, 1, err)
        collect_stage = self.stage(summary)
        self.assertEqual(self.codes(collect_stage["failures"]), ["fixture_parse_broken"])
        self.assertEqual(collect_stage["skipped"], [])
        self.assertIn("fixture_parse_broken", out)

    def test_an_extraction_failure_still_fails_the_run(self):
        self.prepare(website_source())

        def break_extraction(core):
            def explode(payload):
                raise FetchError("fixture_extract_broken")

            core.pipeline.extract_candidates = explode

        code, out, err, summary = self.run_cli(
            fetcher=MappingFetcher(
                {
                    LISTING_URL: FetchResult(LISTING_URL, LISTING, "text/html"),
                    ARTICLE_URL: FetchResult(
                        ARTICLE_URL,
                        article("2026年秋季学期选课通知", "全体本科生请于2026年12月1日前完成选课。"),
                        "text/html",
                    ),
                }
            ),
            hook=break_extraction,
        )

        self.assertEqual(code, 1, err)
        self.assertEqual(self.codes(self.stage(summary)["failures"]), ["fixture_extract_broken"])
        self.assertIn("fixture_extract_broken", out)

    # -- the rule is keyed on codes, never on names -------------------------
    def test_a_source_named_like_the_relay_still_fails_for_a_real_reason(self):
        """Same name as the real relay, same feed shape, but a real fetch failure."""
        self.prepare(
            source(
                FEED_ID,
                "运营中继 · 教务处竞赛科创通知",
                ENABLED_FEED_URL,
                kind="authorized_feed",
                adapter="authorized_api",
                source_identity="relay-oaa-competitions",
                feed_content_mode="authorized_fulltext",
            )
        )

        code, out, err, summary = self.run_cli(
            fetcher=MappingFetcher({ENABLED_FEED_URL: FetchError("fetch_transport_failed")})
        )

        self.assertEqual(code, 1, "a name must never turn a real failure into a skip")
        collect_stage = self.stage(summary)
        self.assertEqual(self.codes(collect_stage["failures"]), ["fetch_transport_failed"])
        self.assertEqual(
            [row["name"] for row in collect_stage["skipped"]],
            [],
            "the skip rule is evidence-based: this source is named 运营中继 and is still a failure",
        )

    def test_an_enabled_feed_serving_broken_content_still_fails_the_run(self):
        """The same feed shape as the skipped one; only the error code differs."""
        for code_name in ("invalid_feed_content", "feed_fulltext_missing", "feed_receipt_missing_or_changed"):
            with self.subTest(code=code_name):
                self.setUp()
                self.prepare(relay_feed(origin_url=ENABLED_FEED_URL))
                code, out, err, summary = self.run_cli(
                    fetcher=MappingFetcher({ENABLED_FEED_URL: FetchError(code_name)})
                )
                self.assertEqual(code, 1, f"a broken *enabled* feed is a real failure: {err}")
                self.assertEqual(self.codes(self.stage(summary)["failures"]), [code_name])
                self.assertEqual(self.stage(summary)["skipped"], [])

    # -- skips must not mask failures ---------------------------------------
    def test_a_skipped_source_never_masks_a_real_failure(self):
        self.prepare(relay_feed(), website_source("Fixture broken column"))

        code, out, err, summary = self.run_cli(
            fetcher=MappingFetcher({LISTING_URL: FetchError("dns_failed")})
        )

        self.assertEqual(code, 1, "a skipped optional source must not hide an outage")
        collect_stage = self.stage(summary)
        self.assertEqual(self.codes(collect_stage["skipped"]), ["feed_https_required"])
        self.assertEqual(self.codes(collect_stage["failures"]), ["dns_failed"])
        self.assertIn("跳过来源", out)
        self.assertIn("失败来源", out)
        self.assertIn("退出码 1", out)

    def test_a_clean_run_without_skips_exits_zero_and_reports_no_skip(self):
        self.prepare(website_source())

        code, out, err, summary = self.run_cli(
            fetcher=MappingFetcher(
                {
                    LISTING_URL: FetchResult(LISTING_URL, LISTING, "text/html"),
                    ARTICLE_URL: FetchResult(
                        ARTICLE_URL,
                        article("2026年秋季学期选课通知", "全体本科生请于2026年12月1日前完成选课。"),
                        "text/html",
                    ),
                }
            )
        )

        self.assertEqual(code, 0, err)
        collect_stage = self.stage(summary)
        self.assertEqual(collect_stage["skipped"], [])
        self.assertEqual(collect_stage["failures"], [])
        self.assertNotIn("跳过来源", out)
        self.assertIn("退出码 0", out)

    def test_a_designed_refusal_is_reported_as_degraded_not_failed(self):
        """`article_redirected_off_pattern` is the product refusing, not an outage.

        t19 verified these are genuine refusals (0 false kills), and the notice is
        still collected. It must stay visible in the report without failing the run.
        """
        self.prepare(website_source())

        code, out, err, summary = self.run_cli(
            fetcher=MappingFetcher(
                {
                    LISTING_URL: FetchResult(LISTING_URL, LISTING, "text/html"),
                    ARTICLE_URL: FetchError("article_redirected_off_pattern"),
                }
            )
        )

        self.assertEqual(code, 0, err)
        collect_stage = self.stage(summary)
        self.assertEqual(collect_stage["failures"], [])
        self.assertEqual(collect_stage["skipped"], [])
        self.assertIn("article_redirected_off_pattern", out, "the code stays visible to the operator")


if __name__ == "__main__":
    unittest.main()
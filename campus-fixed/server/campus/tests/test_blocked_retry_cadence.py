"""A URL the upstream refuses to serve must not be re-requested by every run.

2026-10-21 07:30 unattended run: `article_redirected_off_pattern` appeared for every
mp.weixin.qq.com link the 教务处 columns publish — 33 fetches, all answered with the
anti-scraping challenge page (`/mp/wappoc_appmsgcaptcha`, 「环境异常 / 完成验证后即可继续访问」).
The guard is right to refuse them (see verification/redirect/).

What this file pins is the *cost* of that refusal. The daily command re-selects every
failed item whose `next_retry_at` has passed, and the transient backoff caps at one hour,
so a page that can never be served to this client is requested again on every single run,
for ever. `BLOCKED_BY_UPSTREAM` already names the two codes that mean "this source needs
an authorized channel"; those items back off weekly instead of hourly.

Nothing is accepted and nothing is relaxed: the page is still refused, the error code is
kept, the item stays failed, and the source still reports as blocked.
"""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from server.campus.content import Pipeline
from server.campus.platform import Core, Settings
from server.campus.schema import uid
from server.campus.sources import BLOCKED_BY_UPSTREAM, FetchError, FetchResult
from server.campus.tests.test_pipeline import MappingFetcher

LIST = "https://oaa.tju.edu.cn/cxsj/xsjs.htm"
CHALLENGED = "https://mp.weixin.qq.com/s/CSniYqr1LfSYobIeCZA6nw"
NATIVE_FAILING = "https://oaa.tju.edu.cn/info/1031/2233.htm"
# The real declared pattern of the five 教务处 columns: native articles *and* the
# 公众号 links those columns publish.
PATTERN = r"^https?://oaa\.tju\.edu\.cn/info/\d+/\d+\.htm(?:[?#]|$)|^https://mp\.weixin\.qq\.com/s(?:[/?#]|$)"


class BlockedRetryCadenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name), secret="x" * 48, dev_auth=True)
        self.core = Core(self.settings)
        self.core.pipeline = Pipeline(self.core)
        self.source = dict(
            id=uid(),
            name="天津大学教务处 · 学生竞赛",
            kind="website",
            adapter="website",
            origin_url=LIST,
            source_identity=None,
            enabled=True,
            verification="unverified",
            verification_basis=None,
            health="never_checked",
            last_error_code=None,
            last_checked_at=None,
            last_success_at=None,
            config={"list_selector": "a[href]", "article_url_pattern": PATTERN},
        )
        self.core.store.put("sources", self.source["id"], self.source)
        # Sources.config() reads the declared config from this file, exactly as the
        # repository's own config/campus-sources.json does in production.
        config_path = Path(self.tmp.name) / "sources.json"
        config_path.write_text(
            json.dumps(
                {
                    "sources": [
                        {
                            "id": self.source["id"],
                            "origin_url": LIST,
                            "config": {"list_selector": "a[href]", "article_url_pattern": PATTERN},
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.core.pipeline.sources.config_path = config_path
        listing = f'<a href="{CHALLENGED}">公众号通知</a><a href="{NATIVE_FAILING}">原生通知</a>'
        self.fetcher = MappingFetcher(
            {
                LIST: FetchResult(LIST, listing.encode(), "text/html"),
                CHALLENGED: FetchError("article_redirected_off_pattern"),
                NATIVE_FAILING: FetchError("fetch_transport_failed"),
            }
        )
        self.core.pipeline.sources.fetcher = self.fetcher

    def tearDown(self):
        self.core.store.close()
        self.tmp.cleanup()

    def fetch(self):
        """A source whose health is blocked/failed re-raises its code after the state is saved."""
        try:
            return self.core.pipeline.sources.fetch_source({"source_id": self.source["id"], "job_id": uid()})
        except FetchError:
            return None

    def items(self):
        return self.core.store.get("source_ingestion", self.source["id"])["items"]

    def by_url(self, url):
        return next(item for item in self.items().values() if item["url"] == url)

    def test_blocked_url_backs_off_for_a_week_transient_keeps_the_short_backoff(self):
        self.fetch()
        now = datetime.now(UTC)
        blocked = self.by_url(CHALLENGED)
        transient = self.by_url(NATIVE_FAILING)

        # Still refused, still recorded: the refusal itself does not change.
        self.assertEqual(blocked["status"], "failed")
        self.assertEqual(blocked["error_code"], "article_redirected_off_pattern")
        self.assertEqual(transient["error_code"], "fetch_transport_failed")
        self.assertEqual(len(self.core.store.list("snapshots")), 0)

        blocked_delay = datetime.fromisoformat(blocked["next_retry_at"]) - now
        transient_delay = datetime.fromisoformat(transient["next_retry_at"]) - now
        self.assertGreaterEqual(blocked_delay, timedelta(days=6), "a challenge page is retried hourly for ever")
        self.assertLessEqual(transient_delay, timedelta(hours=2), "a transient outage keeps the short backoff")

    def test_the_next_day_run_does_not_request_the_blocked_url_again(self):
        """The daily command runs 24h later, when an hourly backoff has long expired.

        Only the ingestion clock is moved, so `batch()` sees tomorrow while the
        collector writes its retry stamp with the real clock — exactly the state a
        daily run meets.
        """
        self.fetch()

        class Tomorrow(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(days=1)

        with mock.patch("server.campus.ingestion.datetime", Tomorrow):
            self.fetch()

        requested = [url for url, _ in self.fetcher.calls if url == CHALLENGED]
        transient_requests = [url for url, _ in self.fetcher.calls if url == NATIVE_FAILING]
        self.assertEqual(len(requested), 1, "the daily command re-requested a page that cannot be served")
        self.assertEqual(len(transient_requests), 2, "a transient outage must still be retried tomorrow")
        self.assertEqual(len(self.core.store.list("snapshots")), 0)

    def test_a_fully_blocked_source_is_still_reported_as_blocked(self):
        """Only blocked codes: the documented classification must not regress."""
        self.fetcher.mapping[NATIVE_FAILING] = FetchError("html_access_interstitial")
        self.fetch()
        source = self.core.store.get("sources", self.source["id"])
        self.assertEqual(source["health"], "auth_expired")
        self.assertIn(source["last_error_code"], BLOCKED_BY_UPSTREAM)
        self.assertEqual(len(self.core.store.list("snapshots")), 0)
        self.assertEqual(self.by_url(CHALLENGED)["error_code"], "article_redirected_off_pattern")

    def test_the_blocked_codes_are_the_ones_this_test_covers(self):
        self.assertEqual(
            set(BLOCKED_BY_UPSTREAM),
            {"article_redirected_off_pattern", "html_access_interstitial"},
        )


if __name__ == "__main__":
    unittest.main()
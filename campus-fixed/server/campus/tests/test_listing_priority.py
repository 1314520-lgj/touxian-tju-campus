"""A listing that mixes readable articles with WeChat posts must offer the readable ones first.

Real 教务处 columns list mostly ``mp.weixin.qq.com`` posts next to a few native
``/info/…htm`` articles. A run processes only a handful of items per source, so
when the WeChat posts come first every attempt is refused (anti-scraping
challenge) and the whole source is reported as failed even though it does
publish pages the product can read. Ordering the readable ones first costs
nothing and keeps the WeChat links in the queue for when an authorized channel
exists.
"""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from server.campus.content import Pipeline
from server.campus.platform import Core, Settings
from server.campus.schema import uid
from server.campus.sources import MAX_DOCUMENTS, FetchError, FetchResult, feed_items
from server.campus.tests.test_pipeline import MappingFetcher

PATTERN = r"^https?://oaa\.tju\.edu\.cn/info/\d+/\d+\.htm(?:[?#]|$)|^https://mp\.weixin\.qq\.com/s(?:[/?#]|$)"

LISTING = """<html><body><ul>
<li><a href="https://mp.weixin.qq.com/s/aaa">公众号通知一</a></li>
<li><a href="https://oaa.tju.edu.cn/info/1059/9030.htm">原生通知甲</a></li>
<li><a href="https://mp.weixin.qq.com/s/bbb">公众号通知二</a></li>
<li><a href="https://oaa.tju.edu.cn/info/1059/9000.htm">原生通知乙</a></li>
</ul></body></html>""".encode()

# The real shape of 教务处 · 通知公告 on 2026-10-21: 18 公众号 links and 2 native
# articles, with the native ones near the end of the document order.
LONG_LISTING = (
    "<html><body><ul>"
    + "".join(
        f'<li><a href="https://mp.weixin.qq.com/s/wx{index:02d}">公众号通知 {index}</a></li>' for index in range(12)
    )
    + '<li><a href="https://oaa.tju.edu.cn/info/1059/9030.htm">原生通知甲</a></li>'
    + '<li><a href="https://oaa.tju.edu.cn/info/1059/9000.htm">原生通知乙</a></li>'
    + "</ul></body></html>"
).encode()


def _items():
    result = FetchResult("https://oaa.tju.edu.cn/tzgg.htm", LISTING, "text/html")
    return feed_items(result, "website", {"article_url_pattern": PATTERN, "list_selector": "a[href]"}, 20)


def test_readable_articles_come_before_wechat_posts():
    urls = [item["url"] for item in _items()]
    first_wechat = next((index for index, url in enumerate(urls) if "mp.weixin.qq.com" in url), len(urls))
    native_after = [url for url in urls[first_wechat:] if "oaa.tju.edu.cn/info/" in url]
    assert not native_after, f"a readable article was queued behind a WeChat post: {urls}"


def test_no_item_is_dropped_by_the_ordering():
    """Ordering is a preference, not a filter: WeChat posts stay queued."""
    urls = [item["url"] for item in _items()]
    assert len(urls) == 4
    assert sum("mp.weixin.qq.com" in url for url in urls) == 2
    assert sum("oaa.tju.edu.cn/info/" in url for url in urls) == 2


def test_relative_order_within_a_group_is_kept():
    urls = [item["url"] for item in _items()]
    native = [url for url in urls if "oaa.tju.edu.cn/info/" in url]
    assert native == ["https://oaa.tju.edu.cn/info/1059/9030.htm", "https://oaa.tju.edu.cn/info/1059/9000.htm"]


def test_the_default_budget_still_contains_the_readable_articles():
    """Truncating to the per-run budget must not happen *before* the ordering.

    A run processes at most MAX_DOCUMENTS items per source. Cutting the link list at
    that budget in document order — and only then sorting readable-first — throws away
    the readable articles whenever they sit behind the 公众号 links, which is exactly
    what the five 教务处 columns look like (2–3 native articles behind 15+ WeChat
    links). From the outside that reads as "该栏目同站 0 条".
    """
    result = FetchResult("https://oaa.tju.edu.cn/bszy/tzgg.htm", LONG_LISTING, "text/html")
    items = feed_items(result, "website", {"article_url_pattern": PATTERN, "list_selector": "a[href]"})
    urls = [item["url"] for item in items]
    assert len(urls) == MAX_DOCUMENTS, urls
    native = [url for url in urls if "oaa.tju.edu.cn/info/" in url]
    assert native == ["https://oaa.tju.edu.cn/info/1059/9030.htm", "https://oaa.tju.edu.cn/info/1059/9000.htm"], urls


def test_a_larger_explicit_budget_is_honoured():
    """The collector asks for DISCOVERY_LIMIT + 1 items; that must not be capped."""
    result = FetchResult("https://oaa.tju.edu.cn/bszy/tzgg.htm", LONG_LISTING, "text/html")
    items = feed_items(result, "website", {"article_url_pattern": PATTERN, "list_selector": "a[href]"}, 20)
    assert len(items) == 14


class CollectOrderTests(unittest.TestCase):
    """The per-run budget (MAX_DOCUMENTS) must be spent on the readable articles.

    Measured on the 2026-10-21 data copy: 通知公告 and 学业指导 each queue 15+
    公众号 links next to two or three native articles, and a run of eight items
    never reached the native ones at all — every slot went to a challenge page.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.core = Core(Settings(data_dir=Path(self.tmp.name), secret="x" * 48, dev_auth=True))
        self.core.pipeline = Pipeline(self.core)
        self.source = dict(
            id=uid(),
            name="天津大学教务处 · 通知公告",
            kind="website",
            adapter="website",
            origin_url="https://oaa.tju.edu.cn/bszy/tzgg.htm",
            source_identity=None,
            enabled=True,
            verification="unverified",
            verification_basis=None,
            health="never_checked",
            last_error_code=None,
            last_checked_at=None,
            last_success_at=None,
        )
        self.core.store.put("sources", self.source["id"], self.source)
        config_path = Path(self.tmp.name) / "sources.json"
        config_path.write_text(
            json.dumps(
                {
                    "sources": [
                        {
                            "id": self.source["id"],
                            "origin_url": self.source["origin_url"],
                            "config": {"list_selector": "a[href]", "article_url_pattern": PATTERN},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.core.pipeline.sources.config_path = config_path
        listing = (
            "".join(
                f'<li><a href="https://mp.weixin.qq.com/s/wx{index:02d}">公众号 {index}</a></li>' for index in range(10)
            )
            + '<li><a href="https://oaa.tju.edu.cn/info/1059/9030.htm">原生甲</a></li>'
            + '<li><a href="https://oaa.tju.edu.cn/info/1059/9000.htm">原生乙</a></li>'
        ).encode()
        self.mapping = {
            self.source["origin_url"]: FetchResult(self.source["origin_url"], listing, "text/html"),
        }
        for index in range(10):
            url = f"https://mp.weixin.qq.com/s/wx{index:02d}"
            self.mapping[url] = FetchError("article_redirected_off_pattern")
        for url in ("https://oaa.tju.edu.cn/info/1059/9030.htm", "https://oaa.tju.edu.cn/info/1059/9000.htm"):
            self.mapping[url] = FetchResult(
                url, f"<h1>原生通知</h1><div id='vsb_content'><p>正文</p></div>".encode(), "text/html"
            )
        self.fetcher = MappingFetcher(self.mapping)
        self.core.pipeline.sources.fetcher = self.fetcher

    def tearDown(self):
        self.core.store.close()
        self.tmp.cleanup()

    def seed_known_backlog(self, urls):
        """Reproduce the measured state: these URLs are already queued and failed.

        The real columns reached this state before round 19, so the queue order on
        disk (not the discovery order of this run) decides who gets the budget.
        """
        from server.campus.ingestion import SourceBacklog

        backlog = SourceBacklog(self.core.store)
        _, state = backlog.acquire(self.source["id"])
        backlog.discover(state, [{"url": url, "title": f"已入队 {index}", "published_at": None} for index, url in enumerate(urls)])
        for item in state["items"].values():
            item.update(
                status="failed",
                attempts=1,
                error_code="article_redirected_off_pattern",
                next_retry_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                last_attempt_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            )
        # Expire the seeding lease so the real fetch may acquire the source.
        state["lease_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        backlog.save(self.source["id"], state)

    def test_the_native_articles_are_attempted_within_the_per_run_budget(self):
        # Ten 公众号 links are already queued and failed; the two native articles
        # come last in that queue, exactly as they do in the real columns.
        self.seed_known_backlog(
            [f"https://mp.weixin.qq.com/s/wx{index:02d}" for index in range(10)]
            + ["https://oaa.tju.edu.cn/info/1059/9030.htm", "https://oaa.tju.edu.cn/info/1059/9000.htm"]
        )
        failure = None
        try:
            self.core.pipeline.sources.fetch_source({"source_id": self.source["id"], "job_id": uid()})
        except FetchError as error:
            failure = error.code  # a blocked source re-raises its code after the state is saved
        requested = [url for url, _ in self.fetcher.calls if url not in {self.source["origin_url"]}]
        native = [url for url in requested if "oaa.tju.edu.cn/info/" in url]
        assert native == [
            "https://oaa.tju.edu.cn/info/1059/9030.htm",
            "https://oaa.tju.edu.cn/info/1059/9000.htm",
        ], f"the run spent its budget before reaching the readable articles: {requested} (failure={failure})"
        assert len(requested) <= MAX_DOCUMENTS, requested

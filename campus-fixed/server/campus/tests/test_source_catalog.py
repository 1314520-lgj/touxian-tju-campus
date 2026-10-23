"""Offline catalog guards; these fixtures never establish live collection health."""

import json
import unittest
import uuid
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from server.campus.sources import FetchResult, feed_items

APP = Path(__file__).resolve().parents[3]
LEGACY = {
    "c4e00274-5b08-5d26-9a6d-19aed0d71571": "https://oaa.tju.edu.cn/bszy/tzgg.htm",
    "3ac23043-2575-53d7-8a38-45b8d090b6cc": "https://cic.tju.edu.cn/xwzx/tzgg.htm",
    "ba303189-9801-5add-9b5b-924e8c5575b1": "https://news.tju.edu.cn/xw/zhxw.htm",
}


class SourceCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((APP / "config/campus-sources.json").read_text(encoding="utf-8"))
        cls.sources = cls.catalog["sources"]
        cls.corpus = json.loads((APP / "fixtures/notice-corpus/official-notices.json").read_text(encoding="utf-8"))

    def test_identity_stability_and_legacy_subscription_compatibility(self):
        self.assertGreaterEqual(len(self.sources), 15)
        self.assertEqual(len({s["id"] for s in self.sources}), len(self.sources))
        self.assertEqual(len({s["origin_url"] for s in self.sources}), len(self.sources))
        actual = {s["id"]: s["origin_url"] for s in self.sources}
        for ident, url in LEGACY.items():
            self.assertEqual(actual[ident], url)
        for source in self.sources:
            with self.subTest(source=source["name"]):
                uuid.UUID(source["id"])
                if source["id"] not in LEGACY:
                    self.assertEqual(
                        source["id"], str(uuid.uuid5(uuid.NAMESPACE_URL, "tju-campus-source:" + source["origin_url"]))
                    )
                parsed = urlsplit(source["origin_url"])
                self.assertEqual(parsed.scheme, "https")
                self.assertTrue(parsed.hostname.endswith(".tju.edu.cn"))
                self.assertIsNone(parsed.username)
                self.assertIn(source["origin_url"], source["verification_basis"])
                self.assertNotIn("health", source)
                self.assertNotIn("last_success_at", source)

    def test_listing_patterns_reject_off_domain_links_and_accept_school_article_shapes(self):
        # Deliberate fixtures: URL shapes, not assertions that these article IDs exist.
        for source in self.sources:
            host = urlsplit(source["origin_url"]).hostname
            path = "/info/1111/18052.htm"
            if source["source_identity"] == "job-notices":
                path = "/news/content/id/2730.html"
            elif source["source_identity"] == "job-fairs":
                path = "/recruitment/content/type/2/id/6502.html"
            elif source["source_identity"] == "yx-freshmen":
                path = "/yxkx/bks/202608/t20260815_324689.htm"
            good = "https://" + host + path
            bad = "https://unrelated.example" + path
            misleading = "https://" + host + ".unrelated.example" + path
            html = f'<a href="{bad}">外站</a><a href="{misleading}">相似主机</a><a href="{good}">校内通知</a>'
            result = FetchResult(source["origin_url"], html.encode(), "text/html")
            with self.subTest(source=source["name"]):
                self.assertEqual([i["url"] for i in feed_items(result, "website", source["config"])], [good])

    def test_employment_route_variants_and_explicit_public_account_links(self):
        sources = {s["source_identity"]: s for s in self.sources}
        job = sources["job-notices"]
        urls = ["https://job.tju.edu.cn/News/content?id=2616", "https://job.tju.edu.cn/news/content/id/2730.html"]
        body = "".join(f'<a href="{url}">通知</a>' for url in urls).encode()
        self.assertEqual(
            [i["url"] for i in feed_items(FetchResult(job["origin_url"], body, "text/html"), "website", job["config"])],
            urls,
        )
        fair = sources["job-fairs"]
        urls = [
            "https://job.tju.edu.cn/recruitment/content/type/2/id/6502.html",
            "https://job.tju.edu.cn/recruitment/content/id/6502.html",
        ]
        body = "".join(f'<a href="{url}">招聘会</a>' for url in urls).encode()
        self.assertEqual(
            [
                i["url"]
                for i in feed_items(FetchResult(fair["origin_url"], body, "text/html"), "website", fair["config"])
            ],
            urls,
        )
        oaa = sources["oaa-competitions"]
        allowed = "https://mp.weixin.qq.com/s/example"
        body = f'<a href="{allowed}">公众号原文</a><a href="https://mp.weixin.qq.com.unrelated.example/s/fake">仿冒</a>'.encode()
        self.assertEqual(
            [i["url"] for i in feed_items(FetchResult(oaa["origin_url"], body, "text/html"), "website", oaa["config"])],
            [allowed],
        )

    def test_operator_supplied_domains_round8(self):
        """Round 8: the operator's domain list, each source verified server-side on 2026-10-13."""
        sources = {s["source_identity"]: s for s in self.sources}
        notices = sources["oaa-notices"]
        body = (
            '<a href="https://wx.mp.weixin.qq.com/s/2sX7W0r7Z0w0kF4vY3aZ0g">伪协议</a>'
            '<a href="https://oaa.tju.edu.cn/info/1059/9030.htm">官网原文</a>'
            '<a href="https://mp.weixin.qq.com/s/CSniYqr1LfSYobIeCZA6nw">公众号原文</a>'
        ).encode()
        self.assertEqual(
            [
                i["url"]
                for i in feed_items(FetchResult(notices["origin_url"], body, "text/html"), "website", notices["config"])
            ],
            ["https://oaa.tju.edu.cn/info/1059/9030.htm", "https://mp.weixin.qq.com/s/CSniYqr1LfSYobIeCZA6nw"],
        )
        zs = sources["zs-notices"]
        body = (
            '<a href="https://zs.tju.edu.cn/info/1116/4829.htm">公示</a>'
            '<a href="https://zs.tju.edu.cn/ym21/bkzn/tzgg.htm">栏目自身</a>'
            '<a href="https://zs.tju.edu.cn.unrelated.example/info/1116/4829.htm">仿冒</a>'
        ).encode()
        self.assertEqual(
            [i["url"] for i in feed_items(FetchResult(zs["origin_url"], body, "text/html"), "website", zs["config"])],
            ["https://zs.tju.edu.cn/info/1116/4829.htm"],
        )
        yx = sources["yx-freshmen"]
        body = (
            '<a href="https://yx.tju.edu.cn/yxkx/bks/202608/t20260815_324689.htm">缴费通知</a>'
            '<a href="https://yx.tju.edu.cn/yxkx/bks/202608/">栏目目录</a>'
            '<a href="http://me.tju.edu.cn/">外站</a>'
        ).encode()
        self.assertEqual(
            [i["url"] for i in feed_items(FetchResult(yx["origin_url"], body, "text/html"), "website", yx["config"])],
            ["https://yx.tju.edu.cn/yxkx/bks/202608/t20260815_324689.htm"],
        )

    def test_short_corpus_has_traceable_evidence_and_explicit_unknowns(self):
        items = self.corpus["items"]
        self.assertEqual(len(items), 20)
        self.assertEqual(len({i["source_url"] for i in items}), 20)
        self.assertEqual(len({i["id"] for i in items}), 20)
        for item in items:
            with self.subTest(item=item["id"]):
                self.assertLessEqual(len(item["snippet"]), 25)
                self.assertTrue(urlsplit(item["source_url"]).hostname.endswith(".tju.edu.cn"))
                if item["published_at"]:
                    self.assertIsNotNone(datetime.fromisoformat(item["published_at"]).tzinfo)
                self.assertTrue(item["expected"]["requires_review"])
                self.assertTrue(item["expected"]["reason"])
                self.assertTrue(item["limits"])
                for mention in item["expected"]["date_mentions"]:
                    self.assertIn(mention["text"], item["snippet"])
                    self.assertIn(mention["kind"], ("deadline", "event_time", "date_unknown"))
                    if mention["iso_date"]:
                        date.fromisoformat(mention["iso_date"])
                for material in item["expected"]["materials"]:
                    self.assertIn(material, item["snippet"])
        relative = next(i for i in items if i["id"] == "gs-score-review")
        self.assertEqual(relative["expected"]["date_mentions"], [])
        waiver = next(i for i in items if i["id"] == "gs-retake-waiver")
        self.assertEqual(waiver["expected"]["materials"], [])
        undated = next(i for i in items if i["id"] == "scet-summer")
        self.assertIsNone(undated["published_at"])
        self.assertIsNone(undated["expected"]["date_mentions"][0]["iso_date"])


if __name__ == "__main__":
    unittest.main()

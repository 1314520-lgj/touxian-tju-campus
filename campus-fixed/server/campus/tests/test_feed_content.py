"""Synthetic protocol fixtures; they do not claim real WeChat access."""

import json
import unittest

from server.campus.feed_content import MAX_EMBEDDED_BYTES, MAX_FEED_BYTES, MAX_XML_DEPTH, MAX_XML_NODES, parse_feed
from server.campus.sources import FetchError, FetchResult


def result(body, content_type="application/xml"):
    return FetchResult(
        "https://feed.example/rss", body.encode("utf-8") if isinstance(body, str) else body, content_type
    )


class FeedContentTests(unittest.TestCase):
    def test_wechat_explicit_body_is_preserved(self):
        feed = result(
            '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><link>https://mp.weixin.qq.com/s/fixture</link><title>合成通知</title><content:encoded><![CDATA[<p>请于2026年10月20日前提交申请表。</p>]]></content:encoded></item></channel></rss>'
        )
        parsed = parse_feed(feed, "rss")[0]
        self.assertTrue(parsed["embedded_complete"])
        self.assertIn("提交申请表", parsed["embedded_html"])

    def test_rss_encoded_keeps_html_and_unknown_date(self):
        parsed = parse_feed(
            result(
                '<rss xmlns:c="http://purl.org/rss/1.0/modules/content/"><channel><item><title>A</title><link>/a</link><c:encoded>&lt;p&gt;A &amp;amp; B&lt;/p&gt;</c:encoded></item></channel></rss>'
            ),
            "rsshub",
        )[0]
        self.assertEqual(
            parsed,
            {
                "url": "/a",
                "title": "A",
                "published_at": None,
                "embedded_html": "<p>A &amp; B</p>",
                "embedded_complete": True,
            },
        )

    def test_descriptions_and_summary_never_claim_full_content(self):
        for body in (
            "<rss><channel><item><description><![CDATA[<p>长摘要</p>]]></description></item></channel></rss>",
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><summary type="html">长摘要</summary></entry></feed>',
        ):
            with self.subTest(body=body):
                item = parse_feed(result(body), "werss")[0]
                self.assertFalse(item["embedded_complete"])
                self.assertIsNone(item["embedded_html"])

    def test_atom_alternate_link_and_published_precede_self_and_updated(self):
        body = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><link rel="self" href="https://feed.example/id"/><link rel="alternate" type="text/html" href="https://mp.weixin.qq.com/s/notice"/><title>合成通知</title><published>2026-10-01T08:00:00Z</published><updated>2026-10-02T08:00:00Z</updated><content type="html">&lt;p&gt;合成正文&lt;/p&gt;</content></entry></feed>'
        item = parse_feed(result(body), "rss")[0]
        self.assertEqual(item["url"], "https://mp.weixin.qq.com/s/notice")
        self.assertEqual(item["published_at"], "2026-10-01T08:00:00+00:00")
        self.assertEqual(item["embedded_html"], "<p>合成正文</p>")

    def test_atom_self_link_is_not_an_article_link(self):
        body = '<feed><entry><link rel="self" href="https://feed.example/id"/><content>正文</content></entry></feed>'
        item = parse_feed(result(body), "rss")[0]
        self.assertEqual(item["url"], "")
        self.assertTrue(item["embedded_complete"])

    def test_atom_text_is_escaped_and_xhtml_preserves_links(self):
        body = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><content type="text">3 &lt; 5 &amp; 材料</content></entry><entry><content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>正文 <a href="/attachment.pdf">附件</a></p></div></content></entry></feed>'
        items = parse_feed(result(body), "rss")
        self.assertEqual(items[0]["embedded_html"], "3 &lt; 5 &amp; 材料")
        self.assertIn('<a href="/attachment.pdf">附件</a>', items[1]["embedded_html"])
        self.assertNotIn("ns0:", items[1]["embedded_html"])

    def test_remote_or_binary_atom_content_is_not_embedded(self):
        body = '<feed><entry><content src="https://feed.example/body" type="html"/></entry><entry><content type="application/pdf">YWJj</content></entry></feed>'
        for item in parse_feed(result(body), "rss"):
            self.assertFalse(item["embedded_complete"])
            self.assertIsNone(item["embedded_html"])

    def test_json_explicit_content_html_and_standard_date(self):
        payload = {
            "items": [
                {
                    "url": "https://mp.weixin.qq.com/s/a",
                    "title": "合成",
                    "date_published": "2026-10-01T08:00:00+08:00",
                    "content_html": "<p>完整内容</p>",
                },
                {"link": "/b", "content": "<p>不受支持</p>", "summary": "摘要"},
            ]
        }
        items = parse_feed(result(json.dumps(payload), "application/json"), "authorized_api")
        self.assertTrue(items[0]["embedded_complete"])
        self.assertEqual(items[0]["published_at"], "2026-10-01T08:00:00+08:00")
        self.assertFalse(items[1]["embedded_complete"])
        self.assertIsNone(items[1]["published_at"])

    def test_rss_one_dc_date_supported(self):
        body = '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><item><title>RSS1</title><link>/a</link><dc:date>2026-10-01</dc:date></item></rdf:RDF>'
        self.assertEqual(parse_feed(result(body), "rss")[0]["published_at"], "2026-10-01T00:00:00+08:00")

    def test_content_boundaries_never_silently_truncate(self):
        for size, valid in ((MAX_EMBEDDED_BYTES, True), (MAX_EMBEDDED_BYTES + 1, False)):
            with self.subTest(size=size):
                body = json.dumps([{"url": "/a", "content_html": "a" * size}])
                if valid:
                    self.assertEqual(len(parse_feed(result(body), "authorized_api")[0]["embedded_html"]), size)
                else:
                    with self.assertRaisesRegex(FetchError, "feed_content_too_large"):
                        parse_feed(result(body), "authorized_api")
        body = json.dumps([{"content_html": "中" * (MAX_EMBEDDED_BYTES // 3 + 1)}], ensure_ascii=False)
        with self.assertRaisesRegex(FetchError, "feed_content_too_large"):
            parse_feed(result(body), "authorized_api")

    def test_empty_explicit_body_is_not_complete(self):
        for body in ("", "  ", None):
            self.assertFalse(
                parse_feed(result(json.dumps([{"content_html": body}])), "authorized_api")[0]["embedded_complete"]
            )

    def test_invalid_structures_xml_declarations_and_unknown_adapter_fail(self):
        invalid = [
            ("<!DOCTYPE rss><rss/>", "unsafe_xml"),
            ('<!ENTITY a "x"><rss/>', "unsafe_xml"),
            ("<rss>", "invalid_feed"),
            ("<html/>", "invalid_feed"),
            (
                '<feed><entry><content type="xhtml"><div>wrong namespace</div></content></entry></feed>',
                "invalid_feed_content",
            ),
        ]
        for body, code in invalid:
            with self.subTest(body=body), self.assertRaisesRegex(FetchError, code):
                parse_feed(result(body), "rss")
        for body in ("null", '{"items":{}}', "{"):
            with self.subTest(body=body), self.assertRaisesRegex(FetchError, "invalid_feed"):
                parse_feed(result(body), "authorized_api")
        with self.assertRaisesRegex(FetchError, "invalid_feed_adapter"):
            parse_feed(result("<rss/>"), "website")
        with self.assertRaisesRegex(FetchError, "invalid_feed_content"):
            parse_feed(result('[{"content_html": {}}]'), "authorized_api")

    def test_utf16_xml_cannot_hide_an_entity_declaration(self):
        body = '<!DOCTYPE rss [<!ENTITY secret "hidden">]><rss><channel><item><title>&secret;</title></item></channel></rss>'
        for encoding in ("utf-16-le", "utf-32-le"):
            with self.subTest(encoding=encoding), self.assertRaisesRegex(FetchError, "unsafe_xml"):
                parse_feed(result(body.encode(encoding)), "rss")

    def test_xml_structure_limits_reject_deep_or_wide_feeds(self):
        bodies = (
            "<rss>" + "<node>" * MAX_XML_DEPTH + "</node>" * MAX_XML_DEPTH + "</rss>",
            "<rss>" + "<node/>" * MAX_XML_NODES + "</rss>",
        )
        for body in bodies:
            with self.subTest(size=len(body)), self.assertRaisesRegex(FetchError, "feed_structure_too_large"):
                parse_feed(result(body), "rss")

    def test_item_count_limit_and_total_input_bounds(self):
        items = [{"title": str(i), "content_html": "<p>body</p>"} for i in range(110)]
        feed = result(json.dumps(items))
        self.assertEqual(len(parse_feed(feed, "authorized_api")), 101)
        self.assertEqual(len(parse_feed(feed, "authorized_api", limit=8)), 8)
        for limit in (0, 102, True, "8"):
            with self.subTest(limit=limit), self.assertRaisesRegex(FetchError, "invalid_feed_limit"):
                parse_feed(feed, "authorized_api", limit=limit)
        with self.assertRaisesRegex(FetchError, "body_too_large"):
            parse_feed(result(b"a" * (MAX_FEED_BYTES + 1)), "rss")


if __name__ == "__main__":
    unittest.main()

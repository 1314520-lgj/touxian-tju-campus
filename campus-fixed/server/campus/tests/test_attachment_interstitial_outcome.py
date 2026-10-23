"""A notice whose attachment is behind a captcha is still a collected notice.

The 教务处 columns publish native articles whose body links a `download.jsp`
attachment; that endpoint answers with a download captcha (an input named
`codevalue` plus a `createimage.jsp` image — `attachment_access_restricted`).
The article itself is fetched, parsed and stored, but the *item* used to be
reported as failed, which kept the source permanently "all failed, completed 0"
even though the product had captured the notice. A captcha-gated attachment is
not a transient problem — nothing about it improves by refetching the whole
article every run — so the document completes with the warning kept, while a
genuinely transient attachment failure (network, 5xx) still schedules a retry.

Nothing is accepted that used to be refused: the captcha page is still never
stored as content, and the warning stays on the snapshot.
"""

import json
import tempfile
import unittest
from pathlib import Path

from server.campus.content import Pipeline
from server.campus.platform import Core, Settings
from server.campus.schema import uid
from server.campus.sources import FetchError, FetchResult
from server.campus.tests.test_pipeline import MappingFetcher

LISTING = "https://oaa.tju.edu.cn/bszy/tzgg.htm"
ARTICLE = "https://oaa.tju.edu.cn/info/1059/9030.htm"
ATTACHMENT = (
    "https://oaa.tju.edu.cn/system/_content/download.jsp?urltype=news.DownloadAttachUrl&owner=1&wbfileid=AA"
)
PATTERN = r"^https?://oaa\.tju\.edu\.cn/info/\d+/\d+\.htm(?:[?#]|$)"

LISTING_BODY = f'<a href="{ARTICLE}">原生通知</a>'
ARTICLE_BODY = (
    '<html><head><title>通知</title></head><body><div id="vsb_content">'
    "<h1>2026年暑期剑桥大学移动课堂跨学科学术实践项目宣讲会通知</h1>"
    f'<p>请于9月30日前报名。</p><a href="{ATTACHMENT}">附件下载</a>'
    "</div></body></html>"
)
CAPTCHA_BODY = (
    '<html><body><form><input name="codevalue">'
    '<img src="/system/resource/code/createimage.jsp"></form></body></html>'
)


class AttachmentInterstitialOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name), secret="x" * 48, dev_auth=True)
        self.core = Core(self.settings)
        self.core.pipeline = Pipeline(self.core)
        self.source = dict(
            id=uid(),
            name="天津大学教务处 · 通知公告",
            kind="website",
            adapter="website",
            origin_url=LISTING,
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
                            "origin_url": LISTING,
                            "config": {"list_selector": "a[href]", "article_url_pattern": PATTERN},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.core.pipeline.sources.config_path = config_path
        self.fetcher = MappingFetcher(
            {
                LISTING: FetchResult(LISTING, LISTING_BODY.encode(), "text/html"),
                ARTICLE: FetchResult(ARTICLE, ARTICLE_BODY.encode(), "text/html"),
                ATTACHMENT: FetchResult(ATTACHMENT, CAPTCHA_BODY.encode(), "text/html"),
            }
        )
        self.core.pipeline.sources.fetcher = self.fetcher

    def tearDown(self):
        self.core.store.close()
        self.tmp.cleanup()

    def fetch(self):
        return self.core.pipeline.sources.fetch_source({"source_id": self.source["id"], "job_id": uid()})

    def item(self):
        state = self.core.store.get("source_ingestion", self.source["id"])
        return next(iter(state["items"].values()))

    def test_captcha_gated_attachment_does_not_leave_the_notice_failed(self):
        self.fetch()
        item = self.item()
        self.assertEqual(item["status"], "completed", f"the notice itself was captured: {item}")
        self.assertIsNone(item["error_code"])
        # The source still reports the unfinished attachment (it is not silently clean),
        # but it is no longer "failed" for a notice the product did capture.
        self.assertEqual(self.core.store.get("sources", self.source["id"])["health"], "degraded")

    def test_the_attachment_warning_is_kept_on_the_snapshot(self):
        self.fetch()
        snapshot = next(s for s in self.core.store.list("snapshots") if s["source_url"] == ARTICLE)
        document = self.core.store.get("documents", snapshot["document_id"])
        warnings = document["snapshot_warnings"][snapshot["snapshot_id"]]
        self.assertIn("attachment_access_restricted", warnings)

    def test_a_transient_attachment_failure_still_schedules_a_retry(self):
        self.fetcher.mapping[ATTACHMENT] = FetchError("fetch_transport_failed")
        self.fetch()
        item = self.item()
        self.assertEqual(item["status"], "failed", item)
        self.assertEqual(item["error_code"], "attachment_fetch_fetch_transport_failed")
        self.assertIsNotNone(item["next_retry_at"])


if __name__ == "__main__":
    unittest.main()
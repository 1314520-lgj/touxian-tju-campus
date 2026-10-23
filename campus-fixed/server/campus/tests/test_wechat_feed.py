"""Third-party WeChat feed (WeWe RSS / WeRead route), link mode, end to end.

The product never logs into WeChat and holds no WeChat credential. It reads a
local feed produced by a tool the operator runs; the feed carries the article
body, so this client never requests mp.weixin.qq.com itself (the platform would
answer with an anti-scraping challenge). The item URL stays the real post, so a
stored article can always be checked against the post it came from.
"""

import json

import pytest

from server.campus.content import Pipeline
from server.campus.platform import Core, Settings
from server.campus.schema import uid, validate
from server.campus.sources import FetchError, FetchResult, wechat_feed_sources
from server.campus.tests.test_pipeline import MappingFetcher

FEED_URL = "https://feed.example/wechat.json"
ARTICLE = "https://mp.weixin.qq.com/s/fixture-wechat-1"
BODY = (
    "<h1>2026年下半年全国大学英语四、六级考试报名通知</h1>"
    "<p>全体本科生请于2026年10月30日前完成报名，逾期不再补报。</p>"
)


def feed_bytes(items=None):
    payload = {
        "mp_name": "天津大学教务处",
        "items": items
        if items is not None
        else [
            {
                "url": ARTICLE,
                "title": "2026年下半年全国大学英语四、六级考试报名通知",
                "published_at": "2026-10-20T09:00:00+08:00",
                "content_html": BODY,
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


@pytest.fixture
def campus(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMPUS_WECHAT_FEED_ENABLED", "true")
    monkeypatch.setenv("CAMPUS_WECHAT_FEEDS", "教务处=" + FEED_URL)
    settings = Settings(
        data_dir=tmp_path,
        secret="wechat-feed-test-" * 3,
        dev_auth=True,
        admin_emails={"admin@example.com"},
    )
    core = Core(settings)
    core.pipeline = Pipeline(core)
    try:
        yield core
    finally:
        core.store.close()


def seeded_source(core):
    core.pipeline.sources.seed()
    return next(source for source in core.store.list("sources") if source["kind"] == "wechat")


def fetch(core, source, payload=None):
    fetcher = MappingFetcher({FEED_URL: FetchResult(FEED_URL, feed_bytes(payload), "application/json")})
    core.pipeline.sources.fetcher = fetcher
    return core.pipeline.sources.fetch_source({"source_id": source["id"], "job_id": uid()}), fetcher


def test_seeded_source_is_wechat_feed_and_stays_unverified(campus):
    source = seeded_source(campus)
    assert source["adapter"] == "wechat_feed"
    assert source["source_identity"] == "wechat-mp-via-weread"
    assert source["verification"] == "unverified"
    assert source.get("feed_content_mode", "link") == "link"
    assert "mp.weixin.qq.com" in source["verification_basis"]
    validate("AdminSource", source)


def test_feed_body_becomes_candidate_and_original_url_is_kept(campus):
    source = seeded_source(campus)
    result, fetcher = fetch(campus, source)
    assert len(result["snapshot_ids"]) == 1
    # Only the feed is fetched: this client never requests the post itself.
    assert [url for url, _ in fetcher.calls] == [FEED_URL]

    snapshot = campus.store.get("snapshots", result["snapshot_ids"][0])
    assert snapshot["source_url"] == ARTICLE
    assert snapshot.get("capture") is None

    campus.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
    candidate = campus.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    validate("ExtractionCandidate", candidate)
    assert "四、六级" in candidate["title"]
    assert any(deadline["date"] == "2026-10-30" for deadline in candidate["deadlines"])
    # The body really came from the feed, not from a WeChat fetch.
    assert candidate["review_status"] == "pending"
    assert ARTICLE in json.dumps(candidate, ensure_ascii=False)


def test_feed_title_wins_when_body_has_no_headline(campus):
    """公众号正文常无 <h1>（微信阅读器只给正文），标题必须取 feed 条目标题。

    红测背景：真实 feed 的 59 篇里多数 content_html 没有 <h1>，解析出的 title 段
    会退成账号名（实测「天大学生资助 天大学生资助」），卡片标题因此不可辨识。
    """
    source = seeded_source(campus)
    real_title = "2026年下半年全国大学英语四、六级考试报名通知"
    body_without_headline = (
        '<div class="page" id="readability-page-1">'
        "<p>天大学生资助</p>"
        "<p>全体本科生请于2026年10月30日前完成报名。</p>"
        "</div>"
    )
    result, _ = fetch(
        campus,
        source,
        [{"url": ARTICLE, "title": real_title, "content_html": body_without_headline}],
    )
    snapshot = campus.store.get("snapshots", result["snapshot_ids"][0])
    assert snapshot["title"] == real_title

    parsed = campus.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
    title_segment = next(segment for segment in parsed["segments"] if segment["id"] == "title")
    assert title_segment["text"] == real_title
    assert "title_from_feed" in parsed["warnings"]

    candidate = campus.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    validate("ExtractionCandidate", candidate)
    assert candidate["title"] == real_title
    assert any(deadline["date"] == "2026-10-30" for deadline in candidate["deadlines"])


def test_non_wechat_item_url_is_refused(campus):
    source = seeded_source(campus)
    with pytest.raises(FetchError, match="feed_item_outside_wechat"):
        fetch(campus, source, [{"url": "https://evil.example/s/1", "title": "伪造", "content_html": BODY}])
    assert campus.store.list("snapshots") == []


def test_feed_without_body_is_refused(campus):
    source = seeded_source(campus)
    with pytest.raises(FetchError, match="wechat_feed_requires_fulltext"):
        fetch(campus, source, [{"url": ARTICLE, "title": "只有标题"}])
    assert campus.store.list("snapshots") == []


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CAMPUS_WECHAT_FEED_ENABLED", raising=False)
    monkeypatch.setenv("CAMPUS_WECHAT_FEEDS", "教务处=" + FEED_URL)
    assert wechat_feed_sources() == []


def test_enabled_feed_list_is_parsed(monkeypatch):
    monkeypatch.setenv("CAMPUS_WECHAT_FEED_ENABLED", "true")
    monkeypatch.setenv("CAMPUS_WECHAT_FEEDS", "教务处=" + FEED_URL + ";学工部=https://feed.example/xuegong.json")
    sources = wechat_feed_sources()
    assert [source["origin_url"] for source in sources] == [FEED_URL, "https://feed.example/xuegong.json"]
    assert all(source["kind"] == "wechat" for source in sources)

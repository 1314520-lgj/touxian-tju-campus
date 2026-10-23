"""Authorized feed ingestion against real Store, snapshots and HTTP contracts.

Synthetic feeds explicitly stand in for an operator's authorized provider.
"""

import hashlib
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.campus.content import Pipeline
from server.campus.feed_archive import FeedArchive, original_url
from server.campus.ingestion import SourceBacklog
from server.campus.platform import Core, Settings
from server.campus.schema import uid, validate
from server.campus.sources import FetchError, FetchResult
from server.campus.tests.test_pipeline import MappingFetcher
from server.campus_app import create_app


@pytest.fixture
def campus(tmp_path):
    settings = Settings(
        data_dir=tmp_path, secret="authorized-feed-test-" * 3, dev_auth=True, admin_emails={"admin@example.com"}
    )
    core = Core(settings)
    core.pipeline = Pipeline(core)
    source = dict(
        id=uid(),
        name="合成授权公众号",
        kind="wechat",
        adapter="werss",
        origin_url="https://feed.example/feed.xml",
        source_identity="synthetic-account",
        feed_content_mode="authorized_fulltext",
        enabled=True,
        verification="verified_organization",
        verification_basis="合成测试授权供稿",
        health="never_checked",
        last_error_code=None,
        last_checked_at=None,
        last_success_at=None,
    )
    core.store.put("sources", source["id"], source)
    fetcher = MappingFetcher({})
    core.pipeline.sources.fetcher = fetcher
    yield core, source, fetcher, settings
    core.store.close()


def rss(source, fetcher, count=1, changes=None, summary=False):
    changes = changes or {}
    rows = []
    for i in range(count):
        body = changes.get(i, f"<p>全体本科生请于2026年10月20日前提交申请表。合成记录{i}。</p>")
        field = "description" if summary else "content:encoded"
        rows.append(
            f"<item><title>合成报名通知{i}</title><link>https://mp.weixin.qq.com/s/fixture-{i}</link><pubDate>Thu, 08 Oct 2026 08:00:00 GMT</pubDate><{field}><![CDATA[{body}]]></{field}></item>"
        )
    data = (
        '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>' + "".join(rows) + "</channel></rss>"
    ).encode()
    result = FetchResult(source["origin_url"], data, "application/xml")
    fetcher.mapping[source["origin_url"]] = result
    return result


def fetch(core, source):
    return core.pipeline.sources.fetch_source({"source_id": source["id"], "job_id": uid()})


def test_fulltext_does_not_request_original_and_keeps_verifiable_receipt(campus):
    core, source, fetcher, _ = campus
    listing = rss(source, fetcher)
    result = fetch(core, source)
    assert [url for url, _ in fetcher.calls] == [source["origin_url"]]
    snapshot = core.store.get("snapshots", result["snapshot_ids"][0])
    validate("DocumentSnapshot", snapshot)
    capture = snapshot["capture"]
    assert capture["original_verified"] is False
    assert capture["feed_content_hash"] == hashlib.sha256(listing.body).hexdigest()
    assert capture["feed_origin"] == "https://feed.example"
    receipt = core.store.get("feed_receipts", capture["feed_receipt_id"])
    assert receipt["source_id"] == source["id"]
    core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
    core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    evidence = core.store.list("evidence")
    assert evidence
    assert all(e["capture"] == capture for e in evidence)
    for e in evidence:
        validate("Evidence", e)
    from server.campus.sources import require_snapshot_read

    with pytest.raises(Exception, match="资料尚未发布"):
        require_snapshot_read(core, snapshot["snapshot_id"])


def test_restart_and_transient_listing_outage_replays_private_raw_receipt(campus):
    core, source, fetcher, settings = campus
    rss(source, fetcher, 19)
    fetch(core, source)
    state = core.store.get("source_ingestion", source["id"])
    assert SourceBacklog(core.store).summary(source)["pending_count"] == 11
    assert "embedded_html" not in json.dumps(state)
    assert len(core.store.list("feed_receipts")) == 1
    # Reopen the actual durable Store and a fresh archive cache.
    core.store.close()
    core.store = Core(settings).store
    fetcher.mapping[source["origin_url"]] = FetchError("upstream_http_503")
    fetch(core, source)
    fetch(core, source)
    assert len(core.store.list("snapshots")) == 19
    assert SourceBacklog(core.store).summary(source)["pending_count"] == 0
    assert all(url == source["origin_url"] for url, _ in fetcher.calls)


def test_body_change_beyond_first_batch_is_prioritized_and_receipt_rotation_is_stable(campus):
    core, source, fetcher, _ = campus
    rss(source, fetcher, 19)
    fetch(core, source)
    fetch(core, source)
    fetch(core, source)
    changed = "<p>全体本科生请于2026年10月30日前提交成绩单。合成变更18。</p>"
    rss(source, fetcher, 19, {18: changed})
    result = fetch(core, source)
    assert len(result["snapshot_ids"]) == 1
    newest = core.store.get("snapshots", result["snapshot_ids"][0])
    assert newest["source_url"].endswith("fixture-18")
    assert SourceBacklog(core.store).summary(source)["completed_count"] == 19


def test_publication_correction_creates_new_date_anchor_and_immutable_snapshot(campus):
    core, source, fetcher, _ = campus
    initial = rss(
        source,
        fetcher,
        changes={0: '<p>全体本科生请于明天17:00前提交申请表。</p><div class="date">2027年1月1日活动时间</div>'},
    )
    first = fetch(core, source)["snapshot_ids"][0]
    corrected = initial.body.replace(b"Thu, 08 Oct 2026", b"Fri, 09 Oct 2026")
    fetcher.mapping[source["origin_url"]] = FetchResult(initial.url, corrected, initial.content_type)
    second = fetch(core, source)["snapshot_ids"][0]
    assert first != second
    assert core.store.get("snapshots", first)["published_at"].startswith("2026-10-08")
    assert core.store.get("snapshots", second)["published_at"].startswith("2026-10-09")
    for sid in (first, second):
        core.pipeline.parse_document({"snapshot_id": sid})
    candidate = core.pipeline.extract_candidates({"snapshot_id": second})
    assert candidate["deadlines"][0]["at"].startswith("2026-10-10")


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"verification": "unverified"}, "feed_authorization_not_verified"),
        ({"verification_basis": "   "}, "feed_authorization_not_verified"),
        ({"adapter": "website"}, "feed_mode_requires_feed_adapter"),
    ],
)
def test_authorization_precondition_fails_before_network(campus, changes, code):
    core, source, fetcher, _ = campus
    source.update(changes)
    core.store.put("sources", source["id"], source)
    rss(source, fetcher)
    with pytest.raises(FetchError, match=code):
        fetch(core, source)
    assert fetcher.calls == [] and core.store.list("snapshots") == []


def test_summary_is_not_a_body_and_link_mode_preserves_direct_fetch(campus):
    core, source, fetcher, _ = campus
    rss(source, fetcher, summary=True)
    with pytest.raises(FetchError, match="feed_fulltext_missing"):
        fetch(core, source)
    assert core.store.list("snapshots") == []
    source["feed_content_mode"] = "link"
    core.store.put("sources", source["id"], source)
    url = "https://mp.weixin.qq.com/s/fixture-0"
    fetcher.mapping[url] = FetchResult(url, b"<article>Direct fixture body</article>", "text/html")
    result = fetch(core, source)
    assert fetcher.calls[-1][0] == url
    assert "capture" not in core.store.get("snapshots", result["snapshot_ids"][0])


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://mp.weixin.qq.com/s/test",
        "https://mp.weixin.qq.com.evil.example/test",
        "https://127.0.0.1/test",
        "https://metadata.google.internal/test",
        "https://example.com/test",
    ],
)
def test_wechat_original_identity_is_strict(campus, url):
    _, source, _, _ = campus
    with pytest.raises(FetchError):
        original_url(url, source["origin_url"], source)


def test_feed_attachments_still_use_safe_fetch_without_provider_credentials(campus, monkeypatch):
    core, source, fetcher, _ = campus
    monkeypatch.setenv("CAMPUS_SOURCE_" + source["id"].replace("-", "").upper() + "_TOKEN", "SYNTHETIC")
    rss(
        source,
        fetcher,
        changes={0: '<p>请于10月20日前提交申请表。</p><a href="https://files.example/form.pdf">申请表</a>'},
    )
    fetcher.mapping["https://files.example/form.pdf"] = FetchError("unsafe_address")
    result = fetch(core, source)
    assert result["errors"] == ["unsafe_address"]
    assert fetcher.calls[0][1] == {"Authorization": "Bearer SYNTHETIC"}
    assert fetcher.calls[1] == ("https://files.example/form.pdf", None)
    doc = core.store.list("documents")[0]
    assert "attachment_fetch_unsafe_address" in doc["snapshot_warnings"][result["snapshot_ids"][0]]


def test_cached_receipt_rejects_cross_source_and_redirect_identity_is_distinct(campus):
    core, source, fetcher, _ = campus
    result = rss(source, fetcher)
    from server.campus.feed_content import parse_feed

    items = parse_feed(result, "werss")
    archive = FeedArchive(core)
    archive.record(source, result, items)
    other = {**source, "id": uid()}
    with pytest.raises(FetchError, match="feed_receipt_missing_or_changed"):
        archive.result(other, items[0])
    redirected = FetchResult("https://other-feed.example/path", result.body, result.content_type)
    new_items = parse_feed(redirected, "werss")
    archive.record(source, redirected, new_items)
    assert items[0]["embedded_ref"]["receipt_id"] != new_items[0]["embedded_ref"]["receipt_id"]
    assert len(core.store.list("feed_receipts")) == 2


def test_revoked_authorization_during_fetch_never_commits(campus):
    core, source, fetcher, _ = campus
    rss(source, fetcher)
    original = fetcher.fetch

    def revoked(url, headers=None):
        result = original(url, headers)
        current = core.store.get("sources", source["id"])
        current["verification"] = "unverified"
        core.store.put("sources", source["id"], current)
        return result

    fetcher.fetch = revoked
    with pytest.raises(FetchError, match="source_changed_during_fetch"):
        fetch(core, source)
    assert core.store.list("snapshots") == []


@pytest.mark.parametrize("mode", ["link", "authorized_fulltext"])
def test_public_http_never_receives_feed_credentials_or_fulltext(campus, monkeypatch, mode):
    core, source, fetcher, _ = campus
    source.update(origin_url="http://feed.example/rss", feed_content_mode=mode)
    core.store.put("sources", source["id"], source)
    monkeypatch.setenv("CAMPUS_SOURCE_" + source["id"].replace("-", "").upper() + "_TOKEN", "SYNTHETIC")
    with pytest.raises(FetchError, match="feed_https_required"):
        fetch(core, source)
    assert fetcher.calls == []


def test_safe_fetcher_blocks_cleartext_custom_credentials_before_dns():
    from server.campus.sources import SafeFetcher

    def forbidden(*args, **kwargs):
        raise AssertionError("Must reject before DNS")

    for name in ("Authorization", "Cookie", "X-API-Key"):
        with pytest.raises(FetchError, match="credential_https_required"):
            SafeFetcher(resolver=forbidden).fetch("http://public.example/rss", {name: "SYNTHETIC"})


def test_mode_http_contract_revokes_verification_and_rejects_query_keys(tmp_path):
    app = create_app(
        Settings(
            data_dir=tmp_path, secret="http-feed-contract-" * 3, dev_auth=True, admin_emails={"admin@example.com"}
        ),
        start_worker=False,
    )
    try:
        with TestClient(app) as client:
            challenge = client.post("/api/v1/auth/challenges", json={"email": "admin@example.com"})
            session = client.post(
                "/api/v1/auth/sessions",
                json={"challenge_id": challenge.json()["challenge_id"], "code": challenge.headers["X-Dev-Login-Code"]},
            ).json()
            headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": uid()}
            body = dict(
                name="Synthetic",
                kind="wechat",
                adapter="werss",
                source_identity=None,
                origin_url="https://feed.example/rss",
                feed_content_mode="authorized_fulltext",
            )
            with patch("server.campus.sources.resolve_public"):
                created = client.post("/api/v1/admin/sources", json=body, headers=headers)
                assert created.status_code == 201
                source = created.json()
                validate("AdminSource", source)
                url = "/api/v1/admin/sources/" + source["id"]
                assert (
                    client.post(
                        url + "/verification",
                        json={"verification": "verified_organization", "basis": "合成授权"},
                        headers=headers,
                    ).status_code
                    == 200
                )
                response = client.patch(url, json={"feed_content_mode": "link"}, headers=headers)
                assert response.status_code == 200 and response.json()["verification"] == "unverified"
                assert (
                    client.patch(
                        url, json={"adapter": "website", "feed_content_mode": "authorized_fulltext"}, headers=headers
                    ).status_code
                    == 422
                )
                for key in ("ak", "api%5Fkey", "access_key", "authorization"):
                    secret = client.post(
                        "/api/v1/admin/sources",
                        json={**body, "origin_url": "https://feed.example/rss?" + key + "=SYNTHETIC"},
                        headers={**headers, "Idempotency-Key": uid()},
                    )
                    assert secret.status_code == 422 and secret.json()["code"] == "secret_in_url"
    finally:
        app.state.core.store.close()

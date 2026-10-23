"""Immutable authorized feed receipts; durable backlog holds references, not full HTML."""

from __future__ import annotations

import hashlib
import html
import ipaddress
import uuid
from urllib.parse import urljoin, urlsplit

from .blob_store import BlobError, blob_store
from .ingestion import scope
from .schema import now_iso


def fulltext_enabled(source):
    return source.get("feed_content_mode", "link") == "authorized_fulltext"


def require_feed_authorization(source):
    from .sources import FetchError

    if not source.get("enabled") or not fulltext_enabled(source):
        raise FetchError("feed_mode_not_enabled")
    if source["adapter"] == "website":
        raise FetchError("feed_mode_requires_feed_adapter")
    if source["verification"] == "unverified" or not (source.get("verification_basis") or "").strip():
        raise FetchError("feed_authorization_not_verified")


def original_url(value, base, source):
    from .sources import FetchError, canonical_url

    if not value:
        raise FetchError("feed_original_url_missing")
    url = canonical_url(urljoin(base, value))
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise FetchError("feed_original_https_required")
    if source["kind"] == "wechat" and parsed.hostname != "mp.weixin.qq.com":
        raise FetchError("feed_original_identity_mismatch")
    if parsed.hostname in {"localhost", "metadata.google.internal"} or parsed.hostname.endswith(".localhost"):
        raise FetchError("unsafe_url")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise FetchError("unsafe_url")
    # The original is a provenance link, not a network target. Any attachment
    # request still passes SafeFetcher's independent DNS/TLS/redirect checks.
    return url


class FeedArchive:
    def __init__(self, core):
        self.core = core
        self.cache = {}

    def record(self, source, result, items):
        from .sources import FetchError

        require_feed_authorization(source)
        digest = hashlib.sha256(result.body).hexdigest()
        identity = ":".join((source["id"], scope(source), digest, result.url, result.content_type))
        receipt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "feed-receipt:" + identity))
        key = f"snapshots/{receipt_id}/{digest}.bin"
        try:
            blob_store(self.core).put(key, result.body, digest)
        except BlobError as error:
            raise FetchError(error.code) from None
        receipt = dict(
            id=receipt_id,
            source_id=source["id"],
            scope=scope(source),
            url=result.url,
            content_type=result.content_type,
            adapter=source["adapter"],
            content_hash=digest,
            object_key=key,
            fetched_at=now_iso(),
        )
        with self.core.store.transaction():
            current = self.core.store.get("sources", source["id"])
            if not current or scope(current) != scope(source):
                raise FetchError("source_changed_during_fetch")
            require_feed_authorization(current)
            if not self.core.store.get("feed_receipts", receipt_id):
                self.core.store.put("feed_receipts", receipt_id, receipt)
        self.cache[receipt_id] = (receipt, items)
        for item in items:
            item["embedded_ref"] = (
                dict(receipt_id=receipt_id, html_hash=hashlib.sha256(item["embedded_html"].encode()).hexdigest())
                if item["embedded_complete"]
                else None
            )

    def result(self, source, item):
        from .feed_content import parse_feed
        from .sources import FetchError, FetchResult

        require_feed_authorization(source)
        ref = item.get("embedded_ref")
        if not ref:
            raise FetchError("feed_fulltext_missing")
        receipt_id = ref["receipt_id"]
        cached = self.cache.get(receipt_id)
        if cached is None:
            receipt = self.core.store.get("feed_receipts", receipt_id)
            if not receipt or receipt["source_id"] != source["id"] or receipt["scope"] != scope(source):
                raise FetchError("feed_receipt_missing_or_changed")
            try:
                raw = blob_store(self.core).read(receipt["object_key"], receipt["content_hash"])
            except BlobError as error:
                raise FetchError(error.code) from None
            items = parse_feed(FetchResult(receipt["url"], raw, receipt["content_type"]), receipt["adapter"])
            cached = (receipt, items)
            self.cache[receipt_id] = cached
        receipt, items = cached
        if receipt["source_id"] != source["id"] or receipt["scope"] != scope(source):
            raise FetchError("feed_receipt_missing_or_changed")
        selected = None
        for entry in items:
            if not entry["embedded_complete"]:
                continue
            try:
                url = original_url(entry["url"], receipt["url"], source)
            except FetchError:
                continue
            if url == item["url"] and hashlib.sha256(entry["embedded_html"].encode()).hexdigest() == ref["html_hash"]:
                selected = entry
                break
        if selected is None:
            raise FetchError("feed_entry_receipt_mismatch")
        title = html.escape(selected["title"])
        published = html.escape(selected["published_at"] or "", quote=True)
        meta = f'<meta property="article:published_time" content="{published}">' if published else ""
        body = (
            f'<html><head><meta charset="utf-8">{meta}</head><body><h1>{title}</h1><article>'
            + selected["embedded_html"]
            + "</article></body></html>"
        ).encode("utf-8")
        parsed = urlsplit(receipt["url"])
        capture = dict(
            method="authorized_feed",
            feed_receipt_id=receipt_id,
            feed_content_hash=receipt["content_hash"],
            feed_origin=parsed.scheme + "://" + parsed.netloc,
            original_verified=False,
        )
        return FetchResult(item["url"], body, "text/html; charset=utf-8"), capture

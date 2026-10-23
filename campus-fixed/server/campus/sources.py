"""Public adapters, immutable snapshots and bounded DNS-pinned fetches."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from fastapi import APIRouter, Request

from .blob_store import BlobError, blob_store, public_origin
from .ingestion import DISCOVERY_LIMIT, SourceBacklog
from .platform import APIError, require_user
from .schema import now_iso, uid, validate

MAX_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS, MAX_DOCUMENTS, MAX_ATTACHMENTS = 4, 8, 4
ADAPTERS = {"website", "rss", "rsshub", "werss", "authorized_api", "wechat_feed"}

# --- 微信公众号 feed（微信读书路线）：默认关闭，显式开启 ----------------------
# This product never logs into WeChat and holds no WeChat credential. A third-party
# tool the operator runs (WeWe RSS and relatives) does that part; this product only
# reads the local feed it produces, through the same SafeFetcher and the same source
# registration every other source uses. Two rules keep the evidence chain intact:
#   * a feed item's URL must be the real mp.weixin.qq.com post, so a stored article
#     can always be checked against the post it came from;
#   * the ability stays off until the operator sets the flag, and the sources are
#     seeded unverified, so nothing from a relay can be published without a human.
# The four risks (account containment, third-party forwarding, archived upstream,
# bypassing the platform's normal access path) are written out in
# docs/WECHAT-RSS-SETUP.md and are not softened there.
WECHAT_FEED_ADAPTER = "wechat_feed"
WECHAT_FEED_IDENTITY = "wechat-mp-via-weread"
WECHAT_FEED_ENABLED_ENV = "CAMPUS_WECHAT_FEED_ENABLED"
WECHAT_FEED_SOURCES_ENV = "CAMPUS_WECHAT_FEEDS"
WECHAT_ITEM_HOST = "mp.weixin.qq.com"


def wechat_feed_enabled():
    """Opt-in switch; anything other than an explicit yes means off."""
    return (os.environ.get(WECHAT_FEED_ENABLED_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def wechat_feed_sources():
    """Operator-declared third-party WeChat feeds, read from the environment.

    Declared in the environment rather than config/campus-sources.json on purpose:
    that file is the catalog of *official https origins* (``test_source_catalog``
    pins that shape), while these origins are a local file/HTTP feed of the
    operator's own tool. Nothing here is a WeChat credential — it is a feed address.
    """
    if not wechat_feed_enabled():
        return []
    entries = []
    raw = (os.environ.get(WECHAT_FEED_SOURCES_ENV) or "").replace("\n", ";")
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, url = chunk.partition("=")
        name, url = name.strip(), url.strip()
        if not name or not url.startswith(("http://127.0.0.1", "http://localhost", "https://")):
            continue
        entries.append(
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "tju-wechat-feed:" + url)),
                "name": "微信公众号（微信读书路线）· " + name,
                # Contract enum: the content is WeChat, the transport is a feed.
                "kind": "wechat",
                "origin_url": url,
                "adapter": WECHAT_FEED_ADAPTER,
                "source_identity": WECHAT_FEED_IDENTITY,
                # A relay is never an official origin: unverified keeps every one of
                # its candidates in front of a human (content.py adds the warning).
                "verification": "unverified",
                "verification_basis": (
                    "经第三方微信公众号 RSS（微信读书路线）中转而采：本机 feed " + url
                    + "；条目 URL 必须是真实 mp.weixin.qq.com 原文，正文逐字取自 feed。"
                    "本产品不登录微信、不持有微信凭证。四项风险见 docs/WECHAT-RSS-SETUP.md。"
                ),
            }
        )
    return entries


def _text_or_none(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _wechat_feed_rows(payload):
    """Tolerant mapping of the shapes these tools emit, with the host pinned.

    WeWe RSS and its relatives speak Atom/RSS (handled by ``parse_feed``) and a JSON
    shape close to what the operator relay already writes. Only the fields the
    product needs are read, and any item whose URL is not the real WeChat post is
    refused instead of being stored as if it were one.
    """
    if isinstance(payload, dict):
        items = payload.get("items") or payload.get("data") or payload.get("list")
        account = payload.get("mp_name") or payload.get("mpName") or payload.get("author") or payload.get("title")
    elif isinstance(payload, list):
        items, account = payload, None
    else:
        return []
    if not isinstance(items, list):
        return []
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("link") or item.get("id")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        if (urlsplit(url).hostname or "").lower() != WECHAT_ITEM_HOST:
            raise FetchError("feed_item_outside_wechat")
        body = (
            item.get("content_html")
            or item.get("content")
            or item.get("description_html")
            or item.get("description")
        )
        rows.append(
            {
                "url": url,
                "title": _text_or_none(item.get("title")),
                "published_at": _text_or_none(
                    item.get("published_at") or item.get("pubDate") or item.get("date_published") or item.get("date")
                ),
                "content_html": body if isinstance(body, str) and body.strip() else None,
                "account": _text_or_none(item.get("mp_name") or item.get("mpName") or item.get("author") or account),
            }
        )
    return rows


def wechat_feed_items(result, adapter, config, limit):
    """Items of a third-party WeChat feed: JSON (WeWe RSS style) or Atom/RSS.

    Full text is mandatory here: the post itself cannot be fetched from this client
    (the platform answers with an anti-scraping challenge), so a feed that carries
    only a link is refused rather than quoted from somewhere else.
    """
    if adapter != WECHAT_FEED_ADAPTER:
        raise FetchError("invalid_feed_adapter")
    if not wechat_feed_enabled():
        # Fail closed: the adapter is inert until the operator opts in.
        raise FetchError("wechat_feed_disabled")
    text = decode_body(result.body, result.content_type)
    if text.lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(text)
        except (ValueError, RecursionError):
            raise FetchError("invalid_feed") from None
        rows = _wechat_feed_rows(payload)
    else:
        from .feed_content import parse_feed

        rows = [
            {
                "url": item["url"],
                "title": item.get("title"),
                "published_at": item.get("published_at"),
                "content_html": item.get("content_html"),
                "account": None,
            }
            for item in parse_feed(result, "werss", limit)
        ]
        for row in rows:
            if (urlsplit(row["url"] or "").hostname or "").lower() != WECHAT_ITEM_HOST:
                raise FetchError("feed_item_outside_wechat")
    kept = []
    for row in rows[:limit]:
        if not row.get("content_html"):
            raise FetchError("wechat_feed_requires_fulltext")
        kept.append(row)
    return kept
PUBLIC_FIELDS = (
    "id",
    "name",
    "kind",
    "verification",
    "origin_url",
    "enabled",
    "health",
    "last_success_at",
    "last_checked_at",
)
EXTENSIONS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".doc": "text",
    ".xls": "text",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
}

# A WeChat article link is queued behind the pages the product can actually read
# without an authorized channel (see feed_items).
WECHAT_ARTICLE = re.compile(r"^https?://mp\.weixin\.qq\.com/s(?:[/?#]|$)")

# Upstream refuses to serve the article body to this client (anti-scraping
# challenge, or a link redirected to one). The source itself is reachable: what
# is missing is an authorized channel, so this is not an outage.
BLOCKED_BY_UPSTREAM = frozenset({"article_redirected_off_pattern", "html_access_interstitial"})

# How long a blocked item waits before the collector tries it again. The
# transient backoff caps at one hour, and the daily command re-selects every
# failed item whose next_retry_at has passed, so a challenge page would be
# requested on every single run: the 2026-10-21 07:30 unattended run spent 33
# requests on identical WeChat challenges (`/mp/wappoc_appmsgcaptcha`) linked
# from the 教务处 columns. A blocked answer cannot change within an hour, so
# those items back off for a week instead. Nothing about the refusal changes:
# the page is still rejected, the code is still recorded and the source still
# reports as blocked — only the retry cadence does, and it stays finite so a
# change upstream is noticed within a week.
BLOCKED_RETRY_DELAY = timedelta(days=7)


@dataclass(frozen=True)
class FetchResult:
    url: str
    body: bytes
    content_type: str


class FetchError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def canonical_url(url):
    if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\x00-\x20\\]", url):
        raise FetchError("unsafe_url")
    try:
        p = urlsplit(url)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username is not None or p.password is not None:
            raise FetchError("unsafe_url")
        host = p.hostname.encode("idna").decode("ascii").lower()
        port = p.port or (443 if p.scheme == "https" else 80)
        base = f"[{host}]" if ":" in host else host
        authority = base if port == (443 if p.scheme == "https" else 80) else f"{base}:{port}"
        trusted = [x.rstrip("/") for x in os.environ.get("CAMPUS_TRUSTED_GATEWAY_ORIGINS", "").split(",") if x]
        if port != (443 if p.scheme == "https" else 80) and p.scheme + "://" + authority not in trusted:
            raise FetchError("unsafe_port")
        return urlunsplit((p.scheme, authority, p.path or "/", p.query, ""))
    except (ValueError, UnicodeError):
        raise FetchError("unsafe_url") from None


def resolve_public(url, resolver=socket.getaddrinfo, trusted_origins=()):
    p = urlsplit(canonical_url(url))
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        addresses = {item[4][0] for item in resolver(p.hostname, port, type=socket.SOCK_STREAM)}
    except (OSError, ValueError):
        raise FetchError("dns_failed") from None
    if not addresses:
        raise FetchError("dns_failed")
    trusted = (p.scheme + "://" + p.netloc) in trusted_origins
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            raise FetchError("unsafe_address") from None
        mapped = getattr(address, "ipv4_mapped", None)
        # Exact gateway trust never overrides cloud metadata, including mapped IPv4.
        effective = mapped or address
        metadata = {ipaddress.ip_address("100.100.100.200"), ipaddress.ip_address("fd00:ec2::254")}
        if effective in metadata or effective.is_link_local or effective.is_multicast or effective.is_unspecified:
            raise FetchError("unsafe_address")
        if not effective.is_global and not trusted:
            raise FetchError("unsafe_address")
    return p.hostname, sorted(addresses)[0], port


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self.ip = ip

    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port), self.timeout)


class SafeFetcher:
    """No proxy env, DNS pinning, TLS host validation, per-hop SSRF and byte/time limits."""

    def __init__(
        self, resolver=socket.getaddrinfo, connector=None, max_bytes=MAX_BYTES, timeout=15, trusted_origins=None
    ):
        self.resolver, self.connector, self.max_bytes, self.timeout = resolver, connector, max_bytes, timeout
        raw = os.environ.get("CAMPUS_TRUSTED_GATEWAY_ORIGINS", "") if trusted_origins is None else None
        self.trusted_origins = tuple(trusted_origins or [x.rstrip("/") for x in (raw or "").split(",") if x])

    def fetch(self, url, headers=None):
        current = canonical_url(url)
        initial = (urlsplit(current).scheme, urlsplit(current).netloc)
        if (
            headers
            and any(key.lower() in {"authorization", "cookie", "proxy-authorization", "x-api-key"} for key in headers)
            and initial[0] != "https"
            and initial[0] + "://" + initial[1] not in self.trusted_origins
        ):
            raise FetchError("credential_https_required")
        deadline = time.monotonic() + self.timeout
        for _ in range(MAX_REDIRECTS + 1):
            host, ip, port = resolve_public(current, self.resolver, self.trusted_origins)
            p = urlsplit(current)
            if time.monotonic() > deadline:
                raise FetchError("fetch_timeout")
            factory = self.connector or (_PinnedHTTPS if p.scheme == "https" else _PinnedHTTP)
            conn = factory(host, ip, port, max(1, int(deadline - time.monotonic())))
            request_headers = {"User-Agent": "TJU-Student-Info/1.0", "Accept-Encoding": "identity", "Host": p.netloc}
            if (p.scheme, p.netloc) == initial:
                request_headers.update(headers or {})
            try:
                conn.request("GET", p.path + ("?" + p.query if p.query else ""), headers=request_headers)
                response = conn.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    target = response.getheader("Location")
                    if not target:
                        raise FetchError("redirect_missing_location")
                    redirected = canonical_url(urljoin(current, target))
                    if p.scheme == "https" and urlsplit(redirected).scheme != "https":
                        raise FetchError("redirect_downgrade")
                    current = redirected
                    continue
                if response.status in {401, 403}:
                    raise FetchError("auth_expired")
                if response.status != 200:
                    raise FetchError(f"upstream_http_{response.status}")
                if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
                    raise FetchError("compressed_response_denied")
                length = response.getheader("Content-Length")
                if length and (not length.isdigit() or int(length) > self.max_bytes):
                    raise FetchError("body_too_large")
                chunks = []
                size = 0
                while True:
                    chunk = response.read(min(65536, self.max_bytes + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise FetchError("body_too_large")
                    if time.monotonic() > deadline:
                        raise FetchError("fetch_timeout")
                    chunks.append(chunk)
                return FetchResult(
                    current, b"".join(chunks), response.getheader("Content-Type", "application/octet-stream")
                )
            except FetchError:
                raise
            except (OSError, ssl.SSLError, http.client.HTTPException):
                raise FetchError("fetch_transport_failed") from None
            finally:
                conn.close()
        raise FetchError("too_many_redirects")


def decode_body(body, content_type=""):
    m = re.search(r"charset=([\w-]+)", content_type, re.I)
    for encoding in ([m[1]] if m else []) + ["utf-8-sig", "gb18030"]:
        try:
            return body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    return body.decode("utf-8", errors="replace")


def attachment_access_restricted(body, source_url):
    """Recognize a download captcha by its controls, never by notice wording."""
    if not urlsplit(source_url).path.lower().endswith("/download.jsp"):
        return False
    if len(body) > MAX_BYTES or body.count(b"<") > 100_000:
        return False
    soup = BeautifulSoup(decode_body(body), "html.parser")
    code = any(
        str(node.get("id", "")).lower() == "codevalue" or str(node.get("name", "")).lower() == "codevalue"
        for node in soup.find_all("input")
    )
    challenge = any("createimage.jsp" in str(node.get("src", "")).lower() for node in soup.find_all("img"))
    challenge = challenge or any("createimage.jsp" in node.get_text().lower() for node in soup.find_all("script"))
    return code and challenge


def public_source_url(value):
    """Return an original public provenance URL with credential query fields removed.

    This performs no DNS or network requests: private literal hosts are rejected,
    and the fetcher remains responsible for resolving any actual network target.
    """
    url = canonical_url(value)
    parsed = urlsplit(url)
    host = parsed.hostname.lower()
    if host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local")):
        raise FetchError("private_source")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise FetchError("private_source")
    hidden = {
        "token",
        "access_token",
        "password",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "ak",
        "access_key",
        "secret",
        "key",
        "signature",
        "sig",
        "session",
        "session_id",
        "jwt",
        "codevalue",
    }
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in hidden]
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _reject_redirected_off_pattern(final_url, requested_url, config):
    """Refuse a document that was *redirected* off the declared article pattern.

    A notice link can be answered with an anti-scraping challenge instead of
    the article (WeChat redirects a data-centre request to
    ``/mp/wappoc_appmsgcaptcha``). The landing page is not the notice, so it
    must never become a snapshot; storing it produced candidates titled
    「环境异常」 that only cost the reviewer time.

    Only a *change of address* is judged here. A feed item whose own URL already
    differs from the source pattern (RSS items, non-standard detail paths) is
    left exactly as before, so this rejects more, never fewer.
    """
    pattern = (config or {}).get("article_url_pattern")
    if not pattern or not final_url or not requested_url:
        return
    if canonical_url(final_url) == canonical_url(requested_url):
        return
    if not re.search(pattern, final_url):
        raise FetchError("article_redirected_off_pattern")


def discover_attachments(result):
    """Attachment links from the notice body only.

    Real 天津大学 notice pages put a navigation block on every article whose
    ``download.jsp`` links belong to other notices (125 of them on one real
    page). Scanning every ``<a href>`` queued those instead of the notice's own
    attachment: each answered with a captcha, so one notice produced four
    "access restricted" snapshots that had nothing to do with it. 177 of 363
    stored snapshots were exactly this noise.

    Only the article body counts. A notice whose body links no file has no
    attachment, and that is the honest answer.
    """
    from .content import CONTENT_SELECTORS

    text = decode_body(result.body, result.content_type)
    soup = BeautifulSoup(text, "html.parser")
    root = None
    for selector in CONTENT_SELECTORS:
        try:
            root = soup.select_one(selector)
        except Exception:  # noqa: BLE001 - an unusable selector is not fatal
            root = None
        if root is not None:
            break
    if root is None:
        # No recognized body: fall back to the whole document minus navigation.
        for noise in soup.select("nav,header,footer,aside,[role='navigation'],[role='contentinfo']"):
            noise.decompose()
        root = soup.body or soup
    found = []
    for link in root.select("a[href]"):
        href = urljoin(result.url, link["href"])
        if (
            Path(urlsplit(href).path).suffix.lower() in EXTENSIONS
            or "virtual_attach_file" in href
            or "download.jsp" in href
        ):
            found.append((href, link.get_text(" ", strip=True) or "附件"))
    return list(dict.fromkeys(found))


def _published(raw):
    if not isinstance(raw, str) or not raw.strip():
        return None
    from email.utils import parsedate_to_datetime

    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            value = parsedate_to_datetime(raw)
        except (ValueError, TypeError):
            m = re.search(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?(?:\s+(\d{1,2})[:：](\d{2}))?", raw)
            if not m:
                return None
            try:
                value = datetime(*[int(m[i]) for i in (1, 2, 3)], int(m[4] or 0), int(m[5] or 0))
            except ValueError:
                return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone(timedelta(hours=8)))
    return value.isoformat()


def feed_items(result, adapter, config, limit=MAX_DOCUMENTS):
    if adapter == WECHAT_FEED_ADAPTER:
        return wechat_feed_items(result, adapter, config, limit)
    if adapter in {"authorized_api", "rss", "rsshub", "werss"}:
        from .feed_content import parse_feed

        return [{k: item[k] for k in ("url", "title", "published_at")} for item in parse_feed(result, adapter, limit)]
    text = decode_body(result.body, result.content_type)
    soup = BeautifulSoup(text, "html.parser")
    try:
        links = soup.select(config.get("list_selector", "a[href]"))
    except Exception:
        raise FetchError("invalid_list_selector") from None
    try:
        article_pattern = re.compile(config.get("article_url_pattern", r"/info/\d+/\d+\.htm"))
    except (re.error, TypeError):
        raise FetchError("invalid_article_pattern") from None
    items = []
    seen = set()
    # Collect enough links for the ordering below to mean something: a listing is
    # read in document order, and the readable articles of a 教务处 column sit
    # behind 15+ WeChat links. Cutting at the per-run budget (MAX_DOCUMENTS) first
    # threw those articles away, which from the outside looked like "该栏目同站 0 条".
    scan_limit = max(limit, DISCOVERY_LIMIT)
    for link in links:
        url = urljoin(result.url, link.get("href", ""))
        if not article_pattern.search(url) or url in seen or not link.get_text(" ", strip=True):
            continue
        seen.add(url)
        nearby = link.parent.get_text(" ", strip=True) if link.parent else ""
        items.append(
            {
                "url": url,
                "title": link.get("title") or link.get_text(" ", strip=True)[:300],
                "published_at": _published(nearby),
            }
        )
        if len(items) == scan_limit:
            break
    # A run only processes a handful of items per source. Real 教务处 columns put
    # mostly mp.weixin.qq.com posts next to a few native /info/…htm articles, and
    # an unauthenticated fetch of a WeChat post is refused as an anti-scraping
    # challenge. Queueing the readable articles first is what keeps such a source
    # usable; the WeChat links are kept (stable order inside each group) for when
    # an authorized channel exists.
    items.sort(key=lambda item: WECHAT_ARTICLE.match(item["url"]) is not None)
    return items[:limit]


def content_kind(result):
    kind = EXTENSIONS.get(
        Path(urlsplit(result.url).path).suffix.lower(), "html" if "html" in result.content_type else "text"
    )
    if result.body.startswith(b"%PDF-"):
        return "pdf"
    mime = result.content_type.split(";", 1)[0].lower()
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return "docx"
    if mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return "xlsx"
    if result.body.startswith(b"PK\x03\x04"):
        # Only bounded central-directory metadata is inspected; extraction remains in the sandboxed parser.
        try:
            with zipfile.ZipFile(io.BytesIO(result.body)) as archive:
                entries = archive.infolist()
                if len(entries) <= 2048:
                    names = {entry.filename for entry in entries}
                    if "word/document.xml" in names:
                        return "docx"
                    if "xl/workbook.xml" in names:
                        return "xlsx"
        except (zipfile.BadZipFile, ValueError):
            pass
    return kind


BUILTIN_ORIGINS = {
    "c4e00274-5b08-5d26-9a6d-19aed0d71571": "https://oaa.tju.edu.cn/bszy/tzgg.htm",
    "3ac23043-2575-53d7-8a38-45b8d090b6cc": "https://cic.tju.edu.cn/xwzx/tzgg.htm",
    "ba303189-9801-5add-9b5b-924e8c5575b1": "https://news.tju.edu.cn/xw/zhxw.htm",
    "fc64db2c-93fb-5ff8-ace0-aa0b0bcbe15c": "https://oaa.tju.edu.cn/tzgg/xyzd.htm",
    "56cd6e50-bf3d-5862-b1ce-d7ebaba3d902": "https://oaa.tju.edu.cn/cxsj/xsjs.htm",
    "8bd43bba-7ae5-5a05-8c7e-08aa41dcf700": "https://gs.tju.edu.cn/tzgg/pytz.htm",
    "439b89ae-6232-5ea7-b595-e7dd695c1fb3": "https://gs.tju.edu.cn/tzgg/xwtz.htm",
    "3dd0449a-a81d-5199-a345-9d04a5da1f6e": "https://gs.tju.edu.cn/tzgg/jztz.htm",
    "09ae8359-d53d-5eec-8f90-b976fa6dc011": "https://gs.tju.edu.cn/pygz/pyfa.htm",
    "35bd445b-d3b1-5057-a746-8cd2c08b2be4": "https://gs.tju.edu.cn/pygz/gjjl.htm",
    "c2a586e4-f367-512a-a828-5ca3762b3cb6": "https://job.tju.edu.cn/news/index/cate/1/p/1.html",
    "37e33c84-53a2-5ba9-a793-6f8bd33a46fb": "https://job.tju.edu.cn/recruitment/index.html",
    "22e1eb54-a004-57fb-a806-0b6b711f9ee9": "https://ico.tju.edu.cn/hdyg/xsxmtz.htm",
    "31126592-7bce-514c-a018-6e701349e384": "https://me.tju.edu.cn/xwzx/tzgg.htm",
    "b887b3d5-aec8-5185-9547-9eebc0d32f3b": "https://scet.tju.edu.cn/rcpy/bksjy/tzgg.htm",
    # Round 8: operator-supplied domains verified server-side on 2026-10-13
    # (see verification/operator-sources-2026-10-13.json).
    "ea4fa3a4-90fc-55c4-a5aa-cc6a389cb13d": "https://oaa.tju.edu.cn/tzgg.htm",
    "3434196e-1203-5543-87bb-d71e1c1fc1b2": "https://oaa.tju.edu.cn/tzgg/js_kc.htm",
    "dca313cc-2acc-5d35-98ff-1cbbf4d03452": "https://zs.tju.edu.cn/ym21/bkzn/tzgg.htm",
    "92d2251d-d0cb-5f9d-824d-3b836f8ddf71": "https://yx.tju.edu.cn/",
    # Round 14: the two 教务处 competition columns above publish nothing but
    # WeChat posts, which an unauthenticated fetch cannot read. The same
    # competition notices are published natively by the 新工科教育创新中心,
    # whose secretariat sits inside 教务处 — a readable official source instead
    # of an "authorization required" dead end.
    "5a11fe0a-949d-5beb-9541-fd0771ba7451": "https://eee.tju.edu.cn/tztg/tz.htm",
    # Round 24 (t26): the daily update had almost nothing new to show because the
    # registered columns publish rarely. These four official college columns were
    # picked by fetching 36 college pages off the school's 院系 list and measuring
    # how many dated items each notice list carries (see verification/freshness/).
    # One origin per source stays intact: every host below had no registered column
    # before.
    #
    # The 研究生招生网 (yzb) column is deliberately NOT promoted here even though it
    # is the busiest list of the five: its article pages carry no per-article title
    # (``<title>`` is the site name, there is no ``<h1>``), so its candidates come
    # out named 《天津大学研究生招生网》. Today they all landed in reject/review, but
    # a future-dated 招生 notice would be auto-accepted and shown to students under
    # that name. It stays a trusted-origin declaration in config/ until the listing
    # title is used as a fallback (content.py, a separate task).
    "29d11307-d8cf-542d-b62c-a84efb48f8a8": "https://cse.tju.edu.cn/index/tzgg.htm",
    "0ae23059-38f6-5246-b1d6-e41df2254eb5": "https://science.tju.edu.cn/index/tzgg.htm",
    "db0fd87f-47d9-5bbe-887e-bdedd53baea1": "https://come.tju.edu.cn/xwzx/tzgg.htm",
    "e6eee4aa-def2-51c7-98b7-de86abbe7fb9": "https://tjusee.tju.edu.cn/xwzx/tzgg.htm",
    # Round 25 (t28): the whole official site family was swept instead of four
    # guessed colleges — 36 units off https://www.tju.edu.cn/xy_b_.htm, each unit's
    # own host taken from its introduction page, then every notice-shaped column of
    # each host fetched once and dated (verification/sources/site-inventory.json).
    # Six active columns whose cards show a real notice title are promoted here;
    # the sweep found no other host with an active, registrable column, so these are
    # additions, not replacements. One origin per source stays intact.
    "de389ca7-e9c2-5f3b-abe9-6155f6b39674": "https://marxism.tju.edu.cn/tzgg/tzgg.htm",
    "da9a6991-a3f5-5c94-8b99-6c2b5af46bba": "https://mstu.tju.edu.cn/index/tztg.htm",
    "db549e3c-e846-5e2a-9543-f2ce72716fac": "https://spst.tju.edu.cn/xydt/tzgg.htm",
    "c33979cc-105d-5875-b9ca-b530ad52b904": "https://tjusfl.tju.edu.cn/tzgg.htm",
    "b512cefa-830f-54d7-8cf7-9c5ad9b49107": "https://sdem.tju.edu.cn/tzgg.htm",
    "08068507-c2c7-5275-abff-cce89ddd56e9": "https://synbm.tju.edu.cn/xwzx/tzgg.htm",
    # 2026-10-23 补扫：t28 之后又对 20 台未被清点的主机做了首页发现 + 路径探测，
    # 只有下面 4 个栏目既活跃又能产出可辨识标题（其余 215 个路径 404 / 12 个 502，
    # 或只有新闻稿/党建动态，见 verification/sources/rescan-2026-10-23.json 与
    # home-discover-2026-10-23.json）。
    "86a547df-3823-5630-b4d6-71d8139d4a6b": "https://imas.tju.edu.cn/index/tztg.htm",
    "e2fdcaf7-94bd-5fe0-bc0c-d12eac368759": "https://cs.tju.edu.cn/xwgg/notices.htm",
    "9492bc3a-f059-5209-bdd5-744f3b88761a": "https://scet.tju.edu.cn/xwzx/tzgg.htm",
    "301d600c-2e12-5fe8-a45d-2fd62f24592c": "https://snmc.tju.edu.cn/news/tzgg.htm",
}


def builtin_sources():
    """The registered source catalog (identity-pinned origin per source id).

    Exposed as a function so tests and operators can assert what the product
    will actually register, instead of reading a local variable out of seed().
    """
    config_path = Path(__file__).resolve().parents[2] / "config" / "campus-sources.json"
    if not config_path.exists():
        return []
    entries = []
    for entry in json.loads(config_path.read_text(encoding="utf-8"))["sources"]:
        if entry.get("id") not in BUILTIN_ORIGINS:
            continue
        if entry.get("origin_url") != BUILTIN_ORIGINS[entry["id"]]:
            continue
        if not all(k in entry for k in ("name", "kind", "adapter", "source_identity", "verification_basis")):
            continue
        entries.append(entry)
    return entries


class Sources:
    def __init__(self, core, fetcher=None):
        self.core, self.fetcher = core, fetcher or SafeFetcher()
        self.config_path = Path(__file__).resolve().parents[2] / "config" / "campus-sources.json"

    def seed(self):
        for entry in builtin_sources() + wechat_feed_sources():
            if self.core.store.get("sources", entry["id"]):
                continue
            source = {k: entry[k] for k in ("id", "name", "kind", "origin_url", "adapter", "source_identity")}
            source.update(
                # A WeChat feed is a third-party relay, never an official origin: it is
                # seeded unverified on purpose, so every candidate it produces carries
                # unverified_source_requires_review and a human decides.
                verification=entry.get("verification", "verified_official"),
                enabled=True,
                health="never_checked",
                last_success_at=None,
                last_checked_at=None,
                last_error_code=None,
                verification_basis=entry["verification_basis"],
            )
            validate("AdminSource", source)
            self.core.store.put("sources", source["id"], source)

    def config(self, source):
        config = {}
        if self.config_path.exists():
            entry = next(
                (
                    e
                    for e in json.loads(self.config_path.read_text(encoding="utf-8"))["sources"]
                    if e["id"] == source["id"]
                ),
                None,
            )
            raw_config = (entry or {}).get("config", {})
            if not isinstance(raw_config, dict):
                raise FetchError("invalid_source_config")
            config = dict(raw_config)
        token = os.environ.get("CAMPUS_SOURCE_" + source["id"].replace("-", "").upper() + "_TOKEN")
        if token or source.get("feed_content_mode") == "authorized_fulltext":
            origin = urlsplit(source["origin_url"])
            trusted = getattr(self.fetcher, "trusted_origins", ())
            if origin.scheme != "https" and origin.scheme + "://" + origin.netloc not in trusted:
                raise FetchError("feed_https_required")
        if token:
            config["headers"] = {"Authorization": "Bearer " + token}
        return config

    def persist(self, source, result, title, published_at, parent_id=None, capture=None, content_kind_override=None):
        url = canonical_url(result.url)
        digest = hashlib.sha256(result.body).hexdigest()
        document_id = str(uuid.uuid5(uuid.NAMESPACE_URL, source["id"] + ":" + url))
        capture_kind = ":" + capture["method"] if capture else ""
        snapshot_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, document_id + ":" + digest + ":" + (parent_id or "") + capture_kind)
        )
        existing = self.core.store.get("snapshots", snapshot_id)
        if existing:
            document = self.core.store.get("documents", document_id) or {}
            if document.get("latest_snapshot_id") != snapshot_id:
                document.update(id=document_id, source_id=source["id"], source_url=url, latest_snapshot_id=snapshot_id)
                observations = document.setdefault("snapshot_observations", {})
                observations[snapshot_id] = observations.get(snapshot_id, 1) + 1
                document.setdefault("snapshot_observed_at", {})[snapshot_id] = now_iso()
                self.core.store.put("documents", document_id, document)
                self.core.store.emit(
                    "source.snapshot.created",
                    snapshot_id,
                    1,
                    source_id=source["id"],
                    snapshot_id=snapshot_id,
                    change_kind="updated",
                )
            return existing, False
        kind = content_kind_override or content_kind(result)
        object_key = f"snapshots/{snapshot_id}/{digest}.bin"
        try:
            blob_store(self.core).put(object_key, result.body, digest)
        except BlobError as exc:
            raise FetchError(exc.code) from None
        snapshot = dict(
            document_id=document_id,
            snapshot_id=snapshot_id,
            parent_snapshot_id=parent_id,
            source_id=source["id"],
            external_id=url,
            source_url=url,
            title=title[:300],
            published_at=published_at,
            fetched_at=now_iso(),
            content_hash=digest,
            content_kind=kind,
            object_key=object_key,
            visibility="public",
        )
        if capture:
            snapshot["capture"] = capture
        validate("DocumentSnapshot", snapshot)
        document = self.core.store.get("documents", document_id) or {}
        document.update(id=document_id, source_id=source["id"], source_url=url, latest_snapshot_id=snapshot_id)
        document.setdefault("snapshot_observations", {})[snapshot_id] = 1
        document.setdefault("snapshot_observed_at", {})[snapshot_id] = now_iso()
        self.core.store.put("documents", document_id, document)
        self.core.store.put("snapshots", snapshot_id, snapshot)
        self.core.store.emit(
            "source.snapshot.created",
            snapshot_id,
            1,
            source_id=source["id"],
            snapshot_id=snapshot_id,
            change_kind="created",
        )
        return snapshot, True

    def fetch_source(self, payload):
        backlog = SourceBacklog(self.core.store)
        source, state = backlog.acquire(payload.get("source_id"))
        try:
            return self._fetch_batch(payload, source, state, backlog)
        finally:
            backlog.release(source["id"], state["lease_token"])

    def _fetch_batch(self, payload, source, state, backlog):
        from .feed_archive import FeedArchive, fulltext_enabled, original_url, require_feed_authorization
        from .feed_content import parse_feed

        fulltext = fulltext_enabled(source)
        archive = FeedArchive(self.core)
        created = []
        failures = []
        successes = 0
        try:
            config = self.config(source)
            if source["adapter"] not in ADAPTERS:
                raise FetchError("unsupported_adapter")
            if fulltext:
                require_feed_authorization(source)
            try:
                listing = self.fetcher.fetch(source["origin_url"], config.get("headers"))
                items = (
                    parse_feed(listing, source["adapter"], DISCOVERY_LIMIT + 1)
                    if fulltext
                    else feed_items(listing, source["adapter"], config, DISCOVERY_LIMIT + 1)
                )
                if not items:
                    raise FetchError("empty_feed_or_layout_changed")
                if fulltext:
                    archive.record(source, listing, items)
                discovered = []
                for item in items[:DISCOVERY_LIMIT]:
                    try:
                        if not item["url"]:
                            raise FetchError("feed_original_url_missing")
                        item["url"] = (
                            original_url(item["url"], listing.url, source)
                            if fulltext
                            else canonical_url(urljoin(listing.url, item["url"]))
                        )
                        fields = (
                            ("url", "title", "published_at", "embedded_ref")
                            if fulltext
                            else ("url", "title", "published_at", "content_html", "account")
                            if source["adapter"] == WECHAT_FEED_ADAPTER
                            else ("url", "title", "published_at")
                        )
                        # An absent optional key must not drop the item: keep only the
                        # keys the item actually carries (the backlog stores what it is
                        # given, and the body is what makes the feed quota worth using).
                        discovered.append({k: item[k] for k in fields if k in item})
                    except FetchError as error:
                        failures.append(error.code)
                backlog.discover(state, discovered, truncated=len(items) > DISCOVERY_LIMIT)
            except FetchError as error:
                transient = error.code in {
                    "fetch_timeout",
                    "fetch_transport_failed",
                    "dns_failed",
                    "empty_feed_or_layout_changed",
                } or bool(re.fullmatch(r"upstream_http_5\d\d", error.code))
                if not state["items"] or not transient:
                    raise
                # Previously discovered URLs stay useful during a transient index outage.
                failures.append(error.code)
                backlog.listing_failure(state)
            # A run processes only a handful of items per source, and a 教务处 column
            # queues 15+ 公众号 links next to its two or three native articles. The
            # backlog caps the batch *before* anything can be ordered, so a few runs'
            # worth of candidates are pulled and the readable articles are processed
            # first — measured on the 2026-10-21 copy, 通知公告 and 学业指导 reached
            # their native articles only after their 公众号 queue was deferred.
            candidates = sorted(
                backlog.batch(state, MAX_DOCUMENTS * 4),
                key=lambda entry: WECHAT_ARTICLE.match(entry["url"]) is not None,
            )
            for item in candidates[:MAX_DOCUMENTS]:
                try:
                    if fulltext:
                        result, capture = archive.result(source, item)
                    elif item.get("content_html"):
                        # A feed that already carries the article body (WeWe RSS
                        # fulltext mode). The payload is used verbatim and the item
                        # URL stays the original post, so the evidence chain points at
                        # mp.weixin.qq.com even though this client never fetched it.
                        result, capture = (
                            FetchResult(item["url"], item["content_html"].encode("utf-8"), "text/html"),
                            None,
                        )
                    else:
                        result, capture = self.fetcher.fetch(item["url"]), None
                    _reject_redirected_off_pattern(result.url, item["url"], config)
                    title, published, attachments = item["title"], item["published_at"], []
                    if "html" in result.content_type:
                        soup = BeautifulSoup(decode_body(result.body, result.content_type), "html.parser")
                        heading = soup.select_one("h1")
                        if heading and heading.get_text(strip=True):
                            title = heading.get_text(" ", strip=True)
                        if not fulltext:
                            meta = soup.select_one('meta[property="article:published_time"],meta[name="pubdate"]')
                            if meta:
                                published = _published(meta.get("content")) or published
                            for node in soup.select(".info, .date, .time, .arti-metas, .news-time")[:5]:
                                published = _published(node.get_text(" ", strip=True)) or published
                        attachments.extend(discover_attachments(result))
                    fetched = []
                    attachment_errors = []
                    attachments = list(dict.fromkeys(attachments))
                    if len(attachments) > MAX_ATTACHMENTS:
                        attachment_errors.append("attachment_limit_reached")
                    for attach_url, attach_title in attachments[:MAX_ATTACHMENTS]:
                        try:
                            attachment = self.fetcher.fetch(attach_url)
                            fetched.append((attachment, attach_title))
                            if attachment_access_restricted(attachment.body, attachment.url):
                                failures.append("attachment_access_restricted")
                                attachment_errors.append("attachment_access_restricted")
                        except FetchError as error:
                            failures.append(error.code)
                            attachment_errors.append("attachment_fetch_" + error.code)
                    # All I/O happens before committing snapshots and outbox atomically.
                    committed_snapshots = []
                    with self.core.store.transaction():
                        current_source = backlog.check(source["id"], state)
                        if fulltext:
                            require_feed_authorization(current_source)
                        snapshot, is_new = self.persist(source, result, title, published, capture=capture)
                        if is_new:
                            committed_snapshots.append(snapshot["snapshot_id"])
                        active = []
                        for attach, attach_title in fetched:
                            child, child_new = self.persist(
                                source, attach, attach_title, published, snapshot["snapshot_id"]
                            )
                            active.append(child["snapshot_id"])
                            if child_new:
                                committed_snapshots.append(child["snapshot_id"])
                        doc = self.core.store.get("documents", snapshot["document_id"])
                        previous_warnings = doc.get("snapshot_warnings", {}).get(snapshot["snapshot_id"], [])
                        previous_active = doc.get("snapshot_attachments", {}).get(snapshot["snapshot_id"])
                        # Recovery replaces ingestion warnings; immutable snapshot bytes stay unchanged.
                        retained = [
                            w
                            for w in previous_warnings
                            if not w.startswith("attachment_fetch_")
                            and w not in {"attachment_limit_reached", "attachment_access_restricted"}
                        ]
                        warnings = sorted(set(retained + attachment_errors))
                        doc.setdefault("snapshot_warnings", {})[snapshot["snapshot_id"]] = warnings
                        doc.setdefault("snapshot_attachments", {})[snapshot["snapshot_id"]] = active
                        self.core.store.put("documents", snapshot["document_id"], doc)
                        if not is_new and (
                            warnings != previous_warnings
                            or previous_active is None
                            or set(active) != set(previous_active)
                        ):
                            self.core.store.emit(
                                "source.snapshot.created",
                                snapshot["snapshot_id"],
                                1,
                                source_id=source["id"],
                                snapshot_id=snapshot["snapshot_id"],
                                change_kind="updated",
                            )
                    created.extend(committed_snapshots)
                    # A captcha-gated attachment is an access interstitial, not a
                    # hiccup: the notice itself was captured and is complete, and
                    # the warning on the snapshot records what could not be had.
                    # Failing the item here reported a captured notice as a failed
                    # source on every run (the five 教务处 columns: "all failed,
                    # completed 0" while their native articles were in the store).
                    retry_error = next(
                        (w for w in attachment_errors if w.startswith("attachment_fetch_")),
                        None,
                    )
                    backlog.outcome(item, retry_error)
                    successes += 1
                except (FetchError, ValueError) as error:
                    code = getattr(error, "code", "invalid_feed_item")
                    failures.append(code)
                    backlog.outcome(item, code)
                    if code in BLOCKED_BY_UPSTREAM:
                        # An access challenge is not a transient hiccup: keep the
                        # refusal, but stop re-requesting the same page every run.
                        item["next_retry_at"] = (datetime.now(UTC) + BLOCKED_RETRY_DELAY).isoformat()
            backlog.finish(source["id"], state, payload.get("correlation_id"))
        except (FetchError, ValueError, KeyError) as error:
            code = getattr(error, "code", "invalid_feed")
            failures.append(code)
        with self.core.store.transaction():
            # Keep concurrent administrator edits and reject stale discovery after an identity change.
            source = backlog.check(source["id"], state)
            backlog.save(source["id"], state)
            remaining = [v["error_code"] for v in state["items"].values() if v["status"] == "failed"]
            errors = failures or remaining
            source.update(
                last_checked_at=now_iso(),
                health="degraded" if errors else "healthy",
                last_error_code=errors[0] if errors else None,
            )
            if successes:
                source["last_success_at"] = now_iso()
            if failures and not successes:
                # Zero documents has two very different causes. A dead host or a
                # rejected address is an outage ("failed"). An upstream that
                # answers every article with an anti-scraping challenge is a
                # source needing an authorized channel, which the frozen
                # "auth_expired" value already names — reporting it as "failed"
                # made a healthy deployment look broken on every daily run.
                blocked = set(failures) <= BLOCKED_BY_UPSTREAM
                source["health"] = "auth_expired" if ("auth_expired" in failures or blocked) else "failed"
            self.core.store.put("sources", source["id"], source)
            run_id = payload.get("job_id") or uid()
            self.core.store.put(
                "fetch_runs",
                run_id,
                {
                    "id": run_id,
                    "source_id": source["id"],
                    "finished_at": now_iso(),
                    "snapshot_ids": created,
                    "error_codes": failures,
                },
            )
        if source["health"] in {"failed", "auth_expired"}:
            raise FetchError(source["last_error_code"] or "source_fetch_failed")
        return {"snapshot_ids": created, "errors": failures}


def _get(core, collection, key):
    value = core.store.get(collection, key)
    if value is None:
        raise APIError(404, "not_found", "资源不存在")
    return value


def _page(values, cursor=None, limit=20):
    if not 1 <= limit <= 100:
        raise APIError(422, "invalid_limit", "limit 应在 1 到 100 之间")
    try:
        offset = int(cursor or "0")
        if offset < 0:
            raise ValueError()
    except ValueError:
        raise APIError(400, "invalid_cursor", "分页游标无效") from None
    return {
        "items": values[offset : offset + limit],
        "next_cursor": str(offset + limit) if offset + limit < len(values) else None,
    }


def _idempotency(request):
    key = request.headers.get("Idempotency-Key", "")
    if not 1 <= len(key) <= 200:
        raise APIError(422, "idempotency_required", "请提供 Idempotency-Key")
    return key


def url_has_secret(url):
    names = {
        "token",
        "access_token",
        "password",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "ak",
        "access_key",
        "secret",
        "key",
    }
    try:
        return any(name.lower() in names for name, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True))
    except ValueError:
        return False  # canonical_url reports malformed URLs consistently.


def validate_feed_mode(value):
    if value.get("feed_content_mode", "link") == "authorized_fulltext" and value.get("adapter") == "website":
        raise APIError(422, "feed_mode_requires_feed_adapter", "订阅正文模式需要 RSS 或授权 API 适配器")


def require_snapshot_read(core, snapshot_id):
    snapshot = _get(core, "snapshots", snapshot_id)
    if not getattr(core, "events", None) or not core.events.can_read_snapshot(snapshot_id):
        raise APIError(404, "not_found", "该资料尚未发布或已撤回")
    return snapshot


def make_router(core):
    router = APIRouter()

    @router.post("/admin/snapshots/{snapshot_id}/replacement", status_code=201)
    async def replacement(snapshot_id: str, request: Request):
        user = require_user(request, roles={"admin", "operator"}, write=True)
        from .authorized_upload import replace_attachment

        return await replace_attachment(core, snapshot_id, request, user)

    @router.get("/sources")
    def public_sources(cursor: str | None = None, limit: int = 20):
        return _page(
            [{k: s[k] for k in PUBLIC_FIELDS} for s in core.store.list("sources") if s["enabled"]], cursor, limit
        )

    @router.get("/admin/sources")
    def admin_sources(request: Request, cursor: str | None = None, limit: int = 20):
        require_user(request, roles={"admin", "operator"})
        page = _page(core.store.list("sources"), cursor, limit)
        backlog = SourceBacklog(core.store)
        for source in page["items"]:
            summary = backlog.summary(source)
            if summary:
                source["ingestion"] = summary
        return page

    @router.post("/admin/sources", status_code=201)
    async def create_source(request: Request):
        user = require_user(request, roles={"admin"}, write=True)
        body = await request.json()
        validate("SourceCreate", body)
        if url_has_secret(body["origin_url"]):
            raise APIError(422, "secret_in_url", "凭据需使用服务端环境变量，不可写入来源地址")
        if body["adapter"] not in ADAPTERS:
            raise APIError(422, "unsupported_adapter", "不支持此采集适配器")
        validate_feed_mode(body)
        try:
            body["origin_url"] = canonical_url(body["origin_url"])
            resolve_public(
                body["origin_url"], trusted_origins=getattr(core.pipeline.sources.fetcher, "trusted_origins", ())
            )
        except FetchError as error:
            raise APIError(422, error.code, "仅支持公开 HTTP/HTTPS 来源") from None
        idem = hashlib.sha256((user["id"] + ":source:create:" + _idempotency(request)).encode()).hexdigest()
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with core.store.transaction():
            existing = core.store.get("idempotency", idem)
            if existing:
                if existing["body_hash"] != digest:
                    raise APIError(409, "idempotency_conflict", "相同幂等键对应不同内容")
                return _get(core, "sources", existing["source_id"])
            source = {
                "id": uid(),
                "feed_content_mode": "link",
                **body,
                "verification": "unverified",
                "enabled": True,
                "health": "never_checked",
                "last_success_at": None,
                "last_checked_at": None,
                "verification_basis": None,
                "last_error_code": None,
            }
            validate("AdminSource", source)
            core.store.put("sources", source["id"], source)
            core.store.put("idempotency", idem, {"source_id": source["id"], "body_hash": digest})
        return source

    @router.patch("/admin/sources/{source_id}")
    async def update_source(source_id: str, request: Request):
        require_user(request, roles={"admin"}, write=True)
        body = await request.json()
        validate("SourcePatch", body)
        if "adapter" in body and body["adapter"] not in ADAPTERS:
            raise APIError(422, "unsupported_adapter", "不支持此采集适配器")
        if "origin_url" in body:
            if url_has_secret(body["origin_url"]):
                raise APIError(422, "secret_in_url", "凭据需使用服务端环境变量")
            try:
                body["origin_url"] = canonical_url(body["origin_url"])
                resolve_public(
                    body["origin_url"], trusted_origins=getattr(core.pipeline.sources.fetcher, "trusted_origins", ())
                )
            except FetchError as error:
                raise APIError(422, error.code, "来源地址不安全") from None
        with core.store.transaction():
            source = _get(core, "sources", source_id)
            validate_feed_mode({**source, **body})
            if any(
                k in body and body[k] != source.get(k, "link" if k == "feed_content_mode" else None)
                for k in ("origin_url", "adapter", "source_identity", "feed_content_mode")
            ):
                source.update(
                    verification="unverified", verification_basis=None, health="never_checked", last_error_code=None
                )
            source.update(body)
            validate("AdminSource", source)
            return core.store.put("sources", source_id, source)

    @router.post("/admin/sources/{source_id}/verification")
    async def verify_source(source_id: str, request: Request):
        require_user(request, roles={"admin"}, write=True)
        body = await request.json()
        validate("SourceVerification", body)
        with core.store.transaction():
            source = _get(core, "sources", source_id)
            source.update(verification=body["verification"], verification_basis=body["basis"])
            return core.store.put("sources", source_id, source)

    @router.post("/admin/sources/{source_id}/fetch", status_code=202)
    def fetch_source(source_id: str, request: Request):
        user = require_user(request, roles={"admin", "operator"}, write=True)
        if not _get(core, "sources", source_id)["enabled"]:
            raise APIError(409, "source_disabled", "此来源已停用")
        return core.store.enqueue(
            "fetch_source", {"source_id": source_id}, f"fetch:{user['id']}:{source_id}:{_idempotency(request)}"
        )

    @router.post("/internal/changes", status_code=202)
    async def change_hint(request: Request):
        secret = os.environ.get("CAMPUS_CHANGE_TOKEN", "")
        if len(secret) < 32 or not hmac.compare_digest(
            request.headers.get("Authorization", "").encode(), ("Bearer " + secret).encode()
        ):
            raise APIError(401, "invalid_service_token", "服务凭据无效")
        body = await request.json()
        validate("ChangeHint", body)
        source = _get(core, "sources", body["source_id"])
        if not source["enabled"]:
            raise APIError(409, "source_disabled", "此来源已停用")
        try:
            hinted = canonical_url(body["source_url"])
        except FetchError:
            raise APIError(422, "unsafe_url", "变更提示地址无效") from None
        if urlsplit(hinted).netloc != urlsplit(source["origin_url"]).netloc:
            raise APIError(422, "source_mismatch", "变更提示与来源不匹配")
        key = hashlib.sha256(
            (source["id"] + ":" + body["provider_change_id"] + ":" + _idempotency(request)).encode()
        ).hexdigest()
        return core.store.enqueue("fetch_source", {"source_id": source["id"]}, "change:" + key)

    def signature(snapshot_id, expiry):
        return hmac.new(
            str(core.settings.secret).encode(), f"{snapshot_id}:{expiry}".encode(), hashlib.sha256
        ).hexdigest()

    @router.get("/snapshots/{snapshot_id}")
    def read_snapshot(snapshot_id: str, request: Request):
        snapshot = require_snapshot_read(core, snapshot_id)
        expiry = int(time.time()) + 300
        token = str(expiry) + "." + signature(snapshot_id, expiry)
        url = public_origin(request) + f"/api/v1/snapshots/{snapshot_id}/file?" + urlencode({"read_token": token})
        return {"snapshot": snapshot, "signed_url": url, "expires_at": datetime.fromtimestamp(expiry, UTC).isoformat()}

    @router.get("/snapshots/{snapshot_id}/file")
    def snapshot_file(snapshot_id: str, read_token: str):
        snapshot = require_snapshot_read(core, snapshot_id)
        try:
            raw, supplied = read_token.split(".", 1)
            expiry = int(raw)
            if (
                expiry < int(time.time())
                or expiry > int(time.time()) + 301
                or not hmac.compare_digest(supplied.encode(), signature(snapshot_id, expiry).encode())
            ):
                raise ValueError()
        except ValueError:
            raise APIError(403, "invalid_read_token", "资料链接已过期或无效") from None
        try:
            return blob_store(core).response(
                snapshot,
                {
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "sandbox",
                    "Cache-Control": "no-store",
                },
            )
        except BlobError as exc:
            raise APIError(404 if exc.code == "snapshot_file_missing" else 503, exc.code, "资料文件不可用") from None

    def admin_signature(snapshot_id, actor_id, expiry):
        return hmac.new(
            str(core.settings.secret).encode(), f"admin:{snapshot_id}:{actor_id}:{expiry}".encode(), hashlib.sha256
        ).hexdigest()

    @router.get("/admin/snapshots/{snapshot_id}")
    def admin_snapshot(snapshot_id: str, request: Request):
        user = require_user(request, roles={"admin", "operator"})
        snapshot = _get(core, "snapshots", snapshot_id)
        expiry = int(time.time()) + 300
        token = str(expiry) + "." + admin_signature(snapshot_id, user["id"], expiry)
        url = public_origin(request) + f"/api/v1/admin/snapshots/{snapshot_id}/file?" + urlencode({"read_token": token})
        return {"snapshot": snapshot, "signed_url": url, "expires_at": datetime.fromtimestamp(expiry, UTC).isoformat()}

    @router.get("/admin/snapshots/{snapshot_id}/file")
    def admin_file(snapshot_id: str, read_token: str, request: Request):
        user = require_user(request, roles={"admin", "operator"})
        snapshot = _get(core, "snapshots", snapshot_id)
        try:
            raw, supplied = read_token.split(".", 1)
            expiry = int(raw)
            if (
                expiry < int(time.time())
                or expiry > int(time.time()) + 301
                or not hmac.compare_digest(supplied.encode(), admin_signature(snapshot_id, user["id"], expiry).encode())
            ):
                raise ValueError()
        except ValueError:
            raise APIError(403, "invalid_read_token", "资料链接已过期或不属于当前审核账号") from None
        try:
            return blob_store(core).response(
                snapshot,
                {
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "sandbox",
                    "Cache-Control": "no-store",
                },
            )
        except BlobError as exc:
            raise APIError(404 if exc.code == "snapshot_file_missing" else 503, exc.code, "资料文件不可用") from None

    return router

"""Bounded protocol parsing for operator-authorized RSS/Atom/JSON feeds.

An explicit body field is a format declaration, not proof of authorization or
publisher identity. The caller owns those checks and any decision to use it.
"""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from itertools import islice

MAX_FEED_BYTES = 8 * 1024 * 1024
MAX_FEED_ITEMS = 101
MAX_EMBEDDED_BYTES = 128 * 1024
MAX_XML_DEPTH = 64
MAX_XML_NODES = 20000
ATOM = "http://www.w3.org/2005/Atom"
XHTML = "http://www.w3.org/1999/xhtml"
RSS_CONTENT = "http://purl.org/rss/1.0/modules/content/"
RSS_ONE = "http://purl.org/rss/1.0/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


def _body(value):
    from .sources import FetchError

    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise FetchError("invalid_feed_content")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise FetchError("invalid_feed_content") from None
    if size > MAX_EMBEDDED_BYTES:
        raise FetchError("feed_content_too_large")
    return value if value.strip() else None


def _text(node):
    return "".join(node.itertext()) if node is not None else ""


def _xml_root(text):
    from .sources import FetchError

    parser = ET.XMLPullParser(events=("start", "end"))
    root = None
    depth = count = 0
    try:
        # Consume events after each small chunk, before an unbounded tree grows.
        for offset in range(0, len(text), 16384):
            parser.feed(text[offset : offset + 16384])
            for event, node in parser.read_events():
                if event == "start":
                    depth += 1
                    count += 1
                    if root is None:
                        root = node
                    if depth > MAX_XML_DEPTH or count > MAX_XML_NODES:
                        raise FetchError("feed_structure_too_large")
                else:
                    depth -= 1
        parser.close()
    except (ET.ParseError, ValueError, RecursionError):
        raise FetchError("invalid_feed") from None
    if root is None:
        raise FetchError("invalid_feed")
    return root


def _row(url, title, published, body=None):
    from .sources import _published

    embedded = _body(body)
    return {
        "url": url if isinstance(url, str) else "",
        "title": (title if isinstance(title, str) and title.strip() else "未命名公告")[:300],
        "published_at": _published(published),
        "embedded_html": embedded,
        "embedded_complete": embedded is not None,
    }


def _atom_body(node):
    from .sources import FetchError

    if node is None or node.get("src"):
        return None
    kind = node.get("type", "text").lower()
    if kind in {"html", "text/html"}:
        if len(node):
            raise FetchError("invalid_feed_content")
        return node.text
    if kind in {"text", "text/plain"}:
        if len(node):
            raise FetchError("invalid_feed_content")
        raw = _body(node.text)
        return html.escape(raw) if raw is not None else None
    if kind in {"xhtml", "application/xhtml+xml"}:
        # Atom XHTML is one XHTML div, not a remote or arbitrary XML document.
        if len(node) != 1 or node[0].tag != "{" + XHTML + "}div":
            raise FetchError("invalid_feed_content")
        wrapper = node[0]
        for child in wrapper.iter():
            if not isinstance(child.tag, str) or not child.tag.startswith("{" + XHTML + "}"):
                raise FetchError("invalid_feed_content")
            child.tag = child.tag.split("}", 1)[1]
        return ET.tostring(wrapper, encoding="unicode", method="html")
    return None


def _atom_rows(root, namespace, limit):
    prefix = "{" + namespace + "}" if namespace else ""
    rows = []
    for node in islice(root.findall(prefix + "entry"), limit):
        links = node.findall(prefix + "link")
        link = next(
            (
                link
                for link in links
                if link.get("rel", "alternate") == "alternate"
                and link.get("href")
                and link.get("type", "text/html") in {"text/html", "application/xhtml+xml"}
            ),
            None,
        )
        if link is None:
            link = next(
                (link for link in links if link.get("rel", "alternate") == "alternate" and link.get("href")), None
            )
        date = node.find(prefix + "published")
        if date is None:
            date = node.find(prefix + "updated")
        rows.append(
            _row(
                link.get("href", "") if link is not None else "",
                _text(node.find(prefix + "title")),
                _text(date),
                _atom_body(node.find(prefix + "content")),
            )
        )
    return rows


def _rss_rows(nodes, namespace, limit):
    from .sources import FetchError

    prefix = "{" + namespace + "}" if namespace else ""
    rows = []
    for node in islice(nodes, limit):
        encoded = node.find("{" + RSS_CONTENT + "}encoded")
        if encoded is not None and len(encoded):
            raise FetchError("invalid_feed_content")
        date = node.find(prefix + "pubDate")
        if date is None:
            date = node.find("{http://purl.org/dc/elements/1.1/}date")
        rows.append(
            _row(
                _text(node.find(prefix + "link")),
                _text(node.find(prefix + "title")),
                _text(date),
                encoded.text if encoded is not None else None,
            )
        )
    return rows


def parse_feed(result, adapter, limit=MAX_FEED_ITEMS):
    """Return metadata and only explicit bodies; perform no I/O or rendering.

    ``embedded_complete`` means a nonempty supported full-content field was
    supplied. Description, summary, remote Atom content, and unknown content
    types never gain this flag. Oversized bodies fail rather than truncate.
    """
    from .sources import FetchError, decode_body

    if type(limit) is not int or not 1 <= limit <= MAX_FEED_ITEMS:
        raise FetchError("invalid_feed_limit")
    if not isinstance(result.body, bytes) or len(result.body) > MAX_FEED_BYTES:
        raise FetchError("body_too_large")
    text = decode_body(result.body, result.content_type)
    if adapter == "authorized_api":
        try:
            payload = json.loads(text)
        except (ValueError, RecursionError):
            raise FetchError("invalid_feed") from None
        items = payload if isinstance(payload, list) else payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise FetchError("invalid_feed")
        return [
            _row(
                item.get("url") or item.get("link"),
                item.get("title"),
                item.get("published_at") or item.get("date_published"),
                item.get("content_html"),
            )
            for item in items[:limit]
            if isinstance(item, dict)
        ]
    if adapter not in {"rss", "rsshub", "werss"}:
        raise FetchError("invalid_feed_adapter")
    # Deny declarations before parsing; stdlib XML must not resolve entities.
    if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        raise FetchError("unsafe_xml")
    root = _xml_root(text)
    try:
        if root.tag == "{" + ATOM + "}feed":
            return _atom_rows(root, ATOM, limit)
        if root.tag == "feed":
            return _atom_rows(root, "", limit)
        if root.tag == "rss":
            return _rss_rows(root.findall("channel/item"), "", limit)
        if root.tag == "{" + RDF + "}RDF":
            return _rss_rows(root.findall("{" + RSS_ONE + "}item"), RSS_ONE, limit)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise FetchError("invalid_feed") from None
    raise FetchError("invalid_feed")

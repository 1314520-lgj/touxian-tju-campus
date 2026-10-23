"""Bounded, read-only official-source acceptance. Default: preview, no network/DB.

The network mode uses the product SafeFetcher without trusted private gateways.
Only counters, classifications and URL hashes leave memory; never page text,
article titles, query strings, credentials, response headers or resolved IPs.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import socket
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[2]
MAX_ARTICLES, MAX_ATTACHMENTS = 3, 2
LIST_LIMIT = 101
FAKE_IP_RANGE = ipaddress.ip_network("198.18.0.0/15")
RECOMMENDATIONS = {
    "dns_failed": "Check the runtime resolver and school authoritative DNS; rerun in the deployment runtime.",
    "unsafe_address": "Keep SSRF protection. Check fake-IP/split DNS with the network administrator; use real public resolution.",
    "fetch_transport_failed": "Check runtime egress, TLS trust and upstream availability; never disable certificate validation.",
    "auth_expired": "The upstream requires access or denies requests. Configure an authorized adapter; do not bypass the gate.",
    "no_listing_items": "Check the official listing and configured selector/pattern; it may be empty or rendered by JavaScript.",
    "suspicious_html": "HTTP 200 is insufficient: inspect the public source manually for login, challenge or error pages.",
    "html_body_policy_unavailable": "Use a release containing the shared HTML body policy, then rerun this probe.",
    "partial_body": "Inspect selector/fallback and parser warnings before publishing evidence-backed cards.",
    "html_access_interstitial": "The shared body policy found an access/challenge page; inspect upstream access without bypassing it.",
    "html_empty_body_requires_review": "No usable body was found; inspect the official page and its configured structure.",
    "html_image_body_requires_ocr": "The body needs OCR/manual review; do not publish a title-only card as complete evidence.",
    "generic_body_fallback_requires_review": "The body used a generic fallback; verify it excludes menus and unrelated notices.",
    "unsupported_legacy_office_requires_review": "Convert the public attachment through a reviewed workflow or import its text manually.",
    "ocr_not_configured": "This attachment requires OCR/manual review; a successful download does not prove readable evidence.",
    "parser_not_run": "Download/magic checks passed; run the normal isolated parser before claiming attachment evidence.",
}


def load_catalog(root=ROOT):
    """Read the shipped official catalog; no DB, environment secrets or dynamic URLs."""
    raw = json.loads((root / "config/campus-sources.json").read_text(encoding="utf-8"))
    sources = raw["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 100:
        raise ValueError("invalid_official_catalog")
    seen = set()
    for source in sources:
        ident = str(uuid.UUID(source["id"]))
        parsed = urlsplit(source["origin_url"])
        if (
            ident in seen
            or parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".tju.edu.cn")
        ):
            raise ValueError("invalid_official_catalog")
        if parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.query or parsed.fragment:
            raise ValueError("invalid_official_catalog")
        if source["adapter"] != "website" or not isinstance(source.get("config", {}), dict):
            raise ValueError("invalid_official_catalog")
        seen.add(ident)
    return sources


def url_hash(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def safe_code(code):
    """Never print an exception or a user-controlled diagnostic string."""
    return code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) else "unexpected_probe_error"


class ObservedResolver:
    """Observe the exact resolver used by SafeFetcher, without changing answers."""

    def __init__(self, resolver=socket.getaddrinfo):
        self.resolver = resolver
        self.observations = []

    def __call__(self, host, port, **kwargs):
        try:
            answer = self.resolver(host, port, **kwargs)
        except (OSError, ValueError):
            self.observations.append({"state": "dns_failed", "address_count": 0, "public_count": 0, "fake_ip_count": 0})
            raise
        addresses = {item[4][0] for item in answer}
        public, fake = 0, 0
        for raw in addresses:
            try:
                address = ipaddress.ip_address(raw.split("%")[0])
                address = getattr(address, "ipv4_mapped", None) or address
                public += bool(address.is_global)
                fake += bool(address.version == 4 and address in FAKE_IP_RANGE)
            except ValueError:
                pass
        self.observations.append(
            {
                "state": "public" if addresses and public == len(addresses) else "non_public_or_invalid",
                "address_count": len(addresses),
                "public_count": public,
                "fake_ip_count": fake,
            }
        )
        return answer


def analyze_html(result, analyzer=None):
    """Use the product's pure body policy, without constructing Settings or Core."""
    from bs4 import BeautifulSoup

    from server.campus.sources import decode_body

    soup = BeautifulSoup(decode_body(result.body, result.content_type), "html.parser")
    text = soup.get_text(" ", strip=True)
    # This narrow fail-closed check is diagnostic, not an authentication bypass.
    suspicious = bool(
        soup.select_one('input[type="password"]')
        or (
            len(text) < 2000
            and re.search(r"访问过于频繁|访问频繁|人机验证|安全验证|请输入验证码|页面不存在|内容已被删除", text)
        )
    )
    if suspicious:
        return {
            "state": "failed",
            "segment_count": 0,
            "text_characters": 0,
            "warnings": ["suspicious_html"],
            "shared_body_policy": False,
        }
    if analyzer is None:
        from server.campus import content

        analyzer = getattr(content, "html_body_segments", None)
    if analyzer is None:
        return {
            "state": "not_run",
            "segment_count": 0,
            "text_characters": 0,
            "warnings": ["html_body_policy_unavailable"],
            "shared_body_policy": False,
        }
    segments, state, warnings = analyzer(result.body, result.url)
    return {
        "state": state if state in {"ok", "partial", "failed"} else "failed",
        "segment_count": len(segments),
        "text_characters": sum(len(s["text"]) for s in segments),
        "warnings": list(dict.fromkeys(safe_code(w) for w in warnings)),
        "shared_body_policy": True,
    }


def attachment_urls(result):
    from bs4 import BeautifulSoup

    from server.campus.sources import EXTENSIONS, decode_body

    soup = BeautifulSoup(decode_body(result.body, result.content_type), "html.parser")
    urls = []
    for link in soup.select("a[href]"):
        target = urljoin(result.url, link.get("href", ""))
        path = urlsplit(target).path.lower()
        if Path(path).suffix in EXTENSIONS or "virtual_attach_file" in path or "download.jsp" in path:
            if target not in urls:
                urls.append(target)
        if len(urls) > MAX_ATTACHMENTS:
            break
    return urls


def probe_source(source, fetcher, resolver, articles=1, attachments=1, pause=0.25, html_analyzer=None):
    from server.campus.sources import FetchError, content_kind, feed_items

    started = time.monotonic()
    report = {
        "source_id": source["id"],
        "source_identity": source["source_identity"],
        "origin_host": urlsplit(source["origin_url"]).hostname,
        "state": "failed",
        "listing_items": 0,
        "listing_window_truncated": False,
        "article_samples": [],
        "stages": [],
        "recommendations": [],
    }
    findings = []

    def request(url, stage):
        before = len(resolver.observations)
        entry = {"stage": stage, "url_sha256": url_hash(url)}
        report["stages"].append(entry)
        try:
            result = fetcher.fetch(url)  # No credentials/headers, including source environment tokens.
            entry.update(state="fetched", bytes=len(result.body), content_kind=content_kind(result))
            entry["final_url_sha256"] = url_hash(result.url)
            return result
        except FetchError as error:
            entry.update(state="failed", error=safe_code(error.code))
            findings.append(entry["error"])
            return None
        except Exception:
            entry.update(state="failed", error="unexpected_probe_error")
            findings.append(entry["error"])
            return None
        finally:
            entry["dns"] = resolver.observations[before:]
            if pause:
                time.sleep(pause)

    listing = request(source["origin_url"], "listing")
    if listing is not None:
        try:
            # Inspect HTML gates even if a login page happens to contain matching anchors.
            from bs4 import BeautifulSoup

            from server.campus.sources import decode_body

            soup = BeautifulSoup(decode_body(listing.body, listing.content_type), "html.parser")
            visible = soup.get_text(" ", strip=True)
            blocked = bool(
                soup.select_one('input[type="password"]')
                or (len(visible) < 2000 and re.search(r"访问过于频繁|人机验证|安全验证|请输入验证码", visible))
            )
            items = (
                [] if blocked else feed_items(listing, source["adapter"], source.get("config", {}), limit=LIST_LIMIT)
            )
            report["listing_items"] = min(len(items), LIST_LIMIT - 1)
            report["listing_window_truncated"] = len(items) >= LIST_LIMIT
            if not items:
                findings.append("suspicious_html" if blocked else "no_listing_items")
            for item in items[:articles]:
                url = urljoin(listing.url, item["url"])
                article = request(url, "article")
                sample = {"url_sha256": url_hash(url), "state": "failed", "attachments": []}
                report["article_samples"].append(sample)
                if article is None:
                    continue
                kind = content_kind(article)
                sample["content_kind"] = kind
                if kind != "html":
                    sample.update(state="not_run", warnings=["parser_not_run"])
                    findings.append("parser_not_run")
                    continue
                sample.update(analyze_html(article, html_analyzer))
                if sample["state"] != "ok":
                    findings.extend(sample["warnings"] or ["partial_body"])
                # Never follow attachments from a known login/challenge/error page.
                if sample["state"] == "failed":
                    continue
                targets = attachment_urls(article)
                sample["attachment_links_at_least"] = len(targets)
                sample["attachment_sample_truncated"] = len(targets) > attachments
                for target in targets[:attachments]:
                    attached = request(target, "attachment")
                    attachment = {"url_sha256": url_hash(target), "state": "failed"}
                    sample["attachments"].append(attachment)
                    if attached is None:
                        continue
                    attached_kind = content_kind(attached)
                    attachment["content_kind"] = attached_kind
                    if attached_kind == "html":
                        attachment.update(analyze_html(attached, html_analyzer))
                    else:
                        code = (
                            "unsupported_legacy_office_requires_review"
                            if attached.body.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
                            else "ocr_not_configured"
                            if attached_kind == "image"
                            else "parser_not_run"
                        )
                        attachment.update(state="not_run", warnings=[code])
                    findings.extend(attachment.get("warnings", []))
            samples = report["article_samples"]
            if items and articles == 0:
                report["state"] = "listing_only"
            elif samples and all(sample["state"] == "ok" for sample in samples) and not findings:
                report["state"] = "sample_passed"
            elif items:
                report["state"] = "review_required"
        except FetchError as error:
            findings.append(safe_code(error.code))
        except Exception:
            findings.append("unexpected_probe_error")
    report["finding_codes"] = list(dict.fromkeys(findings))
    report["recommendations"] = list(
        dict.fromkeys(
            RECOMMENDATIONS.get(code, "Inspect the stage error and upstream configuration; rerun after correction.")
            for code in report["finding_codes"]
        )
    )
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def run(sources, *, network=False, articles=1, attachments=1, resolver=None, connector=None, pause=0.25):
    if not 0 <= articles <= MAX_ARTICLES or not 0 <= attachments <= MAX_ATTACHMENTS:
        raise ValueError("invalid_sample_limits")
    report = {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "network" if network else "preview",
        "read_only": True,
        "limits": {
            "articles_per_source": articles,
            "attachments_per_article": attachments,
            "listing_items": LIST_LIMIT - 1,
            "bytes_per_request": 8 * 1024 * 1024,
            "http_deadline_seconds": 15,
            "redirects": 4,
            "dns_timeout": "System resolver managed; not part of the HTTP deadline.",
        },
        "scope": "Bounded current-page samples; no historical completeness, sustained health or end-user reachability claim.",
        "sources": [],
    }
    if not network:
        report["sources"] = [
            {
                "source_id": s["id"],
                "source_identity": s["source_identity"],
                "origin_host": urlsplit(s["origin_url"]).hostname,
                "state": "not_checked",
            }
            for s in sources
        ]
    else:
        from server.campus.sources import SafeFetcher

        observed = ObservedResolver(resolver or socket.getaddrinfo)
        fetcher = SafeFetcher(resolver=observed, connector=connector, trusted_origins=())
        report["sources"] = [probe_source(s, fetcher, observed, articles, attachments, pause) for s in sources]
    report["counts"] = dict(Counter(item["state"] for item in report["sources"]))
    report["source_count"] = len(sources)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="Explicitly GET bounded public official-source samples.")
    parser.add_argument(
        "--source", action="append", default=[], help="Existing static source UUID; repeat to select several."
    )
    parser.add_argument("--articles", type=int, choices=range(MAX_ARTICLES + 1), default=1)
    parser.add_argument("--attachments", type=int, choices=range(MAX_ATTACHMENTS + 1), default=1)
    args = parser.parse_args(argv)
    # Make direct execution from any working directory work, including the runtime image.
    sys.path.insert(0, str(ROOT))
    try:
        sources = load_catalog()
        known = {s["id"] for s in sources}
        if set(args.source) - known:
            parser.error("--source must be an existing official catalog UUID")
        if args.source:
            sources = [s for s in sources if s["id"] in args.source]
        report = run(sources, network=args.network, articles=args.articles, attachments=args.attachments)
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"mode": "network" if args.network else "preview", "error": "invalid_official_catalog"}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(args.network and any(s["state"] != "sample_passed" for s in report["sources"]))


if __name__ == "__main__":
    raise SystemExit(main())

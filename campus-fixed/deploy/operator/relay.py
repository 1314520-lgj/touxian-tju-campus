#!/usr/bin/env python3
"""Operator relay for restricted-network deployments.

The product's own collector refuses to fetch ``*.tju.edu.cn`` from a machine
whose DNS answers with reserved addresses -- correctly (see task pool N4).
An operator who *can* reach the official sites therefore runs this relay:

1. ``build``  -- turn hand-staged article records into one authorized feed per
   column. A record is only produced by the operator actually opening the
   official page; the record keeps the real article URL, the fetch time and a
   SHA-256 of the transported body, and nothing else. The relay never invents
   or rewrites content.
2. ``serve``  -- serve those feeds from ``127.0.0.1``. The product is launched
   with ``CAMPUS_TRUSTED_GATEWAY_ORIGINS`` naming exactly this origin, so the
   product's SafeFetcher accepts it and nothing else changes.
3. ``register`` -- log in as the local operator, create one ``authorized_feed``
   source per column (``adapter=authorized_api``,
   ``feed_content_mode=authorized_fulltext``), record the verification basis
   and trigger a collection. Every feed item keeps the real official URL, so
   candidates, evidence links and published events all point at the original
   notice, never at the relay.
4. ``report`` -- read back source health, review queue and extracted rows and
   write an evidence JSON for the cycle.
5. ``decide`` -- record one review decision (accept/reject) with a reason.

Full procedure and acceptance ladder: ``deploy/operator/OPERATIONS.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.campus.feed_content import MAX_EMBEDDED_BYTES  # noqa: E402

RELAY_DIR = Path(__file__).resolve().parent
STAGING_DIR = RELAY_DIR / "staging"
OUT_DIR = RELAY_DIR / "out"
SESSION_FILE = RELAY_DIR / "session.json"

# One relay source per operator-relayed column. ``source_identity`` is stable:
# re-registering a cycle updates the existing row instead of piling up sources.
COLUMNS = {
    "zs-notices": {
        "name": "运营中继 · 本科招生网通知公告",
        "column_url": "https://zs.tju.edu.cn/ym21/bkzn/tzgg.htm",
    },
    "oaa-competitions": {
        "name": "运营中继 · 教务处竞赛科创通知",
        "column_url": "https://oaa.tju.edu.cn/tzgg/js_kc.htm",
    },
}

FEED_PREFIX = "feed-"
IDENTITY_PREFIX = "relay-"


def now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def relay_basis(column_url: str, day: str) -> str:
    return (
        f"运营中继 {day}：正文由运营方从该栏目逐个页面服务端抓取后原样转入授权 feed，"
        f"中继不改写内容；每条候选的证据链接是真实官网地址 {column_url} 下的文章页。"
        "抓取方式、网络边界与验收记录见 deploy/operator/OPERATIONS.md。"
    )


def _unsafe_host(hostname: str) -> bool:
    if not hostname:
        return True
    if hostname in {"localhost", "metadata.google.internal"} or hostname.endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(hostname).is_global
    except ValueError:
        return False


def validate_record(column: str, path: Path, raw: dict) -> dict:
    """Return a normalized record or raise ``ValueError`` with the reason."""
    for key in ("url", "title", "content_html", "fetched_at"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ValueError(f"{column}/{path.name}: missing {key}")
    url = raw["url"].strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError(f"{column}/{path.name}: article URL must be https")
    if _unsafe_host(parsed.hostname or ""):
        raise ValueError(f"{column}/{path.name}: article URL must be a public host")
    title = raw["title"].strip()
    if len(title) > 300:
        raise ValueError(f"{column}/{path.name}: title longer than 300 characters")
    body = raw["content_html"]
    if len(body.encode("utf-8")) > MAX_EMBEDDED_BYTES:
        raise ValueError(f"{column}/{path.name}: content larger than the feed limit")
    published = raw.get("published_at")
    if published is not None and (not isinstance(published, str) or not published.strip()):
        raise ValueError(f"{column}/{path.name}: published_at must be a non-empty string when present")
    return {
        "url": url,
        "title": title,
        "published_at": published.strip() if isinstance(published, str) else None,
        "content_html": raw["content_html"],
        "fetched_at": raw["fetched_at"].strip(),
        "fetched_by": raw.get("fetched_by", "operator"),
        "note": raw.get("note"),
    }


def stage(staging: Path = STAGING_DIR) -> int:
    """Convert ``<column>/<slug>.txt`` verbatim bodies into staging records.

    The text file is the operator's untouched copy of the fetched page; the
    generated ``<slug>.json`` escapes it into ``<p>`` paragraphs. Nothing is
    summarized or reordered here, so the byte-for-byte text a person reads is
    the text the feed carries. Metadata comes from a ``<slug>.meta.json``
    sidecar (``url``, ``title``, ``published_at``, ``fetched_at``).
    """
    import html as html_module

    count = 0
    for column in sorted(COLUMNS):
        directory = staging / column
        if not directory.is_dir():
            continue
        for text_path in sorted(directory.glob("*.txt")):
            meta_path = text_path.with_suffix("").with_suffix(".meta.json")
            if not meta_path.is_file():
                print(f"relay stage: skip {text_path.name} (no {meta_path.name})")
                continue
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            body = text_path.read_text(encoding="utf-8")
            paragraphs = [block.strip() for block in body.split("\n\n") if block.strip()]
            content_html = "".join(f"<p>{html_module.escape(block)}</p>" for block in paragraphs)
            record = {**meta, "content_html": content_html}
            record_path = text_path.with_suffix(".json")
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            validate_record(column, record_path, record)
            count += 1
    print(f"relay stage: {count} record(s) written under {staging}")
    return count


def build(staging: Path = STAGING_DIR, out: Path = OUT_DIR) -> dict:
    """Validate staging, write per-column feeds plus a provenance report."""
    if not staging.is_dir():
        raise SystemExit(f"staging directory not found: {staging}")
    out.mkdir(parents=True, exist_ok=True)
    provenance = {"generated_at": now_iso(), "columns": {}}
    seen = {}
    for column, meta in COLUMNS.items():
        directory = staging / column
        items, skipped = [], []
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    record = validate_record(column, path, raw)
                except (ValueError, json.JSONDecodeError) as error:
                    skipped.append({"file": path.name, "reason": str(error)})
                    continue
                if record["url"] in seen:
                    skipped.append({"file": path.name, "reason": f"duplicate of {seen[record['url']]}"})
                    continue
                seen[record["url"]] = f"{column}/{path.name}"
                items.append(record)
        feed = {"items": [{k: item[k] for k in ("url", "title", "published_at", "content_html")} for item in items]}
        (out / f"{FEED_PREFIX}{column}.json").write_text(
            json.dumps(feed, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        provenance["columns"][column] = {
            "column_url": meta["column_url"],
            "items": [
                {
                    "url": item["url"],
                    "title": item["title"],
                    "published_at": item["published_at"],
                    "content_sha256": hashlib.sha256(item["content_html"].encode("utf-8")).hexdigest(),
                    "content_bytes": len(item["content_html"].encode("utf-8")),
                    "fetched_at": item["fetched_at"],
                    "fetched_by": item["fetched_by"],
                }
                for item in items
            ],
            "skipped": skipped,
        }
    (out / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    total = sum(len(c["items"]) for c in provenance["columns"].values())
    print(f"relay build: {total} item(s) across {len(provenance['columns'])} column(s); provenance in {out}")
    return provenance


class FeedHandler(http.server.BaseHTTPRequestHandler):
    out_dir = OUT_DIR

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            body, ctype = b'{"ok":true}', "application/json"
        else:
            name = {"provenance.json": "provenance.json"}.get(path, path.lstrip("/"))
            if not (self.out_dir / name).is_file():
                self.send_error(404)
                return
            body, ctype = (self.out_dir / name).read_bytes(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the operator console quiet
        return


def serve(out: Path = OUT_DIR, host: str = "127.0.0.1", port: int = 8791):
    FeedHandler.out_dir = out
    server = http.server.ThreadingHTTPServer((host, port), FeedHandler)
    print(f"relay serve: http://{host}:{port} (feeds: {', '.join(FEED_PREFIX + c for c in COLUMNS)})", flush=True)
    server.serve_forever()


class AdminSession:
    """Minimal admin API client: challenge, login, CSRF, JSON helpers.

    The dev-auth challenge endpoint is rate limited (at most six codes per
    email per hour, and never two within 30 seconds), so a session that has
    already been established is persisted to disk and reused. Only the first
    login of an operating day spends a challenge; every later subcommand
    validates the stored cookie with a plain read of ``/api/v1/me``.
    """

    def __init__(self, api: str, email: str):
        self.api = api.rstrip("/")
        self.email = email
        self.cookies = ""
        self.csrf = ""
        self.me = None

    def _request(self, method: str, path: str, body=None, headers=None):
        request = urllib.request.Request(self.api + path, method=method)
        if self.cookies:
            request.add_header("Cookie", self.cookies)
        if body is not None:
            request.add_header("Content-Type", "application/json")
            body = json.dumps(body).encode("utf-8")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        with urllib.request.urlopen(request, body) as response:
            self.cookies = response.headers.get("Set-Cookie", self.cookies).split(";", 1)[0] or self.cookies
            payload = json.loads(response.read() or b"{}")
            dev_code = response.headers.get("X-Dev-Login-Code")
            return payload, dev_code

    def login(self):
        """Reuse a stored session when it still validates, else challenge once."""
        if self._restore():
            return self.me
        payload, dev_code = self._request("POST", "/api/v1/auth/challenges", {"email": self.email})
        if not dev_code:
            raise SystemExit("no development login code returned: launch the product in operator mode")
        self._request("POST", "/api/v1/auth/sessions", {"challenge_id": payload["challenge_id"], "code": dev_code})
        me, _ = self._request("GET", "/api/v1/me")
        self.csrf = me.get("csrf_token", "")
        self.me = me
        self._save()
        return me

    def _restore(self) -> bool:
        """Adopt the persisted cookie when the API still accepts it.

        The cached cookie belongs to the identity it was created for. Checking
        only the API base URL let a session saved for one email be reused under
        a different ``--email``; the product then authenticated as the old
        account and refused the write with 403, which reads like a permissions
        bug rather than a stale credential.
        """
        try:
            stored = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        cookie = stored.get("cookie")
        if not cookie or stored.get("api") != self.api:
            return False
        if stored.get("email") != self.email:
            return False
        self.cookies = cookie
        try:
            me, _ = self._request("GET", "/api/v1/me")
        except urllib.error.HTTPError:
            self.cookies = ""
            return False
        if not me.get("csrf_token"):
            self.cookies = ""
            return False
        self.csrf = me["csrf_token"]
        self.me = me
        return True

    def _save(self):
        SESSION_FILE.write_text(
            json.dumps({"api": self.api, "email": self.email, "cookie": self.cookies}, ensure_ascii=False, indent=1)
            + "\n",
            encoding="utf-8",
        )

    def post(self, path, body, write=False, idempotency_key=None):
        headers = {}
        if write:
            headers["X-CSRF-Token"] = self.csrf
            # Write endpoints demand an idempotency key; a replayed cycle must
            # update the same rows instead of piling duplicates up.
            headers["Idempotency-Key"] = idempotency_key or f"relay:{path}:{now_iso()}"
        payload, _ = self._request("POST", path, body, headers)
        return payload

    def get(self, path):
        payload, _ = self._request("GET", path)
        return payload


def register(api: str, email: str, feed_origin: str, trigger: bool = True) -> dict:
    session = AdminSession(api, email)
    session.login()
    day = datetime.now(UTC).date().isoformat()
    # One cycle id per register run: replays within the cycle update the same
    # rows, a later cycle enqueues a fresh collection.
    cycle_id = os.environ.get("RELAY_CYCLE") or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    result = {"generated_at": now_iso(), "cycle": cycle_id, "sources": []}
    existing = {}
    cursor = None
    while True:
        page = session.get("/api/v1/admin/sources?limit=100" + (f"&cursor={cursor}" if cursor else ""))
        for source in page["items"]:
            existing[source.get("source_identity")] = source
        cursor = page.get("next_cursor")
        if not cursor:
            break
    for column, meta in COLUMNS.items():
        identity = IDENTITY_PREFIX + column
        feed_url = f"{feed_origin.rstrip('/')}/{FEED_PREFIX}{column}.json"
        source = existing.get(identity)
        if source is None:
            source = session.post(
                "/api/v1/admin/sources",
                {
                    "name": meta["name"],
                    "kind": "authorized_feed",
                    "origin_url": feed_url,
                    "adapter": "authorized_api",
                    "source_identity": identity,
                    "feed_content_mode": "authorized_fulltext",
                },
                write=True,
                idempotency_key=f"relay-create:{identity}",
            )
            action = "created"
        else:
            action = "reused"
        if source.get("feed_content_mode") != "authorized_fulltext":
            source = session.post(
                f"/api/v1/admin/sources/{source['id']}",
                {"feed_content_mode": "authorized_fulltext"},
                write=True,
                idempotency_key=f"relay-mode:{source['id']}",
            )
        session.post(
            f"/api/v1/admin/sources/{source['id']}/verification",
            {"verification": "verified_official", "basis": relay_basis(meta["column_url"], day)},
            write=True,
            idempotency_key=f"relay-verify:{source['id']}:{day}",
        )
        queued = None
        if trigger:
            queued = session.post(
                f"/api/v1/admin/sources/{source['id']}/fetch",
                {},
                write=True,
                idempotency_key=f"relay-fetch:{source['id']}:{cycle_id}",
            )
        result["sources"].append(
            {"column": column, "action": action, "id": source["id"], "feed_url": feed_url, "fetch": queued}
        )
    (RELAY_DIR / "registration.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"relay register: {len(result['sources'])} source(s) ({'; '.join(s['action'] for s in result['sources'])})")
    return result


def decide(api: str, email: str, candidate_id: str, decision: str, reason: str) -> dict:
    """Record one review decision; ``replacement_candidate`` stays null so the
    extraction the operator just read back is exactly what gets published."""
    if decision not in {"accept", "reject"}:
        raise SystemExit("decision must be accept or reject")
    session = AdminSession(api, email)
    session.login()
    pending = []
    cursor = None
    while True:
        page = session.get("/api/v1/admin/reviews?limit=100" + (f"&cursor={cursor}" if cursor else ""))
        pending.extend(page["items"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    candidate = next((c for c in pending if c["candidate_id"] == candidate_id), None)
    if candidate is None:
        raise SystemExit(f"candidate {candidate_id} is not in the pending review queue")
    body = {
        "expected_candidate_revision": candidate["revision"],
        "decision": decision,
        "reason": reason,
        "replacement_candidate": None,
    }
    result = session.post(
        f"/api/v1/admin/reviews/{candidate_id}/decision",
        body,
        write=True,
        idempotency_key=f"relay-decide:{candidate_id}:{candidate['revision']}",
    )
    print(f"relay decide: {candidate_id} -> {decision}")
    return result


def report(api: str, email: str, out: Path) -> dict:
    session = AdminSession(api, email)
    session.login()
    sources = []
    cursor = None
    while True:
        page = session.get("/api/v1/admin/sources?limit=100" + (f"&cursor={cursor}" if cursor else ""))
        for source in page["items"]:
            if str(source.get("source_identity", "")).startswith(IDENTITY_PREFIX):
                sources.append(source)
        cursor = page.get("next_cursor")
        if not cursor:
            break
    pending = []
    cursor = None
    while True:
        page = session.get("/api/v1/admin/reviews?limit=100" + (f"&cursor={cursor}" if cursor else ""))
        pending.extend(page["items"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    evidence = {
        "generated_at": now_iso(),
        "relay_sources": [
            {
                "column": s["source_identity"][len(IDENTITY_PREFIX) :],
                "id": s["id"],
                "health": s.get("health"),
                "last_success_at": s.get("last_success_at"),
                "last_error_code": s.get("last_error_code"),
            }
            for s in sources
        ],
        "pending_candidates": [
            {
                "candidate_id": c.get("candidate_id"),
                "revision": c.get("revision"),
                "change_kind": c.get("change_kind"),
                "title": c.get("title"),
                "extractor_version": c.get("extractor_version"),
                "review_required": c.get("review_required"),
                "action_required": c.get("action_required"),
                "entry_urls": c.get("entry_urls"),
                "deadlines": c.get("deadlines"),
                "materials": c.get("materials"),
                "warnings": c.get("warnings"),
                "snapshot_ids": c.get("snapshot_ids"),
            }
            for c in pending
        ],
    }
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(
        f"relay report: {len(evidence['relay_sources'])} relay source(s), {len(evidence['pending_candidates'])} pending"
    )
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--staging", type=Path, default=STAGING_DIR)
    build_parser.add_argument("--out", type=Path, default=OUT_DIR)
    stage_parser = sub.add_parser("stage")
    stage_parser.add_argument("--staging", type=Path, default=STAGING_DIR)
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--out", type=Path, default=OUT_DIR)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8791)
    register_parser = sub.add_parser("register")
    register_parser.add_argument("--api", default="http://127.0.0.1:8000")
    register_parser.add_argument("--email", default="operator@touxian.local")
    register_parser.add_argument("--feed-origin", default="http://127.0.0.1:8791")
    register_parser.add_argument("--no-fetch", action="store_true")
    report_parser = sub.add_parser("report")
    report_parser.add_argument("--api", default="http://127.0.0.1:8000")
    report_parser.add_argument("--email", default="operator@touxian.local")
    report_parser.add_argument("--out", type=Path, default=RELAY_DIR / "cycle-report.json")
    decide_parser = sub.add_parser("decide")
    decide_parser.add_argument("--api", default="http://127.0.0.1:8000")
    decide_parser.add_argument("--email", default="operator@touxian.local")
    decide_parser.add_argument("--candidate", required=True)
    decide_parser.add_argument("--decision", required=True, choices=["accept", "reject"])
    decide_parser.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    if args.command == "build":
        build(args.staging, args.out)
    elif args.command == "stage":
        stage(args.staging)
    elif args.command == "serve":
        serve(args.out, args.host, args.port)
    elif args.command == "register":
        register(args.api, args.email, args.feed_origin, trigger=not args.no_fetch)
    elif args.command == "report":
        report(args.api, args.email, args.out)
    elif args.command == "decide":
        decide(args.api, args.email, args.candidate, args.decision, args.reason)


if __name__ == "__main__":
    main()

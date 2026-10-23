"""Create a fresh public-history sample, never modify the supplied database.

Only public school-source collections are retained. Authentication, private
items, notification destinations, queue payloads, runtime secrets and the
identity of the reviewing operator are excluded by construction rather than
deleted from a copied DB.

The *decisions* are kept. Dropping the review chain outright hid the reasoning
behind "163 candidates became 73 published notices"; keeping the actor would
have exported an operator's address. The chain is therefore retained with the
actor field removed, so the funnel is auditable without naming anyone.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.campus.blob_store import LocalBlobStore
from server.campus.platform import Settings
from server.campus.schema import validate
from server.campus.sources import attachment_access_restricted, canonical_url, public_source_url, url_has_secret
from server.campus_app import assemble

COLLECTIONS = {
    "catalog",
    "sources",
    "documents",
    "snapshots",
    "parsed",
    "parse_inputs",
    "evidence",
    "candidates",
    "candidate_inputs",
    "candidate_versions",
    "event_receipts",
    "events",
    "event_versions",
    "event_snapshots",
    "event_snapshot_versions",
    "reviews",
    "publication_audits",
    "fetch_runs",
}
# Kept, but with the person removed: who decided is not public, what was decided is.
REDACTED_FIELDS = {"reviews": ("reviewer_id",), "publication_audits": ("actor_id",)}
# The exact key set a redacted record may still carry.
DECISION_CHAIN_KEYS = {
    "reviews": {"id", "candidate_id", "revision", "decision", "reason", "created_at"},
    "publication_audits": {"event_id", "reason", "publication_state", "created_at"},
}
SCHEMAS = {
    "sources": "AdminSource",
    "snapshots": "DocumentSnapshot",
    "parsed": "ParsedDocument",
    "evidence": "Evidence",
    "candidates": "ExtractionCandidate",
    "candidate_versions": "ExtractionCandidate",
    "events": "EventVersion",
    "event_versions": "EventVersion",
}
OPAQUE_FIELDS = {
    "documents": {
        "id",
        "latest_snapshot_id",
        "snapshot_attachments",
        "snapshot_observations",
        "snapshot_observed_at",
        "snapshot_warnings",
        "change_summaries",
        "source_id",
        "source_url",
    },
    "parse_inputs": {"parser_version", "snapshot_warnings"},
    "event_receipts": {"event_id", "candidate_id", "candidate_revision", "skipped"},
    "event_snapshot_versions": {"snapshot_ids", "observed_at"},
    "event_snapshots": {"snapshot_ids"},
}


def check_public_urls(value, parent=None):
    if isinstance(value, dict):
        for field, item in value.items():
            if field in {"source_url", "origin_url", "feed_origin"}:
                for url in item if isinstance(item, list) else [item]:
                    if not isinstance(url, str) or url_has_secret(url) or public_source_url(url) != canonical_url(url):
                        raise ValueError("Credential-bearing or private provenance URL")
            elif field == "entry_urls" and parent != "field_evidence":
                # Published notices can legitimately direct students to a
                # campus intranet system. These links are never fetched here.
                for url in item:
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme not in {"http", "https"}
                        or not parsed.hostname
                        or parsed.username
                        or parsed.password
                        or url_has_secret(url)
                    ):
                        raise ValueError("Unsafe action entry URL")
            check_public_urls(item, field)
    elif isinstance(value, list):
        for item in value:
            check_public_urls(item, parent)


def project(collection, value):
    if collection in OPAQUE_FIELDS:
        value = {k: v for k, v in value.items() if k in OPAQUE_FIELDS[collection]}
    if collection in REDACTED_FIELDS:
        # Drop only the identifying field; the decision and its reason stay.
        value = {k: v for k, v in value.items() if k not in REDACTED_FIELDS[collection]}
        if not set(value) <= DECISION_CHAIN_KEYS[collection]:
            raise ValueError(f"{collection} record carries an unexpected field")
    if collection == "candidate_inputs":
        clean = {}
        for key, item in value.items():
            UUID(key)
            clean[key] = {
                k: v
                for k, v in item.items()
                if k in {"parser_version", "state", "warnings", "observation", "observed_at"}
            }
        value = clean
    if collection == "documents":
        for field in (
            "snapshot_observations",
            "snapshot_observed_at",
            "snapshot_warnings",
            "snapshot_attachments",
            "change_summaries",
        ):
            for key, item in value.get(field, {}).items():
                UUID(key)
                if field == "snapshot_observations" and not isinstance(item, int):
                    raise ValueError("Invalid snapshot observation")
                if field == "snapshot_attachments":
                    for sid in item:
                        UUID(sid)
                if field == "change_summaries":
                    validate("ChangeSummary", item)
    return value


def sanitize(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Source and destination must be independent directories")
    if destination.exists():
        raise ValueError("Destination must not already exist")
    destination.mkdir(parents=True)
    connection = sqlite3.connect((source / "campus.sqlite3").as_uri() + "?mode=ro", uri=True)
    try:
        original = connection.execute("SELECT collection,key,data FROM campus_records").fetchall()
    finally:
        connection.close()
    records = {(collection, key): json.loads(data) for collection, key, data in original if collection in COLLECTIONS}
    for (collection, _key), value in records.items():
        check_public_urls(value)
        if collection == "snapshots" and value.get("visibility") != "public":
            raise ValueError("Private snapshot")
        if collection == "snapshots" and ("sources", value["source_id"]) not in records:
            raise ValueError("Snapshot source missing")
    blobs = LocalBlobStore(source)
    core = assemble(Settings(data_dir=destination, secret="sample-build-only-never-shipped-" * 3))
    # Never connect to or mutate an operator's inherited remote index.
    core.search.url = ""
    core.search.key = ""
    counts = {}
    try:
        with core.store.transaction():
            for (collection, key), value in records.items():
                value = project(collection, copy.deepcopy(value))
                if collection == "sources":
                    value["enabled"] = False
                    value["verification_basis"] = "随用户提供的历史公开来源目录；启用前由运营重新核验。"
                if collection == "parsed":
                    snapshot = records.get(("snapshots", key))
                    if (
                        snapshot
                        and snapshot.get("parent_snapshot_id")
                        and attachment_access_restricted(
                            blobs.read(snapshot["object_key"], snapshot["content_hash"]), snapshot["source_url"]
                        )
                    ):
                        value["state"] = "failed"
                        value["segments"] = []
                        value["warnings"] = list(dict.fromkeys(value["warnings"] + ["attachment_access_restricted"]))
                if collection in SCHEMAS:
                    validate(SCHEMAS[collection], value)
                core.store.put(collection, key, value)
                counts[collection] = counts.get(collection, 0) + 1
        copied = 0
        for snapshot in core.store.list("snapshots"):
            data = blobs.read(snapshot["object_key"], snapshot["content_hash"])
            LocalBlobStore(destination).put(snapshot["object_key"], data, snapshot["content_hash"])
            copied += 1
        for event in core.store.list("events"):
            core.search.index_event(
                {"subject": {"kind": "public_event", "id": event["id"], "revision": event["version"]}}
            )
        # Validate standalone evidence and every historical version, including
        # withdrawn history. Public display gates still decide readability.
        evidence_rows = core.store.list("evidence")
        for parsed in core.store.list("parsed"):
            evidence_rows.extend(parsed["evidence"])
        for event in core.store.list("events") + core.store.list("event_versions"):
            evidence_rows.extend(event["evidence"])
        for candidate in core.store.list("candidates") + core.store.list("candidate_versions"):
            evidence_rows.extend(candidate["evidence"])
        for evidence in evidence_rows:
            snapshot = core.store.get("snapshots", evidence["snapshot_id"])
            if not snapshot:
                raise ValueError("Evidence has no snapshot")
            if evidence["content_hash"] != snapshot["content_hash"] or evidence.get("capture") != snapshot.get(
                "capture"
            ):
                raise ValueError("Evidence hash or provenance mismatch")
            if evidence["source_id"] != snapshot["source_id"] or canonical_url(evidence["source_url"]) != canonical_url(
                snapshot["source_url"]
            ):
                raise ValueError("Evidence source mismatch")
            parsed = core.store.get("parsed", evidence["snapshot_id"])
            segments = {s["id"]: s for s in parsed["segments"]}
            segment = segments.get(evidence["segment_id"])
            if not segment or evidence["quote"] not in segment["text"] or evidence["page"] != segment["page"]:
                raise ValueError("Evidence quote mismatch after sample sanitization")
        # The decision chain must survive the redaction, and no actor may survive it.
        for collection, keys in DECISION_CHAIN_KEYS.items():
            for record in core.store.list(collection):
                if not set(record) <= keys:
                    raise ValueError(f"{collection} record exposes more than the redacted decision chain")
                if any(field in record for fields in REDACTED_FIELDS.values() for field in fields):
                    raise ValueError(f"{collection} record still names its actor")
        counts["snapshot_files"] = copied
    finally:
        core.store.close()
    connection = sqlite3.connect(destination / "campus.sqlite3")
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT count(*) FROM campus_jobs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM campus_outbox").fetchone()[0] == 0
        present = {r[0] for r in connection.execute("SELECT DISTINCT collection FROM campus_records")}
        assert present <= COLLECTIONS | {"search_receipts", "local_index_receipts"}
    finally:
        connection.close()
    assert not (destination / ".server-secret").exists()
    report = {
        "source": "User supplied public historical sample, 2026-10-09",
        "auth_private_jobs_outbox_secrets_removed": True,
        "sources_enabled": False,
        # The funnel "163 candidates -> 73 published" is only checkable if the
        # decisions travel with the sample. They do, minus the actor.
        "review_decision_chain_retained": True,
        "redacted_actor_fields": {collection: list(fields) for collection, fields in REDACTED_FIELDS.items()},
        "counts": counts,
        "database_sha256": hashlib.sha256((destination / "campus.sqlite3").read_bytes()).hexdigest(),
    }
    (destination / "PUBLIC_SAMPLE.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    print(json.dumps(sanitize(args.source, args.destination), ensure_ascii=False, indent=2))

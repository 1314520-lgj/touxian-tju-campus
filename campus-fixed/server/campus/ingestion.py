"""Durable, bounded source discovery and fair batches; no network I/O under locks."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from .schema import now_iso, uid

MAX_KNOWN = 500
DISCOVERY_LIMIT = 100
LEASE_SECONDS = 3600


def scope(source):
    identity = {k: source.get(k) for k in ("origin_url", "adapter", "source_identity")}
    # Preserve legacy link-mode queue identities; revoke full-text work on any
    # authorization or mode change, including an edit during network I/O.
    if source.get("feed_content_mode", "link") == "authorized_fulltext":
        identity.update(
            feed_content_mode="authorized_fulltext",
            verification=source.get("verification"),
            verification_basis=source.get("verification_basis"),
        )
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def moment(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class SourceBacklog:
    def __init__(self, store):
        self.store = store

    def acquire(self, source_id):
        from .sources import FetchError

        with self.store.transaction():
            # This existing row serializes first-time creation on PostgreSQL too.
            source = self.store.get("sources", source_id)
            if not source or not source["enabled"]:
                raise FetchError("source_disabled_or_missing")
            state = self.store.get("source_ingestion", source_id) or {}
            now = datetime.now(UTC)
            if state.get("lease_until") and moment(state["lease_until"]) > now:
                raise FetchError("source_busy")
            if state.get("scope") != scope(source):
                state = dict(scope=scope(source), items={}, sequence=0, window_truncated=False, last_discovered_at=None)
            state.update(lease_token=uid(), lease_until=(now + timedelta(seconds=LEASE_SECONDS)).isoformat())
            self.store.put("source_ingestion", source_id, state)
        return source, state

    def check(self, source_id, state):
        from .sources import FetchError

        source = self.store.get("sources", source_id)
        current = self.store.get("source_ingestion", source_id)
        if not source or not source["enabled"] or scope(source) != state["scope"]:
            raise FetchError("source_changed_during_fetch")
        if (
            not current
            or current.get("lease_token") != state["lease_token"]
            or moment(current["lease_until"]) <= datetime.now(UTC)
        ):
            raise FetchError("source_lease_lost")
        return source

    def discover(self, state, items, truncated=False):
        known = state["items"]
        state["window_truncated"] = bool(truncated)
        for item in items:
            key = hashlib.sha256(item["url"].encode()).hexdigest()
            if key in known:
                if "embedded_ref" in item:
                    previous = known[key].get("embedded_ref") or {}
                    current = item.get("embedded_ref") or {}
                    if (
                        previous.get("html_hash") != current.get("html_hash")
                        or known[key]["title"] != item["title"]
                        or known[key]["published_at"] != item["published_at"]
                    ):
                        known[key].update(status="pending", attempts=0, next_retry_at=None, error_code=None)
                    known[key]["embedded_ref"] = item["embedded_ref"]
                known[key].update({k: item[k] for k in ("title", "published_at")})
                continue
            if len(known) >= MAX_KNOWN:
                completed = [(k, v) for k, v in known.items() if v["status"] == "completed"]
                if not completed:
                    state["window_truncated"] = True
                    continue
                oldest = min(completed, key=lambda pair: pair[1]["sequence"])[0]
                del known[oldest]
            state["sequence"] += 1
            known[key] = dict(
                item,
                sequence=state["sequence"],
                status="pending",
                attempts=0,
                last_attempt_at=None,
                last_success_at=None,
                next_retry_at=None,
                error_code=None,
            )
        state["last_discovered_at"] = now_iso()
        state.update(listing_failures=0, listing_retry_at=None)

    def listing_failure(self, state):
        attempts = state.get("listing_failures", 0) + 1
        delay = min(3600, 60 * 2 ** min(attempts - 1, 6))
        state.update(
            listing_failures=attempts, listing_retry_at=(datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        )

    def batch(self, state, limit):
        now = datetime.now(UTC)
        values = list(state["items"].values())
        pending = sorted((v for v in values if v["status"] == "pending"), key=lambda v: v["sequence"])
        failed = sorted(
            (v for v in values if v["status"] == "failed" and moment(v["next_retry_at"]) <= now),
            key=lambda v: v["next_retry_at"],
        )
        ready = (pending + failed)[:limit]
        if ready:
            return ready
        # Refresh old successful documents fairly, instead of repeating the first eight.
        return sorted(
            (v for v in values if v["status"] == "completed"), key=lambda v: (v["last_attempt_at"], v["sequence"])
        )[:limit]

    def outcome(self, item, error=None):
        now = datetime.now(UTC)
        item["last_attempt_at"] = now.isoformat()
        if error:
            item.update(status="failed", attempts=item["attempts"] + 1, error_code=error)
            delay = min(3600, 60 * 2 ** min(item["attempts"] - 1, 6))
            item["next_retry_at"] = (now + timedelta(seconds=delay)).isoformat()
        else:
            item.update(
                status="completed", attempts=0, error_code=None, next_retry_at=None, last_success_at=now.isoformat()
            )

    def save(self, source_id, state):
        self.check(source_id, state)
        self.store.put("source_ingestion", source_id, state)

    def finish(self, source_id, state, correlation_id=None):
        with self.store.transaction():
            self.save(source_id, state)
            pending = any(v["status"] == "pending" for v in state["items"].values())
            retry = [
                v["next_retry_at"] for v in state["items"].values() if v["status"] == "failed" and v["attempts"] < 5
            ]
            if state.get("listing_retry_at") and state.get("listing_failures", 0) < 5:
                retry.append(state["listing_retry_at"])
            if pending or retry:
                # A finite discovery queue drains now; errors back off and stop automatic retries after 5 attempts.
                self.store.enqueue(
                    "fetch_source",
                    {"source_id": source_id, "correlation_id": correlation_id or uid()},
                    "source-backfill:" + source_id + ":" + state["lease_token"],
                    run_at=None if pending else min(retry),
                )

    def release(self, source_id, token):
        with self.store.transaction():
            self.store.get("sources", source_id)
            state = self.store.get("source_ingestion", source_id)
            if state and state.get("lease_token") == token:
                state.update(lease_token=None, lease_until=None)
                self.store.put("source_ingestion", source_id, state)

    def summary(self, source):
        state = self.store.get("source_ingestion", source["id"])
        if not state or state.get("scope") != scope(source):
            return None
        values = list(state["items"].values())
        return dict(
            known_count=len(values),
            pending_count=sum(v["status"] == "pending" for v in values),
            failed_count=sum(v["status"] == "failed" for v in values),
            completed_count=sum(v["status"] == "completed" for v in values),
            window_truncated=state["window_truncated"],
            last_discovered_at=state["last_discovered_at"],
        )

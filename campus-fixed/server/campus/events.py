from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

from fastapi import APIRouter, Body, Query, Request

from .platform import APIError, require_user
from .schema import now_iso, uid, validate


def page(items, cursor=None, limit=20):
    try:
        start = int(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))) if cursor else 0
        if start < 0:
            raise ValueError()
    except Exception:
        raise APIError(400, "invalid_cursor", "分页游标无效") from None
    result = items[start : start + limit]
    nxt = (
        base64.urlsafe_b64encode(str(start + limit).encode()).decode().rstrip("=")
        if start + limit < len(items)
        else None
    )
    return {"items": result, "next_cursor": nxt}


def idempotent(core, request, user_id, operation, body, action):
    key = request.headers.get("Idempotency-Key", "")
    if not key or len(key) > 200:
        raise APIError(400, "idempotency_required", "请提供有效幂等键")
    identity = hashlib.sha256((user_id + ":" + operation + ":" + key).encode()).hexdigest()
    request_hash = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with core.store.transaction():
        previous = core.store.get("idempotency", identity)
        if previous:
            if previous["request_hash"] != request_hash:
                raise APIError(409, "idempotency_conflict", "相同请求标识的内容已经变化")
            if previous["result"].get("deleted"):
                raise APIError(410, "item_deleted", "这次导入创建的事项已经删除")
            return copy.deepcopy(previous["result"])
        result = action()
        core.store.put(
            "idempotency",
            identity,
            {
                "user_id": user_id,
                "operation": operation,
                "request_hash": request_hash,
                "result": result,
                "record_key": identity,
                "created_at": now_iso(),
            },
            expected_revision=0,
        )
        return result


class Events:
    IMPORTANT_FIELDS = {
        "materials",
        "deadlines",
        "action_steps",
        "status",
        "entry_urls",
        "publication_state",
        "action_required",
        "audience",
    }
    # Mirrors the contract enum so a stored summary can never widen the API surface.
    KNOWN_CHANGE_KINDS = {
        "deadline_or_date_touched",
        "content_added",
        "content_removed",
        "wording_revised",
    }

    def __init__(self, core):
        self.core, self.store = core, core.store
        # Set by _strong_match when an event is reused on title alone across two
        # independent source documents. Consumed by the reconcile in progress so
        # an unproven merge can never be published as settled fact.
        self._pending_merge_review = False

    def get_public(self, event_id, version=None):
        current = self.store.get("events", str(event_id))
        if not current or current["publication_state"] != "published":
            raise APIError(404, "not_found", "事项不存在或暂不可公开")
        if version is None:
            return current
        result = self.store.get("event_versions", f"{event_id}:{version}")
        if not result or result["publication_state"] != "published":
            raise APIError(404, "not_found", "该公开版本不存在")
        return result

    def snapshot_ids(self, event_id, version=None):
        event = self.get_public(event_id, version)
        mapping = self.store.get("event_snapshot_versions", f"{event_id}:{event['version']}")
        if mapping:
            return mapping["snapshot_ids"]
        # Compatibility for an existing approved record: never follow unreviewed descendants.
        candidates = (self.store.get("event_snapshots", event_id) or {}).get("snapshot_ids", [])
        latest = {}
        for sid in candidates or {e["snapshot_id"] for e in event["evidence"]}:
            snapshot = self.store.get("snapshots", sid)
            if snapshot and (
                snapshot["document_id"] not in latest
                or snapshot["fetched_at"] > latest[snapshot["document_id"]]["fetched_at"]
            ):
                latest[snapshot["document_id"]] = snapshot
        return sorted(x["snapshot_id"] for x in latest.values())

    def current_snapshot_ids(self, event_id):
        return self.snapshot_ids(event_id)

    def changes(self, event_id):
        """Changes between captures adopted by published versions, in capture order."""
        from . import change_detect

        event = self.get_public(event_id)
        adopted = {}
        versions = [
            v
            for v in self.store.list("event_versions")
            if v["id"] == event_id and v["publication_state"] == "published"
        ]
        if not any(v["version"] == event["version"] for v in versions):
            versions.append(event)
        for version in versions:
            mapping = self.store.get("event_snapshot_versions", f"{event_id}:{version['version']}")
            ids = mapping["snapshot_ids"] if mapping else {e["snapshot_id"] for e in version["evidence"]}
            for snapshot_id in ids:
                snapshot = self.store.get("snapshots", snapshot_id)
                if not snapshot or snapshot.get("visibility") != "public":
                    continue
                observed = (mapping or {}).get("observed_at", {}).get(snapshot_id) or snapshot["fetched_at"]
                adopted[snapshot_id] = min(adopted.get(snapshot_id, observed), observed)
        rows = []
        previous = {}
        for snapshot_id in sorted(adopted, key=lambda sid: (adopted[sid], sid)):
            snapshot = self.store.get("snapshots", snapshot_id)
            if not snapshot:
                continue
            previous_id = previous.get(snapshot["document_id"])
            previous[snapshot["document_id"]] = snapshot_id
            if previous_id is None:
                continue
            document = self.store.get("documents", snapshot["document_id"]) or {}
            change = (document.get("change_summaries") or {}).get(snapshot_id)
            if not change or change.get("previous_snapshot_id") != previous_id:
                before = self.store.get("parsed", previous_id)
                after = self.store.get("parsed", snapshot_id)
                if not before or not after:
                    continue
                report = change_detect.diff(
                    "\n".join(s["text"] for s in before["segments"]),
                    "\n".join(s["text"] for s in after["segments"]),
                )
                if not report["changed"]:
                    continue
                change = {
                    "previous_snapshot_id": previous_id,
                    "summary": change_detect.summarize(report),
                    "counts": report["counts"],
                    "kinds": report["kinds"],
                    "truncated": report["truncated"],
                }
            if not change or not change.get("summary"):
                continue
            raw_counts = change.get("counts") or {}
            counts = {
                name: max(0, int(raw_counts.get(name) or 0)) for name in ("added", "removed", "rewritten", "unchanged")
            }
            rows.append(
                {
                    "snapshot_id": snapshot_id,
                    "observed_at": adopted[snapshot_id],
                    "change": {
                        "previous_snapshot_id": change.get("previous_snapshot_id"),
                        "summary": str(change["summary"])[:400],
                        "counts": counts,
                        "kinds": [kind for kind in (change.get("kinds") or []) if kind in self.KNOWN_CHANGE_KINDS],
                        "truncated": bool(change.get("truncated")),
                    },
                }
            )
        rows.sort(key=lambda row: (row["observed_at"], row["snapshot_id"]))
        current = self.get_public(event_id)
        if current["version"] != event["version"]:
            raise APIError(409, "event_version_changed", "资料已更新，请重新查看变更记录")
        result = {"event_id": event["id"], "version": event["version"], "changes": rows}
        validate("EventChangeSummary", result)
        return result

    def quality(self, event_id):
        """Quality of exactly the captures adopted by the current public version."""
        from .blob_store import BlobError, blob_store
        from .sources import FetchError, attachment_access_restricted, public_source_url

        event = self.get_public(event_id)
        mapping = self.store.get("event_snapshot_versions", f"{event_id}:{event['version']}")
        ids = mapping["snapshot_ids"] if mapping else {e["snapshot_id"] for e in event["evidence"]}
        documents = []
        for sid in sorted(set(ids)):
            snapshot = self.store.get("snapshots", sid)
            if not snapshot or snapshot.get("visibility") != "public":
                raise APIError(404, "not_found", "该资料尚未发布或已撤回")
            try:
                source_url = public_source_url(snapshot["source_url"])
            except FetchError:
                raise APIError(404, "not_found", "该资料不是可公开的原文来源") from None
            parsed = self.store.get("parsed", sid)
            state = parsed.get("state") if parsed else "unparsed"
            if state not in {"ok", "partial", "failed"}:
                state = "unparsed"
            warnings = list(dict.fromkeys((parsed or {}).get("warnings", [])))
            attachment = bool(snapshot.get("parent_snapshot_id"))
            if attachment and snapshot["content_kind"] == "html":
                try:
                    body = blob_store(self.core).read(snapshot["object_key"], snapshot["content_hash"])
                    if attachment_access_restricted(body, snapshot["source_url"]):
                        if "attachment_access_restricted" not in warnings:
                            warnings.append("attachment_access_restricted")
                        if parsed:
                            state = "failed"
                except BlobError as error:
                    warnings.append(error.code)
                    if parsed:
                        state = "failed"
            documents.append(
                {
                    "snapshot_id": sid,
                    "source_url": source_url,
                    "content_kind": snapshot["content_kind"],
                    "state": state,
                    "is_attachment": attachment,
                    "warnings": warnings,
                }
            )
        current = self.get_public(event_id)
        if current["version"] != event["version"]:
            raise APIError(409, "quality_changed", "资料已更新，请重新查看完整性")
        result = {
            "event_id": event_id,
            "version": event["version"],
            "complete": bool(documents) and all(d["state"] == "ok" for d in documents),
            "documents": documents,
        }
        validate("EventDocumentQuality", result)
        return result

    def can_read_snapshot(self, snapshot_id):
        # A published historical proof stays readable, but a withdrawn event
        # revokes every version. Read the inventory once rather than per event.
        published = {e["id"] for e in self.store.list("events") if e["publication_state"] == "published"}
        for version in self.store.list("event_versions"):
            if version["id"] not in published or version["publication_state"] != "published":
                continue
            if any(e["snapshot_id"] == snapshot_id for e in version["evidence"]):
                return True
            mapping = self.store.get("event_snapshot_versions", f"{version['id']}:{version['version']}")
            if mapping and snapshot_id in mapping["snapshot_ids"]:
                return True
        return False

    def changed_since(self, event_id, acknowledged_version, current=None):
        current = current or self.store.get("events", event_id)
        if not current:
            return set()
        changed = set()
        for version in range(max(1, acknowledged_version + 1), current["version"] + 1):
            record = (
                current if version == current["version"] else self.store.get("event_versions", f"{event_id}:{version}")
            )
            if record:
                changed.update(record["changed_fields"])
        return changed

    def _older_than_current(self, candidate, event):
        if not event:
            return False
        mapping = self.store.get("event_snapshot_versions", f"{event['id']}:{event['version']}") or {}
        approved = [s for sid in mapping.get("snapshot_ids", []) if (s := self.store.get("snapshots", sid))]
        proposed = [s for sid in candidate["snapshot_ids"] if (s := self.store.get("snapshots", sid))]
        inputs = (
            self.store.get(
                "candidate_inputs",
                f"{candidate['candidate_id']}:{candidate['revision']}",
            )
            or {}
        )

        def stamp(s, proposed=False):
            sid = s["snapshot_id"]
            value = inputs.get(sid, {}).get("observed_at") if proposed else mapping.get("observed_at", {}).get(sid)
            return datetime.fromisoformat((value or s["fetched_at"]).replace("Z", "+00:00"))

        old_by_document = {s["document_id"]: s for s in approved}
        for snap in proposed:
            old = old_by_document.get(snap["document_id"])
            if old and stamp(snap, True) < stamp(old):
                return True
        # A reviewed cross-source link is an identity decision, not permission for
        # an older queued snapshot to undo a later published source update.
        return bool(approved and proposed and max(stamp(s, True) for s in proposed) < max(stamp(s) for s in approved))

    @staticmethod
    def _semantic(field, value):
        if field == "deadlines":
            return sorted(
                {
                    json.dumps(
                        {k: d[k] for k in ["kind", "precision", "date", "at"]},
                        sort_keys=True,
                    )
                    for d in value
                }
            )
        if field == "audience":
            return {k: sorted(v) if isinstance(v, list) else v for k, v in value.items() if k != "evidence_ids"}
        return value

    def _check_evidence(self, candidate):
        evidence_ids = {e["id"] for e in candidate["evidence"]}
        refs = [eid for ids in candidate["field_evidence"].values() for eid in ids]
        refs += candidate["audience"]["evidence_ids"]
        refs += [eid for d in candidate["deadlines"] for eid in d["evidence_ids"]]
        if not set(refs) <= evidence_ids:
            raise APIError(422, "missing_evidence", "候选字段引用了不存在的依据")
        for e in candidate["evidence"]:
            document = self.core.pipeline.parsed(e["snapshot_id"])
            segment = next(
                (s for s in (document or {}).get("segments", []) if s["id"] == e["segment_id"]),
                None,
            )
            snap = self.store.get("snapshots", e["snapshot_id"])
            if (
                not segment
                or not snap
                or e["quote"] not in segment["text"]
                or segment["page"] != e["page"]
                or snap["content_hash"] != e["content_hash"]
                or snap["source_url"] != e["source_url"]
                or snap["source_id"] != e["source_id"]
            ):
                raise APIError(422, "invalid_evidence", "证据无法在不可变快照中核对")
        if candidate["proposed_status"] == "cancelled" and not candidate["field_evidence"]["status"]:
            raise APIError(422, "missing_cancel_evidence", "取消事项必须有原文依据")

    @staticmethod
    def _title(text):
        return re.sub(r"[\W_]+", "", text.casefold())

    def _document_roots(self, snapshot_ids):
        """Independent source documents behind a set of snapshots.

        A notice and its attachments are separate documents but one notice, so
        they are folded to their root: an attachment snapshot points at its
        parent via `parent_snapshot_id`. Two *unrelated* roots therefore mean
        two genuinely different notices, which must never be silently merged
        under one event.
        """
        roots = set()
        for snapshot_id in snapshot_ids:
            snapshot = self.store.get("snapshots", snapshot_id)
            seen = set()
            while snapshot and snapshot.get("parent_snapshot_id") and snapshot["snapshot_id"] not in seen:
                seen.add(snapshot["snapshot_id"])
                snapshot = self.store.get("snapshots", snapshot["parent_snapshot_id"])
            if snapshot:
                roots.add(snapshot["document_id"])
        return roots

    def _strong_match(self, candidate):
        # Per-call signal; must not survive into the next candidate.
        self._pending_merge_review = False
        if candidate.get("target_event_id"):
            target = self.store.get("events", candidate["target_event_id"])
            if target:
                reviewed = any(
                    r.get("candidate_id") == candidate["candidate_id"]
                    and r.get("revision") == candidate["revision"]
                    and r.get("decision") == "accept"
                    for r in self.store.list("reviews")
                )
                if reviewed:
                    return target
        candidate_roots = self._document_roots(candidate["snapshot_ids"])
        dates = {(d["kind"], d["date"], d["at"]) for d in candidate["deadlines"] if d["precision"] != "unknown"}
        title_identity = self._title(candidate["title"])
        fallback = None
        for event in self.store.list("events"):
            mapping = self.store.get("event_snapshots", event["id"]) or {"snapshot_ids": []}
            event_roots = self._document_roots(mapping["snapshot_ids"])
            if event_roots & candidate_roots:
                # Same notice republished at the same URL: unambiguous identity.
                if len(event_roots & candidate_roots) == 1 and candidate_roots == event_roots:
                    return event
                # A different source document shares this event. Only accept the
                # merge when the title independently confirms it is the same
                # notice; otherwise leave it for a human instead of silently
                # folding two unrelated notices into one event.
                if title_identity == self._title(event["title"]):
                    return event
                if fallback is None:
                    fallback = event
                continue
            event_dates = {(d["kind"], d["date"], d["at"]) for d in event["deadlines"] if d["precision"] != "unknown"}
            same_identity = (
                title_identity == self._title(event["title"]) and candidate["event_type"] == event["event_type"]
            )
            audience_compatible = all(
                set(candidate["audience"][key]) == set(event["audience"][key])
                for key in [
                    "college_ids",
                    "education_levels",
                    "campuses",
                    "entry_years",
                ]
            )
            audience_compatible = (
                audience_compatible and candidate["audience"]["certainty"] == event["audience"]["certainty"]
            )
            audience_compatible = audience_compatible and self._title(
                candidate["audience"].get("conditions_text") or ""
            ) == self._title(event["audience"].get("conditions_text") or "")
            if (
                same_identity
                and audience_compatible
                and dates
                and dates == event_dates
                and candidate["change_kind"] == "new"
            ):
                return event
        if fallback is not None:
            # Distinct documents, matching title: merge is plausible but not
            # proven, so the merged event is forced into review below rather
            # than appearing as settled public fact.
            self._pending_merge_review = True
        return fallback

    def reconcile_candidate(self, candidate_id, revision):
        candidate = self.core.pipeline.get_accepted_candidate(candidate_id, revision)
        if not candidate or candidate["review_status"] != "accepted":
            raise APIError(409, "candidate_not_accepted", "候选版本尚未通过核对")
        validate("ExtractionCandidate", candidate)
        self._check_evidence(candidate)
        receipt_key = f"candidate:{candidate_id}:{revision}"
        with self.store.transaction():
            previous = self.store.get("event_receipts", receipt_key)
            if previous:
                return self.store.get("events", previous["event_id"])
            existing = self._strong_match(candidate)
            latest = self.store.get("candidates", candidate_id)
            newer_accepted = (
                latest
                and latest["revision"] > revision
                and any(
                    self.core.pipeline.get_accepted_candidate(candidate_id, n)
                    for n in range(revision + 1, latest["revision"] + 1)
                )
            )
            if newer_accepted or self._older_than_current(candidate, existing):
                # Queue delivery is at least once and may be out of order. A
                # historical accepted revision must never roll back approved data.
                self.store.put(
                    "event_receipts",
                    receipt_key,
                    {
                        "event_id": existing["id"] if existing else None,
                        "candidate_id": candidate_id,
                        "candidate_revision": revision,
                        "skipped": True,
                    },
                )
                return existing
            identity = existing["id"] if existing else uid()
            version = existing["version"] + 1 if existing else 1
            # Historical evidence lives in event_versions. The current event
            # describes the current candidate, including only its field proofs.
            evidence = {e["id"]: e for e in candidate["evidence"]}
            status = candidate["proposed_status"]
            if (
                candidate["review_required"]
                or candidate["audience"]["certainty"] == "unknown"
                or any(d["precision"] == "unknown" for d in candidate["deadlines"])
            ):
                status = "under_review" if status != "cancelled" else status
            if self._pending_merge_review and status != "cancelled":
                # Two independent documents were folded into one event on title
                # evidence alone. Keep the merge visible but unreviewed rather
                # than presenting it as confirmed public fact.
                status = "under_review"
            event = {
                "id": identity,
                "version": version,
                "title": candidate["title"],
                "event_type": candidate["event_type"],
                "status": status,
                "publication_state": (existing or {}).get("publication_state", "published"),
                "action_required": candidate["action_required"],
                "action_steps": candidate["action_steps"],
                "materials": candidate["materials"],
                "entry_urls": candidate["entry_urls"],
                "deadlines": [] if status == "cancelled" else candidate["deadlines"],
                "audience": candidate["audience"],
                "evidence": list(evidence.values()),
                "field_evidence": candidate["field_evidence"],
                "source_ids": sorted(
                    set((existing or {}).get("source_ids", [])) | {e["source_id"] for e in candidate["evidence"]}
                ),
                "changed_fields": [],
                "published_at": (existing or {}).get("published_at")
                or next(
                    (
                        s.get("published_at")
                        for sid in candidate["snapshot_ids"]
                        if (s := self.store.get("snapshots", sid))
                    ),
                    None,
                ),
                "updated_at": now_iso(),
                "visibility": "public",
            }
            fields = [
                "title",
                "status",
                "action_required",
                "action_steps",
                "materials",
                "entry_urls",
                "deadlines",
                "audience",
                "publication_state",
            ]
            event["changed_fields"] = [
                k for k in fields if existing and self._semantic(k, existing[k]) != self._semantic(k, event[k])
            ]
            validate("EventVersion", event)
            self.store.put(
                "events",
                identity,
                event,
                expected_revision=self.store.revision("events", identity),
            )
            self.store.put("event_versions", f"{identity}:{version}", event, expected_revision=0)
            mapping = self.store.get("event_snapshots", identity) or {"snapshot_ids": []}
            mapping["snapshot_ids"] = sorted(set(mapping["snapshot_ids"]) | set(candidate["snapshot_ids"]))
            self.store.put("event_snapshots", identity, mapping)
            old_ids = (self.store.get("event_snapshot_versions", f"{identity}:{version - 1}") or {}).get(
                "snapshot_ids", []
            )

            def root_document(snapshot_id):
                snap = self.store.get("snapshots", snapshot_id)
                while snap and snap.get("parent_snapshot_id"):
                    snap = self.store.get("snapshots", snap["parent_snapshot_id"])
                return snap["document_id"] if snap else None

            replacing = {root_document(sid) for sid in candidate["snapshot_ids"]}
            current_ids = set(candidate["snapshot_ids"]) | {
                sid for sid in old_ids if root_document(sid) not in replacing
            }
            if set(event["changed_fields"]) & self.IMPORTANT_FIELDS:
                # Other sources may still repeat superseded requirements. Their
                # original body remains searchable only in historical versions.
                current_ids = set(candidate["snapshot_ids"])
            # Freeze observed ordering with each approved version. Immutable bytes can
            # reappear later, but delayed historical candidates retain their old order.
            candidate_inputs = self.store.get("candidate_inputs", f"{candidate_id}:{revision}") or {}
            old_mapping = self.store.get("event_snapshot_versions", f"{identity}:{version - 1}") or {}
            observed = {}
            for snapshot_id in current_ids:
                snap = self.store.get("snapshots", snapshot_id)
                observed[snapshot_id] = (
                    candidate_inputs.get(snapshot_id, {}).get("observed_at")
                    or old_mapping.get("observed_at", {}).get(snapshot_id)
                    or snap["fetched_at"]
                )
            self.store.put(
                "event_snapshot_versions",
                f"{identity}:{version}",
                {"snapshot_ids": sorted(current_ids), "observed_at": observed},
                expected_revision=0,
            )
            self.store.put(
                "event_receipts",
                receipt_key,
                {
                    "event_id": identity,
                    "candidate_id": candidate_id,
                    "candidate_revision": revision,
                },
            )
            if existing and set(event["changed_fields"]) & self.IMPORTANT_FIELDS:
                for state in self.store.list("user_states"):
                    if state["event_id"] == identity and not state["needs_attention"]:
                        state["needs_attention"] = True
                        state["state_revision"] += 1
                        validate("UserEventState", state)
                        self.store.put("user_states", f"{state['user_id']}:{identity}", state)
            self.store.emit(
                "public_event.changed",
                identity,
                version,
                subject={"kind": "public_event", "id": identity, "revision": version},
                change_kind="cancelled" if status == "cancelled" else "updated" if existing else "created",
            )
            return event

    def state(self, user_id, event_id):
        return self.store.get("user_states", f"{user_id}:{event_id}")

    def set_state(self, user_id, event_id, patch):
        validate("EventStatePatch", patch)
        with self.store.transaction():
            event = self.get_public(event_id)
            if patch["acknowledged_event_version"] > event["version"]:
                raise APIError(409, "event_version_conflict", "不能确认尚不存在的事项版本")
            key = f"{user_id}:{event_id}"
            state = self.state(user_id, event_id)
            old_revision = state["state_revision"] if state else 0
            if patch["expected_state_revision"] != old_revision:
                raise APIError(409, "state_conflict", "个人状态已变化，请刷新后重试")
            if state and patch["acknowledged_event_version"] < state["acknowledged_version"]:
                raise APIError(409, "event_version_conflict", "不能回退已确认的事项版本")
            state = state or {
                "event_id": event_id,
                "user_id": user_id,
                "state": "todo",
                "read": False,
                "saved": False,
                "acknowledged_version": event["version"],
                "state_revision": 0,
                "needs_attention": False,
            }
            for k in ["state", "read", "saved"]:
                if k in patch:
                    state[k] = patch[k]
            state["state_revision"] = old_revision + 1
            state["acknowledged_version"] = patch["acknowledged_event_version"]
            state["needs_attention"] = bool(
                self.changed_since(event_id, patch["acknowledged_event_version"], event) & self.IMPORTANT_FIELDS
            )
            validate("UserEventState", state)
            self.store.put(
                "user_states",
                key,
                state,
                expected_revision=self.store.revision("user_states", key),
            )
            self.store.emit(
                "user_event_state.changed",
                event_id,
                state["state_revision"],
                user_id=user_id,
                subject={
                    "kind": "public_event",
                    "id": event_id,
                    "revision": event["version"],
                },
                change_kind="state_changed",
            )
            return state

    def _match(self, event, profile):
        audience = event["audience"]
        reasons = []
        unknown = audience["certainty"] != "explicit"
        for key, pkey in [
            ("education_levels", "education_level"),
            ("college_ids", "college_id"),
            ("campuses", "campus"),
            ("entry_years", "entry_year"),
        ]:
            wanted = audience[key]
            if wanted:
                got = profile.get(pkey)
                if got is None or got == "unknown":
                    unknown = True
                    reasons.append("画像尚未填写完整，适用对象需要核对")
                elif got not in wanted:
                    return "not_matched", ["原文明确适用对象与画像不一致"]
                else:
                    reasons.append("原文适用对象符合你的画像")
        if audience.get("conditions_text") and not any(
            audience[k] for k in ["education_levels", "college_ids", "campuses", "entry_years"]
        ):
            unknown = True
        return ("possible" if unknown else "matched"), list(dict.fromkeys(reasons)) or ["通知未限制所选学生身份"]

    def feed(self, user_id):
        profile = self.store.get("profiles", user_id) or {
            "education_level": "unknown",
            "college_id": None,
            "entry_year": None,
            "campus": "unknown",
        }
        subscriptions = self.store.get("subscriptions", user_id) or {
            "source_ids": [],
            "event_types": [],
        }
        cards = []
        states = {s["event_id"]: s for s in self.store.list("user_states") if s["user_id"] == user_id}
        source_map = {s["id"]: s for s in self.store.list("sources")}
        for event in self.store.list("events"):
            if event["publication_state"] != "published":
                continue
            state = states.get(event["id"])
            source_filter = subscriptions.get("source_mode") == "selected" or (
                subscriptions.get("source_mode") != "all" and bool(subscriptions["source_ids"])
            )
            if (
                source_filter
                and not set(event["source_ids"]) & set(subscriptions["source_ids"])
                and not (state or {}).get("saved")
            ):
                continue
            if (
                subscriptions["event_types"]
                and event["event_type"] not in subscriptions["event_types"]
                and not (state or {}).get("saved")
            ):
                continue
            match, reasons = self._match(event, profile)
            if match == "not_matched" and not (state or {}).get("saved"):
                continue
            uncertain = event["status"] == "under_review" or match in [
                "possible",
                "unknown",
            ]
            classification = (
                "needs_review"
                if uncertain
                else "required"
                if event["action_required"] is True
                else "optional"
                if event["action_steps"]
                else "informational"
            )
            deadlines = [d for d in event["deadlines"] if d["precision"] != "unknown"]
            # Lead the label with the date that still matters. Rendering the
            # original order made a card headlined by an already-closed 报名
            # window while its real 考试 date sat behind the fold.
            today_iso = datetime.now(TZ_SHANGHAI).date().isoformat()

            def upcoming_first(deadline):
                value = deadline.get("date") or deadline.get("at") or "9999"
                return (value[:10] < today_iso, value)

            labels = [
                d["date"] + "截止（原文未提供具体时刻）" if d["precision"] == "date" else d["at"]
                for d in sorted(deadlines, key=upcoming_first)
            ]
            sources = [source_map.get(sid) for sid in event["source_ids"]]
            stale = any(not s or s["health"] != "healthy" for s in sources)
            cards.append(
                {
                    "event": event,
                    "state": state,
                    "classification": classification,
                    "match": match,
                    "match_reasons": reasons,
                    "deadline_label": "；".join(labels) if labels else None,
                    "source_stale": stale,
                }
            )

        def sort_key(c):
            attention = (c["state"] or {}).get("needs_attention", False)
            done = (c["state"] or {}).get("state") in ["done", "archived"]
            # Order by the next deadline that still matters, not by the earliest
            # date the notice happens to mention. A notice whose 报名 window
            # closed in September but whose 考试 is in December was ordered by
            # the September date, so the student's first card opened with a date
            # that had already passed. Undated notices sort last: they are
            # reference material, not today's task.
            today = datetime.now(TZ_SHANGHAI).date().isoformat()
            dates = [d.get("date") or d.get("at") or "9999" for d in c["event"]["deadlines"]]
            upcoming = [value for value in dates if value[:10] >= today]
            return (
                done,
                not attention,
                min(upcoming, default="9999"),
                c["event"]["title"],
            )

        return sorted(cards, key=sort_key)

    def list_personal(self, user_id):
        return [x for x in self.store.list("personal_items") if x["owner_user_id"] == user_id]

    def selected_subjects(self, user_id):
        selected = [
            {"kind": "personal_item", "id": x["id"], "revision": x["revision"]}
            for x in self.list_personal(user_id)
            if x["state"] not in ["done", "archived"]
        ]
        for state in self.store.list("user_states"):
            if state["user_id"] == user_id and state["saved"]:
                event = self.store.get("events", state["event_id"])
                if event:
                    selected.append(
                        {
                            "kind": "public_event",
                            "id": event["id"],
                            "revision": event["version"],
                        }
                    )
        return selected

    def resolve_subject(self, user_id, subject):
        validate("SubjectRef", subject)
        if subject["kind"] == "public_event":
            event = self.store.get("events", subject["id"])
            state = self.state(user_id, subject["id"])
            if not event:
                return None
            return {
                "subject": {
                    "kind": "public_event",
                    "id": event["id"],
                    "revision": event["version"],
                },
                "record": event,
                "selected": bool(state and state["saved"]),
                "state": (state or {}).get("state", "todo"),
                "withdrawn": event["publication_state"] != "published",
                "deleted": False,
            }
        item = self.store.get("personal_items", subject["id"])
        if item and item["owner_user_id"] == user_id:
            return {
                "subject": {
                    "kind": "personal_item",
                    "id": item["id"],
                    "revision": item["revision"],
                },
                "record": item,
                "selected": item["state"] not in ["done", "archived"],
                "state": item["state"],
                "withdrawn": False,
                "deleted": False,
            }
        tombstone = self.store.get("personal_tombstones", subject["id"])
        if tombstone and tombstone["owner_user_id"] == user_id:
            return {
                "subject": {
                    "kind": "personal_item",
                    "id": subject["id"],
                    "revision": tombstone["revision"],
                },
                "record": None,
                "selected": False,
                "state": "archived",
                "withdrawn": False,
                "deleted": True,
            }
        return None

    def create_private(self, user_id, draft):
        validate("PersonalItemDraft", draft)
        item = {
            "id": uid(),
            "owner_user_id": user_id,
            "visibility": "private",
            "draft": copy.deepcopy(draft),
            "state": "todo",
            "revision": 1,
        }
        validate("PersonalItem", item)
        with self.store.transaction():
            self.store.put("personal_items", item["id"], item, expected_revision=0)
            self.store.emit(
                "personal_item.changed",
                item["id"],
                1,
                user_id=user_id,
                subject={"kind": "personal_item", "id": item["id"], "revision": 1},
                change_kind="created",
            )
        return item

    def change_private(self, user_id, item_id, patch=None, delete=False):
        if not delete:
            validate("PersonalItemPatch", patch)
        with self.store.transaction():
            item = self.store.get("personal_items", item_id)
            if not item or item["owner_user_id"] != user_id:
                raise APIError(404, "not_found", "个人事项不存在")
            if not delete and patch["expected_revision"] != item["revision"]:
                raise APIError(409, "item_conflict", "事项已变化，请刷新")
            revision = item["revision"] + 1
            if delete:
                self.store.put(
                    "personal_tombstones",
                    item_id,
                    {
                        "owner_user_id": user_id,
                        "revision": revision,
                        "deleted_at": now_iso(),
                    },
                )
                self.store.delete("personal_items", item_id)
                # Remove private text from cached responses and historical digests too.
                for cached in self.store.list("idempotency"):
                    if (
                        cached.get("user_id") == user_id
                        and cached.get("operation") == "create_private"
                        and (cached.get("result") or {}).get("id") == item_id
                    ):
                        cached["result"] = {"deleted": True, "id": item_id}
                        self.store.put("idempotency", cached["record_key"], cached)
                for key in [
                    f"{user_id}:{d['digest']['date']}" for d in self.store.list("digests") if d["user_id"] == user_id
                ]:
                    row = self.store.get("digests", key)
                    row["digest"]["personal_items"] = [i for i in row["digest"]["personal_items"] if i["id"] != item_id]
                    self.store.put("digests", key, row)
            else:
                if "draft" in patch:
                    item["draft"] = patch["draft"]
                if "state" in patch:
                    item["state"] = patch["state"]
                item["revision"] = revision
                validate("PersonalItem", item)
                self.store.put(
                    "personal_items",
                    item_id,
                    item,
                    expected_revision=self.store.revision("personal_items", item_id),
                )
            self.store.emit(
                "personal_item.changed",
                item_id,
                revision,
                user_id=user_id,
                subject={"kind": "personal_item", "id": item_id, "revision": revision},
                change_kind="deleted" if delete else "updated",
            )
            return None if delete else item

    def publication(self, event_id, patch, actor_id):
        validate("EventPublicationPatch", patch)
        with self.store.transaction():
            event = self.store.get("events", event_id)
            if not event:
                raise APIError(404, "not_found", "事项不存在")
            if event["version"] != patch["expected_event_version"]:
                raise APIError(409, "event_conflict", "事项版本已经变化")
            event["version"] += 1
            event["publication_state"] = patch["publication_state"]
            event["updated_at"] = now_iso()
            event["changed_fields"] = ["publication_state"]
            validate("EventVersion", event)
            self.store.put("events", event_id, event)
            self.store.put(
                "event_versions",
                f"{event_id}:{event['version']}",
                event,
                expected_revision=0,
            )
            previous = self.store.get("event_snapshot_versions", f"{event_id}:{event['version'] - 1}")
            if previous:
                self.store.put(
                    "event_snapshot_versions",
                    f"{event_id}:{event['version']}",
                    previous,
                    expected_revision=0,
                )
            self.store.put(
                "publication_audits",
                f"publication:{uid()}",
                {
                    "event_id": event_id,
                    "actor_id": actor_id,
                    "reason": patch["reason"],
                    "publication_state": patch["publication_state"],
                    "created_at": now_iso(),
                },
            )
            self.store.emit(
                "public_event.changed",
                event_id,
                event["version"],
                subject={
                    "kind": "public_event",
                    "id": event_id,
                    "revision": event["version"],
                },
                change_kind="withdrawn" if patch["publication_state"] == "withdrawn" else "updated",
            )
            return event

    def unusable_events(self, today):
        """Published notices whose title is not a notice at all.

        An earlier, more permissive review gate published column fragments
        ("【考试安排】2022"), navigation pages ("下载中心") and bare links; they sit on
        a student's list until something takes them back. The one hard rule here:
        a notice that still has a deadline ahead is never swept, however odd its
        title — something a student can still act on outranks tidiness.
        """
        from server.campus.hygiene import title_problems

        rows = []
        for event in self.store.list("events"):
            if event["publication_state"] != "published":
                continue
            problems = title_problems(event.get("title"))
            if not problems:
                continue
            dates = self.deadline_dates(event)
            if dates and max(dates) >= today:
                continue
            rows.append({"id": event["id"], "title": event["title"], "reason": "、".join(problems)})
        return rows

    def withdraw_unusable(self, actor_id, reason="标题不是通知（栏目碎片/附件名/新闻稿），自动下架", apply=True):
        """Take non-notices back out of the student-facing surfaces."""
        today = datetime.now(TZ_SHANGHAI).date()
        unusable = self.unusable_events(today)
        if apply:
            for row in unusable:
                event = self.store.get("events", row["id"])
                if not event or event["publication_state"] != "published":
                    continue
                self.publication(
                    row["id"],
                    {
                        "expected_event_version": event["version"],
                        "publication_state": "withdrawn",
                        "reason": f"{reason}：{row['reason']}",
                    },
                    actor_id,
                )
        return {"unusable": unusable, "applied": bool(apply), "reason": reason}

    @staticmethod
    def deadline_dates(event):
        """Every calendar date this event states, ignoring unusable values."""
        found = []
        for deadline in event.get("deadlines") or []:
            value = deadline.get("date") or (deadline.get("at") or "")[:10]
            if not value:
                continue
            try:
                found.append(date.fromisoformat(value))
            except ValueError:
                continue
        return found

    def expired_events(self, today):
        """Published notices whose every stated deadline is already behind us.

        A notice with no usable date is not expired — it has nothing to expire,
        and evergreen instructions ("学位认证操作步骤") stay useful for years. A
        notice with any future deadline is still actionable and stays too.
        """
        rows = []
        for event in self.store.list("events"):
            if event["publication_state"] != "published":
                continue
            dates = self.deadline_dates(event)
            if not dates or max(dates) >= today:
                continue
            rows.append({"id": event["id"], "title": event["title"], "deadline": max(dates).isoformat()})
        return sorted(rows, key=lambda row: row["deadline"])

    def withdraw_expired(self, today, actor_id, reason="截止时间已过，自动下架", apply=True):
        """Take expired notices out of every student-facing surface.

        Withdrawal is the product's own publication state: the notice leaves the
        feed, the public list, search and answers, the change is audited with the
        actor and reason, and the record can be read again if the decision is
        reversed. It is not a destructive delete, so user state, evidence and the
        publication trail stay consistent.
        """
        expired = self.expired_events(today)
        if apply:
            for row in expired:
                event = self.store.get("events", row["id"])
                if not event or event["publication_state"] != "published":
                    continue
                self.publication(
                    row["id"],
                    {
                        "expected_event_version": event["version"],
                        "publication_state": "withdrawn",
                        "reason": reason,
                    },
                    actor_id,
                )
        return {"expired": expired, "applied": bool(apply), "reason": reason}


def make_router(core):
    router = APIRouter()

    @router.get("/events")
    def events(cursor: str | None = None, limit: int = Query(20, ge=1, le=100)):
        return page(
            sorted(
                [e for e in core.store.list("events") if e["publication_state"] == "published"],
                key=lambda e: e["updated_at"],
                reverse=True,
            ),
            cursor,
            limit,
        )

    @router.get("/events/{event_id}")
    def event(event_id: str):
        return core.events.get_public(event_id)

    @router.get("/events/{event_id}/changes")
    def event_changes(event_id: str):
        return core.events.changes(event_id)

    @router.get("/events/{event_id}/quality")
    def event_quality(event_id: str):
        return core.events.quality(event_id)

    @router.get("/events/{event_id}/versions")
    def versions(event_id: str, cursor: str | None = None, limit: int = Query(20, ge=1, le=100)):
        core.events.get_public(event_id)
        values = [
            e
            for e in core.store.list("event_versions")
            if e["id"] == event_id and e["publication_state"] == "published"
        ]
        return page(sorted(values, key=lambda e: e["version"], reverse=True), cursor, limit)

    @router.get("/me/feed")
    def feed(
        request: Request,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
    ):
        user = require_user(request)
        return page(core.events.feed(user["id"]), cursor, limit)

    @router.get("/me/events/{event_id}/state")
    def state(event_id: str, request: Request):
        user = require_user(request)
        core.events.get_public(event_id)
        return {"state": core.events.state(user["id"], event_id)}

    @router.patch("/me/events/{event_id}/state")
    def change_state(event_id: str, request: Request, body: dict = Body(...)):
        user = require_user(request, write=True)
        return core.events.set_state(user["id"], event_id, body)

    @router.get("/me/items")
    def items(
        request: Request,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
    ):
        user = require_user(request)
        return page(core.events.list_personal(user["id"]), cursor, limit)

    @router.post("/me/items", status_code=201)
    def create_item(request: Request, body: dict = Body(...)):
        user = require_user(request, write=True)
        validate("PersonalItemDraft", body)
        return idempotent(
            core,
            request,
            user["id"],
            "create_private",
            body,
            lambda: core.events.create_private(user["id"], body),
        )

    @router.patch("/me/items/{item_id}")
    def update_item(item_id: str, request: Request, body: dict = Body(...)):
        user = require_user(request, write=True)
        return core.events.change_private(user["id"], item_id, body)

    @router.delete("/me/items/{item_id}", status_code=204)
    def remove_item(item_id: str, request: Request):
        user = require_user(request, write=True)
        core.events.change_private(user["id"], item_id, delete=True)

    @router.get("/admin/events")
    def admin_events(
        request: Request,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
    ):
        require_user(request, roles={"admin"})
        return page(
            sorted(core.store.list("events"), key=lambda e: e["updated_at"], reverse=True),
            cursor,
            limit,
        )

    @router.patch("/admin/events/{event_id}/publication")
    def publication(event_id: str, request: Request, body: dict = Body(...)):
        user = require_user(request, roles={"admin"}, write=True)
        return core.events.publication(event_id, body, user["id"])

    return router
